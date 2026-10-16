"""IP-камера (RTSP): фоновое чтение потока, чтобы всегда иметь свежий кадр под рукой."""
import logging
import os
import threading
import time

# TCP для RTSP стабильнее UDP (меньше битых кадров). Нужно задать до создания VideoCapture.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

log = logging.getLogger("ip_cam")


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

    # --- жизненный цикл ---
    def start(self, timeout: float = 10.0) -> None:
        self._cap = self._connect()
        if self._cap is None:
            raise RuntimeError("не удалось подключиться к IP-камере")
        self._thread = threading.Thread(target=self._reader, name="ip-cam-reader", daemon=True)
        self._thread.start()
        if self._wait_frame(timeout) is None:
            self.stop()
            raise RuntimeError("IP-камера не отдаёт кадры")
        log.info("IP-камера подключена")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # --- внутренняя кухня ---
    def _connect(self) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _reader(self) -> None:
        while not self._stop.is_set():
            ok, frame = self._cap.read() if self._cap else (False, None)
            if ok and frame is not None:
                with self._cond:
                    self._frame = frame
                    self._frame_time = time.monotonic()
                    self._cond.notify_all()
                continue

            log.warning("IP-камера потеряла поток, переподключение...")
            if self._cap is not None:
                self._cap.release()
            self._cap = None
            while not self._stop.is_set() and self._cap is None:
                time.sleep(self.reconnect_delay)
                self._cap = self._connect()

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
        return frame

    @staticmethod
    def encode_jpg(frame: np.ndarray, quality: int = 95) -> bytes:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("не удалось закодировать кадр в JPG")
        return buf.tobytes()
