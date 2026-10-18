"""Термальная камера: захват кадра, матрица температур и хитмап."""
import io
import logging
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger("thermal")


@dataclass
class ThermalShot:
    raw16: np.ndarray  # сырые 16-битные значения сенсора (192x256)
    temp: np.ndarray  # матрица температур, °C, float32 (192x256)

    def heatmap(self, scale: int = 3) -> np.ndarray:
        return make_heatmap(self.raw16, scale)

    def heatmap_png(self) -> bytes:
        ok, buf = cv2.imencode(".png", self.heatmap())
        if not ok:
            raise RuntimeError("не удалось закодировать heatmap в PNG")
        return buf.tobytes()

    def temp_npy(self) -> bytes:
        bio = io.BytesIO()
        np.save(bio, self.temp)
        return bio.getvalue()


def make_heatmap(raw16: np.ndarray, scale: int = 3) -> np.ndarray:
    lo, hi = np.percentile(raw16, (2, 98))
    gray = np.clip((raw16.astype(np.float32) - lo) / max(hi - lo, 1), 0, 1)
    gray = (gray * 255).astype(np.uint8)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    heat = cv2.applyColorMap(gray, cv2.COLORMAP_TURBO)
    h, w = raw16.shape
    return cv2.resize(heat, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)


class ThermalCamera:
    """Камера отдаёт кадр 256x384 YUYV: верхняя половина - картинка, нижняя - сырые данные температур."""

    def __init__(self, device: str, width: int, height: int, flush_frames: int = 2):
        self.device = device
        self.width = width
        self.height = height
        self.flush_frames = flush_frames  # сколько старых кадров выбросить перед съёмкой
        self._cap: cv2.VideoCapture | None = None

    # --- жизненный цикл ---
    def open(self) -> None:
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.device)
        if not cap.isOpened():
            raise RuntimeError(f"нет термальной камеры {self.device}")
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height * 2)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        log.info("термальная камера открыта: %s", self.device)

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # --- съёмка ---
    def capture(self) -> ThermalShot:
        if self._cap is None:
            raise RuntimeError("термальная камера не открыта")

        # сбрасываем накопленные в буфере кадры, чтобы получить свежий
        for _ in range(self.flush_frames):
            self._cap.grab()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise RuntimeError("термальная камера не вернула кадр")
        return self._frame_to_shot(frame)

    def _frame_to_shot(self, frame: np.ndarray) -> ThermalShot:
        expected = (self.height * 2, self.width, 2)
        if frame.size != np.prod(expected):
            raise RuntimeError(f"нужен кадр {expected}, пришло {frame.shape}")
        frame = frame.reshape(expected)

        bottom = frame[self.height:]
        raw16 = bottom[:, :, 0].astype(np.uint16) + (bottom[:, :, 1].astype(np.uint16) << 8)
        temp = raw16.astype(np.float32) / 64.0 - 273.15
        return ThermalShot(raw16=raw16, temp=temp)
