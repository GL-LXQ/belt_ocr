from __future__ import annotations

import json
import threading
from pathlib import Path
import cv2
import numpy as np

from .config import AppConfig
from .grouping import group_lines_into_blocks
from .preprocess import ImagePreprocessor
from .types import OCRLine


class BeltOCREngine:
    def __init__(self, config: AppConfig, backend=None):
        self.config = config
        if backend is None:
            from .backends import PaddleOCRBackend
            backend = PaddleOCRBackend(config.ocr)
        self.backend = backend
        self.preprocessor = ImagePreprocessor(config.preprocess)
        self._lock = threading.Lock()

    def _validate_input_path(self, image_path: str) -> Path:
        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"图片不存在: {path}")

        roots = self.config.security.allowed_roots
        if roots:
            resolved_roots = [Path(r).expanduser().resolve() for r in roots]
            if not any(path == root or root in path.parents for root in resolved_roots):
                raise PermissionError(f"图片路径不在 allowed_roots 中: {path}")
        return path

    def _resolve_roi(self, image: np.ndarray) -> tuple[np.ndarray, int, int]:
        roi = self.config.roi
        h_img, w_img = image.shape[:2]
        if not roi.enabled:
            return image, 0, 0

        x, y, w, h = roi.x, roi.y, roi.w, roi.h
        if roi.reference_width and roi.reference_height:
            if (w_img, h_img) != (roi.reference_width, roi.reference_height):
                if not roi.scale_if_size_changes:
                    raise ValueError(
                        f"图像尺寸 {w_img}x{h_img} 与 ROI 参考尺寸 "
                        f"{roi.reference_width}x{roi.reference_height} 不一致"
                    )
                sx = w_img / roi.reference_width
                sy = h_img / roi.reference_height
                x, y = round(x * sx), round(y * sy)
                w, h = round(w * sx), round(h * sy)

        x = max(0, x)
        y = max(0, y)
        x2 = min(w_img, x + w)
        y2 = min(h_img, y + h)
        if x2 <= x or y2 <= y:
            raise ValueError("ROI 超出图像范围或面积为 0")
        return image[y:y2, x:x2], x, y

    def _resize_to_reference(
        self, image: np.ndarray
    ) -> tuple[np.ndarray, float, float]:
        roi = self.config.roi
        if not roi.enabled or not roi.resize_to_reference:
            return image, 1.0, 1.0

        reference_width = roi.reference_width
        reference_height = roi.reference_height
        if reference_width is None or reference_height is None:
            raise ValueError("自动缩放需要配置 ROI 参考尺寸")

        image_height, image_width = image.shape[:2]
        if (image_width, image_height) == (reference_width, reference_height):
            return image, 1.0, 1.0

        resized = cv2.resize(
            image,
            (reference_width, reference_height),
            interpolation=cv2.INTER_LINEAR,
        )
        return (
            resized,
            image_width / reference_width,
            image_height / reference_height,
        )

    def _touches_edge(self, bbox: list[int], roi_width: int, roi_height: int) -> bool:
        f = self.config.filters
        m = max(0, f.edge_margin_px)
        sides = set(f.edge_sides)
        x1, y1, x2, y2 = bbox
        return (
            ("left" in sides and x1 <= m)
            or ("right" in sides and x2 >= roi_width - 1 - m)
            or ("top" in sides and y1 <= m)
            or ("bottom" in sides and y2 >= roi_height - 1 - m)
        )

    @staticmethod
    def _restore_bbox_scale(
        bbox: list[int],
        scale_x: float,
        scale_y: float,
        roi_width: int,
        roi_height: int,
    ) -> list[int]:
        if scale_x <= 0 or scale_y <= 0:
            raise ValueError("预处理缩放比例必须大于 0")
        x1, y1, x2, y2 = bbox
        restored = [
            round(x1 / scale_x),
            round(y1 / scale_y),
            round(x2 / scale_x),
            round(y2 / scale_y),
        ]
        restored[0] = min(max(restored[0], 0), roi_width)
        restored[1] = min(max(restored[1], 0), roi_height)
        restored[2] = min(max(restored[2], 0), roi_width)
        restored[3] = min(max(restored[3], 0), roi_height)
        return restored

    def _postprocess_lines(
        self,
        raw: list[OCRLine],
        roi_width: int,
        roi_height: int,
        off_x: int,
        off_y: int,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        output_scale_x: float = 1.0,
        output_scale_y: float = 1.0,
    ) -> list[OCRLine]:
        f = self.config.filters
        out: list[OCRLine] = []
        for line in raw:
            text = line.text.strip() if f.strip_text else line.text
            if not text:
                continue
            if line.confidence < f.min_confidence:
                continue

            bbox = self._restore_bbox_scale(
                line.bbox,
                scale_x=scale_x,
                scale_y=scale_y,
                roi_width=roi_width,
                roi_height=roi_height,
            )
            if f.discard_edge_lines and self._touches_edge(bbox, roi_width, roi_height):
                continue

            x1, y1, x2, y2 = bbox
            out.append(
                OCRLine(
                    text=text,
                    bbox=[
                        round((x1 + off_x) * output_scale_x),
                        round((y1 + off_y) * output_scale_y),
                        round((x2 + off_x) * output_scale_x),
                        round((y2 + off_y) * output_scale_y),
                    ],
                    confidence=float(line.confidence),
                    detection_confidence=line.detection_confidence,
                )
            )
        return out

    def _save_debug_images(self, path: Path, roi_img: np.ndarray, stages: list[tuple[str, np.ndarray]]) -> None:
        debug_dir = Path(self.config.preprocess.debug.output_dir).expanduser()
        debug_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(debug_dir / f"{path.stem}_00_roi_original.png"), roi_img)
        for index, (name, stage_image) in enumerate(stages, start=1):
            cv2.imwrite(str(debug_dir / f"{path.stem}_{index:02d}_{name}.png"), stage_image)

    def process(self, image_path: str) -> dict:
        path = self._validate_input_path(image_path)
        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"OpenCV 无法读取图片: {path}")
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        elif image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)

        roi_source, output_scale_x, output_scale_y = self._resize_to_reference(image)
        roi_img, off_x, off_y = self._resolve_roi(roi_source)
        processed = self.preprocessor.process(roi_img)
        ocr_img = processed.image

        if self.config.preprocess.debug.save_images:
            self._save_debug_images(path, roi_img, processed.stages)

        # 大家共用同一个 GPU 模型，排队执行能避免同时抢显存。
        with self._lock:
            raw_lines = self.backend.predict(ocr_img)

        h, w = roi_img.shape[:2]
        lines = self._postprocess_lines(
            raw_lines,
            w,
            h,
            off_x,
            off_y,
            scale_x=processed.scale_x,
            scale_y=processed.scale_y,
            output_scale_x=output_scale_x,
            output_scale_y=output_scale_y,
        )
        g = self.config.grouping
        blocks = group_lines_into_blocks(
            lines,
            horizontal_overlap_ratio=g.horizontal_overlap_ratio,
            center_x_tolerance_ratio=g.center_x_tolerance_ratio,
            max_vertical_gap_ratio=g.max_vertical_gap_ratio,
            min_lines_per_block=g.min_lines_per_block,
        )

        result = {
            "image_path": str(path),
            "blocks": [
                {
                    "bbox": block.bbox,
                    "lines": [
                        {
                            "text": line.text,
                            "bbox": line.bbox,
                            "confidence": float(line.confidence),
                        }
                        for line in block.lines
                    ],
                }
                for block in blocks
            ],
        }

        if self.config.output.save_json:
            out_dir = Path(self.config.output.output_dir).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{path.stem}.json"
            out_path.write_text(
                json.dumps(
                    result,
                    ensure_ascii=self.config.output.ensure_ascii,
                    indent=self.config.output.indent,
                ),
                encoding="utf-8",
            )
        return result
