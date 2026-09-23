"""验证短数字格式与各类文字统一大写后的终选结果。"""

import pytest

from models import CapturedFrame
from text_recognition import TextRecognizer


@pytest.mark.parametrize(
    ("unicode_text", "ascii_text"),
    [
        ("٠٠٣", "003"),
        ("٢١", "21"),
    ],
)
def test_short_number_rejects_unicode_digits(
    unicode_text: str,
    ascii_text: str,
) -> None:
    """高置信度的非 ASCII 数字不覆盖有效的普通数字。

    Args:
        unicode_text: 非 ASCII 数字候选。
        ascii_text: 同长度的普通数字候选。

    Returns:
        返回示例：
            None  # 候选筛选结果已通过断言验证
    """
    # 建立包含两条候选的单张图片结果。
    frame = CapturedFrame(
        "session", "capture", "camera", "capture-1", "2026-09-23", 1.0, b"BM"
    )
    frame_results = [{
        "frame_id": frame.frame_id,
        "blocks": [{"lines": [
            {"text": unicode_text, "confidence": 0.99},
            {"text": ascii_text, "confidence": 0.80},
        ]}],
    }]

    # 检查最终结果采用普通数字及其来源图片。
    result = TextRecognizer().generate_final_text_and_images(frame_results, (frame,))
    assert result.ordered_lines == (ascii_text,)
    assert result.normalized_lines == (ascii_text,)
    assert result.line_frame_ids == ((frame.frame_id,),)
    assert result.selected_frames == (frame,)


def test_uppercases_selected_lines_and_groups_mixed_case_serials() -> None:
    """四类文字统一大写后按编号连号，并关联最终证据图片。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 大写、连号和证据图片已通过断言验证
    """
    # 建立两张图片及其识别出的四类候选。
    first_frame = CapturedFrame(
        "session", "capture", "camera", "capture-1", "2026-09-23", 1.0, b"BM1"
    )
    second_frame = CapturedFrame(
        "session", "capture", "camera", "capture-2", "2026-09-23", 2.0, b"BM2"
    )
    frame_results = [
        {
            "frame_id": first_frame.frame_id,
            "blocks": [{"lines": [
                {"text": "2378240 vega x 5epj1152", "confidence": 0.95},
                {"text": "2926215c", "confidence": 0.92},
            ]}],
        },
        {
            "frame_id": second_frame.frame_id,
            "blocks": [{"lines": [
                {"text": "2926215C", "confidence": 0.91},
                {"text": "2926216C", "confidence": 0.90},
                {"text": "1234567a", "confidence": 0.99},
                {"text": "7654321b", "confidence": 0.98},
                {"text": "0 03", "confidence": 0.90},
                {"text": "21", "confidence": 0.90},
            ]}],
        },
    ]

    # 检查混合大小写的编号合并为连号组，所有输出字段逐项对应。
    result = TextRecognizer().generate_final_text_and_images(
        frame_results, (first_frame, second_frame)
    )
    assert result.ordered_lines == (
        "2378240 VEGA X 5EPJ1152", "2926215C", "2926216C", "0 03", "21"
    )
    assert result.normalized_lines == (
        "2378240VEGAX5EPJ1152", "2926215C", "2926216C", "003", "21"
    )
    assert result.line_frame_ids == (
        (first_frame.frame_id,),
        (first_frame.frame_id,),
        (first_frame.frame_id,),
        (second_frame.frame_id,),
        (second_frame.frame_id,),
    )
    assert result.selected_frames == (first_frame, second_frame)
