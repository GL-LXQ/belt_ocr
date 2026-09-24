from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import (
    BilateralFilterStep,
    CLAHEStep,
    ContrastStretchStep,
    GammaStep,
    GaussianBlurStep,
    GrayscaleStep,
    InvertStep,
    MedianBlurStep,
    MorphologyStep,
    PreprocessConfig,
    ResizeStep,
    SharpenStep,
    ThresholdStep,
    TophatStep,
    RemoveHorizontalLinesStep,
)


@dataclass
class PreprocessResult:
    image: np.ndarray
    stages: list[tuple[str, np.ndarray]]
    scale_x: float
    scale_y: float


class ImagePreprocessor:
    """按照配置里的顺序处理图片。"""

    _INTERPOLATIONS = {
        "nearest": cv2.INTER_NEAREST,
        "linear": cv2.INTER_LINEAR,
        "cubic": cv2.INTER_CUBIC,
        "area": cv2.INTER_AREA,
        "lanczos4": cv2.INTER_LANCZOS4,
    }

    def __init__(self, config: PreprocessConfig):
        self.config = config

    @staticmethod
    def _to_uint8(image: np.ndarray) -> np.ndarray:
        if image.dtype == np.uint8:
            return image.copy()
        min_value = float(np.min(image))
        max_value = float(np.max(image))
        if max_value <= min_value:
            return np.zeros(image.shape, dtype=np.uint8)
        scaled = (image.astype(np.float32) - min_value) * (255.0 / (max_value - min_value))
        return np.clip(scaled, 0, 255).astype(np.uint8)

    @staticmethod
    def _as_gray(image: np.ndarray) -> np.ndarray:
        if image.ndim == 2:
            return image
        if image.ndim == 3 and image.shape[2] == 1:
            return image[:, :, 0]
        if image.ndim == 3 and image.shape[2] == 3:
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if image.ndim == 3 and image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        raise ValueError(f"不支持的图像形状: {image.shape}")

    @staticmethod
    def _as_bgr(image: np.ndarray) -> np.ndarray:
        if image.ndim == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.ndim == 3 and image.shape[2] == 1:
            return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
        if image.ndim == 3 and image.shape[2] == 3:
            return image.copy()
        if image.ndim == 3 and image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        raise ValueError(f"不支持的图像形状: {image.shape}")

    def _apply_luminance(self, image: np.ndarray, func) -> np.ndarray:
        if image.ndim == 2:
            return func(image)
        bgr = self._as_bgr(image)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        l_channel = func(l_channel)
        return cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2BGR)

    @staticmethod
    def _contrast_stretch_gray(gray: np.ndarray, low_percentile: float, high_percentile: float) -> np.ndarray:
        low, high = np.percentile(gray, [low_percentile, high_percentile])
        if high <= low:
            return gray.copy()
        scaled = (gray.astype(np.float32) - float(low)) * (255.0 / float(high - low))
        return np.clip(scaled, 0, 255).astype(np.uint8)

    def _apply_step(self, work: np.ndarray, step) -> np.ndarray:
        if isinstance(step, GrayscaleStep):
            return self._as_gray(work)

        if isinstance(step, ResizeStep):
            return cv2.resize(
                work,
                None,
                fx=step.scale,
                fy=step.scale,
                interpolation=self._INTERPOLATIONS[step.interpolation],
            )

        if isinstance(step, GammaStep):
            table = np.clip(
                np.power(np.arange(256, dtype=np.float32) / 255.0, step.gamma) * 255.0,
                0,
                255,
            ).astype(np.uint8)
            return cv2.LUT(work, table)

        if isinstance(step, GaussianBlurStep):
            return cv2.GaussianBlur(work, (step.kernel_size, step.kernel_size), step.sigma)

        if isinstance(step, MedianBlurStep):
            return cv2.medianBlur(work, step.kernel_size)

        if isinstance(step, BilateralFilterStep):
            return cv2.bilateralFilter(work, step.diameter, step.sigma_color, step.sigma_space)

        if isinstance(step, ContrastStretchStep):
            return self._apply_luminance(
                work,
                lambda gray: self._contrast_stretch_gray(
                    gray, step.low_percentile, step.high_percentile
                ),
            )

        if isinstance(step, CLAHEStep):
            clahe = cv2.createCLAHE(
                clipLimit=step.clip_limit,
                tileGridSize=(step.tile_grid_size, step.tile_grid_size),
            )
            return self._apply_luminance(work, clahe.apply)

        if isinstance(step, SharpenStep):
            if step.strength <= 0:
                return work.copy()
            blur = cv2.GaussianBlur(work, (0, 0), step.sigma)
            return cv2.addWeighted(work, 1.0 + step.strength, blur, -step.strength, 0)

        if isinstance(step, ThresholdStep):
            gray = self._as_gray(work)
            threshold_type = cv2.THRESH_BINARY_INV if step.invert else cv2.THRESH_BINARY
            if step.method == "fixed":
                _, result = cv2.threshold(gray, step.value, 255, threshold_type)
                return result
            if step.method == "otsu":
                _, result = cv2.threshold(gray, 0, 255, threshold_type | cv2.THRESH_OTSU)
                return result
            return cv2.adaptiveThreshold(
                gray,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                threshold_type,
                step.adaptive_block_size,
                step.adaptive_c,
            )

        if isinstance(step, TophatStep):
            gray = self._as_gray(work)

            shapes = {
                "rect": cv2.MORPH_RECT,
                "ellipse": cv2.MORPH_ELLIPSE,
                "cross": cv2.MORPH_CROSS,
            }

            kernel = cv2.getStructuringElement(
                shapes[step.shape],
                (step.kernel_size, step.kernel_size),
            )

            return cv2.morphologyEx(
                gray,
                cv2.MORPH_TOPHAT,
                kernel,
            )
        if isinstance(step, RemoveHorizontalLinesStep):
            gray = self._as_gray(work)
            line_kernel = cv2.getStructuringElement(
                cv2.MORPH_RECT,
                (step.line_length, step.line_height),
            )
            horizontal = cv2.morphologyEx(
                gray,
                cv2.MORPH_OPEN,
                line_kernel,
            )

            if step.vertical_dilate > 1:
                dilate_kernel = cv2.getStructuringElement(
                    cv2.MORPH_RECT,
                    (1, step.vertical_dilate),
                )
                horizontal = cv2.dilate(horizontal, dilate_kernel, iterations=1)

            if step.strength != 1.0:
                horizontal = np.clip(
                    horizontal.astype(np.float32) * step.strength,
                    0,
                    255,
                ).astype(np.uint8)

            return cv2.subtract(gray, horizontal)
        if isinstance(step, MorphologyStep):
            kernel = cv2.getStructuringElement(
                cv2.MORPH_RECT, (step.kernel_size, step.kernel_size)
            )
            operation = cv2.MORPH_OPEN if step.operation == "open" else cv2.MORPH_CLOSE
            return cv2.morphologyEx(work, operation, kernel, iterations=step.iterations)

        if isinstance(step, InvertStep):
            return cv2.bitwise_not(work)

        raise TypeError(f"不支持的预处理步骤: {type(step).__name__}")

    def process(self, image: np.ndarray) -> PreprocessResult:
        if image is None or image.size == 0:
            raise ValueError("预处理收到空图像")

        original_h, original_w = image.shape[:2]
        work = self._to_uint8(image)

        if not self.config.enabled:
            return PreprocessResult(
                image=self._as_bgr(work),
                stages=[],
                scale_x=1.0,
                scale_y=1.0,
            )

        stages: list[tuple[str, np.ndarray]] = []
        for step in self.config.steps:
            if not step.enabled:
                continue
            work = self._apply_step(work, step)
            stages.append((step.type, work.copy()))

        final_h, final_w = work.shape[:2]
        return PreprocessResult(
            image=self._as_bgr(work),
            stages=stages,
            scale_x=final_w / original_w,
            scale_y=final_h / original_h,
        )
