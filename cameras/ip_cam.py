"""IP-камера (RTSP): фоновое чтение потока, чтобы всегда иметь свежий кадр под рукой."""
import logging
import os
import threading
import time

# TCP для RTSP стабильнее UDP (меньше битых кадров). Нужно задать до создания VideoCapture.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from core.diagnostics import mask_url  # noqa: E402

log = logging.getLogger("ip_cam")


# таймауты открытия и чтения RTSP (мс): без них OpenCV/FFmpeg может ждать недоступную камеру очень долго
OPEN_TIMEOUT_MS = 10_000
READ_TIMEOUT_MS = 10_000


class FrozenFrame(RuntimeError):
    """Камера отдаёт один и тот же кадр - поток «замёрз», хотя формально работает."""


class IPCamera:
    """Поток читается в отдельном потоке, поэтому capture() возвращает кадр мгновенно
    и не отдаёт устаревшие кадры из буфера. Обрыв связи обрабатывается переподключением."""

    def __init__(self, url: str, reconnect_delay: float = 2.0, max_frame_age: float = 1.0):
        self.url = url
        self.reconnect_delay = reconnect_delay
        self.max_frame_age = max_frame_age  # кадр старше этого (сек) считается устаревшим

        self._cap: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._frame_time = 0.0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_returned: np.ndarray | None = None  # для проверки «замёрзшего» потока

    # --- жизненный цикл ---
    def start(self, timeout: float = 10.0) -> None:
        self._cap = self._connect()
        if self._cap is None:
            raise RuntimeError("не удалось подключиться к IP-камере")
        self._thread = threading.Thread(target=self._reader, name="ip-cam-reader", daemon=True)
        self._thread.start()
        if self._wait_frame(timeout) is None:
            self.stop()
            raise RuntimeError(f"IP-камера не отдаёт кадры ({mask_url(self.url)})")
        with self._cond:
            h, w = self._frame.shape[:2]
        fps = self._cap.get(cv2.CAP_PROP_FPS) if self._cap is not None else 0
        log.info("IP-камера подключена: %s, кадр %dx%d, %.0f к/с", mask_url(self.url), w, h, fps or 0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                # поток читалки висит внутри read(): освобождать камеру из другого потока опасно
                # (может уронить процесс), поэтому оставляем - читалка закроет её сама, когда очнётся
                log.warning("поток чтения IP-камеры не завершился за 3 с, оставляю его закрыться самостоятельно")
                return
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        log.debug("IP-камера закрыта")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # --- внутренняя кухня ---
    def _connect(self) -> cv2.VideoCapture | None:
        params = []
        # параметры таймаутов есть в OpenCV >= 4.5.2; в старых версиях просто открываем без них
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS]
        try:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, params) if params else cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        except cv2.error:
            return None
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _reader(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    ok, frame = self._cap.read() if self._cap else (False, None)
                except cv2.error:
                    ok, frame = False, None
                if ok and frame is not None:
                    with self._cond:
                        self._frame = frame
                        self._frame_time = time.monotonic()
                        self._cond.notify_all()
                    continue

                if self._stop.is_set():
                    break
                lost_at, attempts = time.monotonic(), 0
                log.warning("IP-камера потеряла поток, переподключение каждые %.0f с...", self.reconnect_delay)
                if self._cap is not None:
                    self._cap.release()
                self._cap = None
                while not self._stop.is_set() and self._cap is None:
                    time.sleep(self.reconnect_delay)
                    attempts += 1
                    self._cap = self._connect()
                    if self._cap is None:
                        log.debug("IP-камера: попытка переподключения %d не удалась", attempts)
                if self._cap is not None:
                    log.info("IP-камера переподключилась через %.0f с (попыток: %d)",
                             time.monotonic() - lost_at, attempts)
        except Exception:
            # читалка не должна умирать молча: capture() начнёт возвращать ошибки,
            # и основной цикл переоткроет камеру
            log.exception("поток чтения IP-камеры упал")
        finally:
            if self._stop.is_set() and self._cap is not None:
                self._cap.release()
                self._cap = None

    def _wait_frame(self, timeout: float) -> np.ndarray | None:
        """Ждёт свежий кадр (не старше max_frame_age)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                fresh = self._frame is not None and time.monotonic() - self._frame_time <= self.max_frame_age
                if fresh:
                    return self._frame.copy()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # --- съёмка ---
    def capture(self, timeout: float = 5.0) -> np.ndarray:
        frame = self._wait_frame(timeout)
        if frame is None:
            raise RuntimeError("IP-камера не вернула свежий кадр")
        # живой поток с шумом матрицы не даёт два побитно одинаковых кадра с разницей в секунды
        if self._last_returned is not None and np.array_equal(frame, self._last_returned):
            raise FrozenFrame("IP-камера отдаёт тот же кадр, что и в прошлый раз (поток замёрз)")
        self._last_returned = frame
        return frame

    @staticmethod
    def encode_jpg(frame: np.ndarray, quality: int = 95) -> bytes:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("не удалось закодировать кадр в JPG")
        return buf.tobytes()
