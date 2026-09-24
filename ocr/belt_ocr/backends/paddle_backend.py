from __future__ import annotations

from typing import Any

import numpy as np

from ..config import OCRConfig
from ..types import OCRLine


class PaddleOCRBackend:
    """对 PaddleOCR 做一层简单封装，模型启动时加载一次，后面重复使用。"""

    def __init__(self, cfg: OCRConfig):
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "未安装 PaddleOCR/PaddlePaddle。请先按 README 安装 GPU 版 PaddlePaddle 和 paddleocr。"
            ) from exc

        kwargs: dict[str, Any] = {
            "text_detection_model_name": cfg.detection_model,
            "text_recognition_model_name": cfg.recognition_model,
            "text_recognition_batch_size": cfg.recognition_batch_size,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": cfg.device,
            "enable_hpi": cfg.enable_hpi,
        }
        if cfg.detection_model_dir:
            kwargs["text_detection_model_dir"] = cfg.detection_model_dir
        if cfg.recognition_model_dir:
            kwargs["text_recognition_model_dir"] = cfg.recognition_model_dir
        if cfg.engine:
            kwargs["engine"] = cfg.engine
        if cfg.text_det_limit_side_len is not None:
            kwargs["text_det_limit_side_len"] = cfg.text_det_limit_side_len
        if cfg.text_det_limit_type is not None:
            kwargs["text_det_limit_type"] = cfg.text_det_limit_type
        if cfg.text_det_thresh is not None:
            kwargs["text_det_thresh"] = cfg.text_det_thresh
        if cfg.text_det_box_thresh is not None:
            kwargs["text_det_box_thresh"] = cfg.text_det_box_thresh
        if cfg.text_det_unclip_ratio is not None:
            kwargs["text_det_unclip_ratio"] = cfg.text_det_unclip_ratio

        self._ocr = PaddleOCR(**kwargs)

    @staticmethod
    def _payload_from_result(result: Any) -> dict:
        data = getattr(result, "json", None)
        if callable(data):
            data = data()
        if data is None and isinstance(result, dict):
            data = result
        if data is None:
            try:
                data = dict(result)
            except Exception as exc:
                raise RuntimeError("无法解析 PaddleOCR Result 对象") from exc
        if "res" in data and isinstance(data["res"], dict):
            data = data["res"]
        return data

    @staticmethod
    def _box_from_poly(poly) -> list[int]:
        arr = np.asarray(poly)
        return [
            int(np.min(arr[:, 0])),
            int(np.min(arr[:, 1])),
            int(np.max(arr[:, 0])),
            int(np.max(arr[:, 1])),
        ]

    def predict(self, image: np.ndarray) -> list[OCRLine]:
        result_list = self._ocr.predict(image)
        if not result_list:
            return []

        payload = self._payload_from_result(result_list[0])
        texts = list(payload.get("rec_texts", []))
        scores = list(payload.get("rec_scores", []))
        boxes = payload.get("rec_boxes")
        polys = payload.get("rec_polys")

        lines: list[OCRLine] = []
        for i, text in enumerate(texts):
            score = float(scores[i]) if i < len(scores) else 0.0
            if boxes is not None and i < len(boxes):
                bbox = [int(v) for v in np.asarray(boxes[i]).tolist()]
            elif polys is not None and i < len(polys):
                bbox = self._box_from_poly(polys[i])
            else:
                continue
            lines.append(OCRLine(text=str(text), bbox=bbox, confidence=score))
        return lines
