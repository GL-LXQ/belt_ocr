"""验证 OCR 主流程组装最终文字与证据图片。"""

import time
from unittest.mock import Mock

import pytest

from camera.hikrobot_sdk import CameraFrame
from models import OCRResult
from text_recognition import OCRProcessingError, TextRecognizer


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
    # 将单图文字放入模型结果。
    model_results = [{"blocks": [{"lines": model_lines}]}]
    return recognize_model_results(model_results)


def recognize_model_results(model_results: object) -> OCRResult:
    """将给定模型返回值送入整轮 OCR 主流程。

    Args:
        model_results: 整轮模型返回值。

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


def test_model_result_count_mismatch_raises_processing_error() -> None:
    """确认模型结果数量不一致时报告 OCR 处理失败。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 模型结果数量异常已核对
    """
    with pytest.raises(OCRProcessingError, match="数量与图片数量不一致"):
        recognize_model_results([])


@pytest.mark.parametrize("model_results", [
    None,
    {"blocks": []},
    [None],
    [{}],
    [{"blocks": None}],
    [{"blocks": [None]}],
    [{"blocks": [{}]}],
    [{"blocks": [{"lines": None}]}],
    [{"blocks": [{"lines": [None]}]}],
    [{"blocks": [{"lines": [{}]}]}],
    [{"blocks": [{"lines": [{"text": None}]}]}],
])
def test_malformed_model_result_raises_processing_error(model_results: object) -> None:
    """确认业务筛选必需的模型返回结构缺失时报告 OCR 处理失败。

    Args:
        model_results: 缺少必需结构的模型返回值。

    Returns:
        返回示例：
            None  # 模型返回结构异常已核对
    """
    with pytest.raises(OCRProcessingError):
        recognize_model_results(model_results)


@pytest.mark.parametrize("model_line", [
    {"text": "12345678901234567890"},
    {"text": "123", "confidence": "0.95"},
    {"text": "12", "confidence": float("nan")},
    {"text": "1234567A", "confidence": float("inf")},
    {"text": "1234567A", "confidence": True},
])
def test_compared_candidate_requires_usable_confidence(model_line: dict) -> None:
    """确认参与置信度比较的候选必须提供有限数值。

    Args:
        model_line: 格式有效但置信度无效的文字行。

    Returns:
        返回示例：
            None  # 置信度异常已核对
    """
    with pytest.raises(OCRProcessingError, match="置信度不是有限数值"):
        recognize_model_lines([model_line])


@pytest.mark.parametrize("model_line,review_reason", [
    ({"text": "ABC"}, "没有格式正确的 3 位文字"),
    ({"text": "AB", "confidence": "bad"}, "没有格式正确的 2 位文字"),
    ({"text": "ABCDEFGH", "confidence": float("nan")}, "没有可靠的 8 位文字"),
])
def test_filtered_candidate_ignores_unusable_confidence(
    model_line: dict, review_reason: str
) -> None:
    """确认格式过滤掉的候选不会触发置信度异常。

    Args:
        model_line: 格式不符且置信度异常的文字行。
        review_reason: 对应类别的人工复核原因。

    Returns:
        返回示例：
            None  # 格式复核原因已核对
    """
    result = recognize_model_lines([model_line])

    # 核对模型结构有效但业务格式不符时继续人工复核。
    assert isinstance(result, OCRResult)
    assert review_reason in result.review_reason


def test_empty_blocks_require_review() -> None:
    """确认没有文字块的模型结果仍进入人工复核。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 空文字块的复核结果已核对
    """
    result = recognize_model_results([{"blocks": []}])

    # 核对空文字块保留原始图片并标记复核。
    assert isinstance(result, OCRResult)
    assert len(result.review_frames) == 1
    assert "没有最终文字" in result.review_reason
