"""验证整轮 OCR 的阶段顺序、数据契约和黑盒失败边界。"""

import time
from unittest.mock import Mock

import pytest

from models import OCRResult
from mvs_sdk import CameraFrame
from text_recognition import TextRecognizer


@pytest.fixture
def recognition_inputs():
    """创建原始帧和无设备依赖的编码接口。

    Args:
        无外部参数。

    Returns:
        (frames, encoder)  # 独立帧集合与图片编码替身
    """
    frames = tuple(
        CameraFrame("serial", number, 0, 0, time.monotonic(), 2, 2, 0, 0, b"pixels")
        for number in range(1, 4)
    )
    return (
        frames,
        Mock(return_value=b"BM-image"),
    )


def test_processing_order_and_image_identity(recognition_inputs):
    """验证编码、筛选、识别和终选顺序以及跨帧对应关系。

    Args:
        recognition_inputs: 原始帧和编码替身。

    Returns:
        None  # 顺序、筛选和最终来源对应关系已验证
    """
    frames, encoder = recognition_inputs
    recognizer = TextRecognizer()
    calls = []

    def filter_frames(images):
        """检查编码已全部完成并排除首帧。

        Args:
            images: 已编码图片。

        Returns:
            images[1:]  # 保留后两帧
        """
        assert encoder.call_count == 3
        calls.append("filter")
        return images[1:]

    def recognize_images(images):
        """核对仅识别合格图片。

        Args:
            images: 合格图片字节。

        Returns:
            [{"blocks": []}, {"blocks": []}]  # 两张图片的测试模型结果
        """
        assert images == [b"BM-image", b"BM-image"]
        calls.append("recognize")
        return [{"blocks": []}, {"blocks": []}]

    def select_result(results, images):
        """检查帧身份并生成一条文字对应两张图片。

        Args:
            results: 逐图识别结果。
            images: 合格内存图片。

        Returns:
            OCRResult  # 一条文字、两张图片及其来源编号
        """
        calls.append("select")
        assert [result["frame_id"] for result in results] == ["capture-2", "capture-3"]
        assert all(image.session_id == "session" for image in images)
        return OCRResult(("ABC",), images, (("capture-2", "capture-3"),))

    # 替换三个算法边界，执行真实顺序主流程。
    recognizer.filter_qualified_frames = filter_frames
    recognizer.recognize_images = recognize_images
    recognizer.generate_final_text_and_images = select_result
    result = recognizer.process_session_frames("session", "capture", "camera", frames, encoder)
    assert calls == ["filter", "recognize", "select"]
    assert result.ordered_lines == ("ABC",)
    assert len(result.selected_frames) == 2
    assert result.line_frame_ids == (("capture-2", "capture-3"),)


@pytest.mark.parametrize(
    "failure",
    ["no_frames", "no_qualified", "model", "selection", "no_text", "no_images", "count"],
)
def test_processing_failure_boundaries(recognition_inputs, failure):
    """验证空数据、未实现算法和模型输出异常均不会产生成功结果。

    Args:
        recognition_inputs: 原始帧与编码接口。
        failure: 要注入的失败阶段。

    Returns:
        None  # 每种失败均抛出明确异常
    """
    frames, encoder = recognition_inputs
    recognizer = TextRecognizer()
    if failure == "no_frames":
        frames = ()
    if failure == "no_qualified":
        recognizer.filter_qualified_frames = lambda images: ()
    if failure not in {"no_frames", "no_qualified", "model"}:
        recognizer.recognize_images = lambda images: [{"blocks": []} for image in images]
    if failure == "count":
        recognizer.recognize_images = lambda images: []
    if failure in {"no_text", "no_images"}:
        recognizer.generate_final_text_and_images = lambda results, images: OCRResult(
            ordered_lines=() if failure == "no_text" else ("ABC",),
            selected_frames=() if failure == "no_images" else images,
            line_frame_ids=(),
        )
    with pytest.raises((ValueError, NotImplementedError)):
        recognizer.process_session_frames("session", "capture", "camera", frames, encoder)
    if failure == "no_frames":
        encoder.assert_not_called()
