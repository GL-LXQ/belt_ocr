"""验证短数字候选只接受 ASCII 数字。"""

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
