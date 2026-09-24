"""验证 OCR Engine 的路径和内存图片入口。"""

import json
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from ocr.config import AppConfig, OutputConfig
from ocr.engine import BeltOCREngine
from ocr.types import OCRLine


@pytest.mark.parametrize("image_shape", [(30, 50), (30, 50, 3), (30, 50, 4)])
def test_path_and_memory_image_share_ocr_results(tmp_path, image_shape) -> None:
    """确认两种入口处理同一张图片时返回相同文字块。

    Args:
        tmp_path: pytest 提供的临时目录。
        image_shape: 测试图片的高、宽和可选通道数。

    Returns:
        返回示例：
            None  # 两种入口的结果与输出文件已核对
    """
    # 准备图片、输出目录和固定的 OCR 后端结果。
    image = np.full(image_shape, 128, dtype=np.uint8)
    image_path = tmp_path / "sample.png"
    output_directory = tmp_path / "output"
    assert cv2.imwrite(str(image_path), image)
    backend = Mock()
    backend.predict.return_value = [OCRLine("示例", [5, 5, 30, 15], 0.9)]
    config = AppConfig(output=OutputConfig(output_dir=str(output_directory)))
    engine = BeltOCREngine(config, backend=backend)

    # 先处理内存图片，确认不写 JSON。
    memory_result = engine.process_image(image)
    assert memory_result["image_path"] is None
    assert not output_directory.exists()

    # 再处理相同图片的路径，核对返回结果和原有 JSON 输出。
    path_result = engine.process(str(image_path))
    assert path_result["image_path"] == str(image_path.resolve())
    assert path_result["blocks"] == memory_result["blocks"]
    assert path_result["blocks"] == [
        {
            "bbox": [5, 5, 30, 15],
            "lines": [{"text": "示例", "bbox": [5, 5, 30, 15], "confidence": 0.9}],
        }
    ]
    output_path = output_directory / "sample.json"
    saved_result = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved_result == path_result

    # 核对两种入口交给 OCR 后端的图片一致。
    memory_image = backend.predict.call_args_list[0].args[0]
    path_image = backend.predict.call_args_list[1].args[0]
    np.testing.assert_array_equal(memory_image, path_image)
