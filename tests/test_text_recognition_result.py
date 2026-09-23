"""验证 OCR 主流程组装最终文字与证据图片。"""

import time
from unittest.mock import Mock

import pytest

from camera.hikrobot_sdk import CameraFrame
from models import OCRResult
from text_recognition import TextRecognizer


RELIABLE_MODEL_LINES = (
    {"text": "12345678901234567890", "confidence": 0.95},
    {"text": "1234567A", "confidence": 0.95},
    {"text": "123", "confidence": 0.95},
    {"text": "12", "confidence": 0.95},
)


def recognize_model_lines(model_lines: list[dict]) -> OCRResult:
    """将给定模型文字送入整轮 OCR 主流程。

    Args:
        model_lines: 一张图片中模型返回的文字及置信度。

    Returns:
        返回示例：
            OCRResult(
                ordered_lines=("123",),  # 最终文字
                normalized_lines=("123",),  # 去空白后的文字
                selected_frames=(),  # 选中的证据图片
                line_frame_ids=(),  # 文字对应的图片编号
                review_frames=(),  # 待复核的原始图片
                review_reason=None,  # 待复核原因
            )
    """
    # 创建一张带完整元数据的原始帧。
    camera_frame = CameraFrame(
        camera_serial="camera-1",
        frame_number=1,
        device_timestamp=1,
        host_timestamp=1,
        received_monotonic=time.monotonic(),
        width=2,
        height=1,
        pixel_type=17301505,
        lost_packet_count=0,
        data=b"image",
    )

    # 用指定模型结果运行整轮 OCR。
    recognizer = TextRecognizer()
    model_results = [{"blocks": [{"lines": model_lines}]}]
    recognizer.recognize_images = Mock(return_value=model_results)
    return recognizer.process_session_frames(
        "session-1", "capture-1", "camera-1", (camera_frame,)
    )


def test_all_reliable_categories_need_no_review() -> None:
    """确认四类可靠文字都进入最终结果且无需复核。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 四类文字与正常状态已核对
    """
    result = recognize_model_lines(list(RELIABLE_MODEL_LINES))

    # 核对最终文字和证据图片。
    assert result.ordered_lines == tuple(line["text"] for line in RELIABLE_MODEL_LINES)
    assert result.normalized_lines == result.ordered_lines
    assert result.line_frame_ids == (("capture-1-1",),) * 4
    assert len(result.selected_frames) == 1

    # 核对无需复核的结果。
    assert result.review_frames == ()
    assert result.review_reason is None


def test_reliable_text_preserves_original_spacing() -> None:
    """确认可靠文字保留原始空白并标记缺失类别。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 原始文字、去空白文字和复核原因已核对
    """
    result = recognize_model_lines([{"text": " 123 ", "confidence": 0.95}])

    # 核对文字和对应证据图片。
    assert result.ordered_lines == (" 123 ",)
    assert result.normalized_lines == ("123",)
    assert result.line_frame_ids == (("capture-1-1",),)
    selected_frame = result.selected_frames[0]
    review_frame = result.review_frames[0]
    assert selected_frame.camera_frame is review_frame.camera_frame

    # 核对缺失类别已进入复核原因。
    assert "没有可靠的 20 位文字" in result.review_reason


@pytest.mark.parametrize("missing_text,character_length", [
    ("12345678901234567890", 20),
    ("1234567A", 8),
    ("123", 3),
    ("12", 2),
])
def test_missing_category_preserves_reliable_text(
    missing_text: str, character_length: int
) -> None:
    """确认缺少任一类别时保留其他文字并登记复核原因。

    Args:
        missing_text: 从模型结果中移除的文字。
        character_length: 被移除文字所属的类别长度。

    Returns:
        返回示例：
            None  # 其余文字和对应复核原因已核对
    """
    model_lines = [
        line for line in RELIABLE_MODEL_LINES if line["text"] != missing_text
    ]
    result = recognize_model_lines(model_lines)

    # 核对其余可靠文字仍进入最终结果。
    assert result.ordered_lines == tuple(line["text"] for line in model_lines)
    assert result.selected_frames

    # 核对缺少类别的原因和待复核图片。
    assert f"没有可靠的 {character_length} 位文字" in result.review_reason
    assert len(result.review_frames) == 1


@pytest.mark.parametrize("valid_text,invalid_text,character_length", [
    ("123", "ABC", 3),
    ("12", "AB", 2),
])
def test_invalid_numeric_format_requires_review(
    valid_text: str, invalid_text: str, character_length: int
) -> None:
    """确认数字类别格式过滤后为空时登记复核原因。

    Args:
        valid_text: 被替换的可靠数字文字。
        invalid_text: 相同长度但格式不符的文字。
        character_length: 当前数字类别长度。

    Returns:
        返回示例：
            None  # 格式复核原因及其余文字已核对
    """
    model_lines = [
        {"text": invalid_text, "confidence": line["confidence"]}
        if line["text"] == valid_text else line
        for line in RELIABLE_MODEL_LINES
    ]
    result = recognize_model_lines(model_lines)

    # 核对格式不符的文字没有进入最终结果。
    assert invalid_text not in result.ordered_lines
    assert len(result.ordered_lines) == 3

    # 核对该类别的格式复核原因。
    assert f"没有格式正确的 {character_length} 位文字" in result.review_reason


def test_low_confidence_requires_review() -> None:
    """确认最高置信度不足时登记原因并保留其他文字。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 低置信度原因及其余文字已核对
    """
    model_lines = [
        {"text": line["text"], "confidence": 0.79}
        if line["text"] == "12345678901234567890" else line
        for line in RELIABLE_MODEL_LINES
    ]
    result = recognize_model_lines(model_lines)

    # 核对不可靠文字被排除，其余文字保留。
    assert result.ordered_lines == ("1234567A", "123", "12")

    # 核对最高置信度不足的原因。
    assert "20 位文字最高置信度不足" in result.review_reason


def test_multiple_missing_categories_keep_all_review_reasons() -> None:
    """确认多个类别同时缺失时保留全部复核原因。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 两个复核原因和其余文字已核对
    """
    result = recognize_model_lines(list(RELIABLE_MODEL_LINES[2:]))

    # 核对剩余可靠文字。
    assert result.ordered_lines == ("123", "12")

    # 核对两个类别的复核原因。
    assert "没有可靠的 20 位文字" in result.review_reason
    assert "没有可靠的 8 位文字" in result.review_reason


def test_no_final_text_combines_review_reasons() -> None:
    """确认完全没有文字时保留类别原因及整轮原因。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 类别原因和没有最终文字的原因已核对
    """
    result = recognize_model_lines([])

    # 核对无文字时的证据图片和复核原因。
    assert result.ordered_lines == ()
    assert len(result.review_frames) == 1
    assert "没有可靠的 20 位文字" in result.review_reason
    assert "没有可靠的 8 位文字" in result.review_reason
    assert "没有最终文字" in result.review_reason
