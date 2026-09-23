"""验证整轮 OCR 缺少最终文字或证据图片时记录复核并结束本轮。"""

import pytest

from camera.hikrobot_sdk import CameraFrame
from models import OCRResult
from text_recognition import TextRecognizer


@pytest.mark.parametrize(
    ("result", "error_code", "warning_text"),
    [
        (
            OCRResult((), (), (), ()),
            "OCR_NO_TEXT",
            "本轮没有最终文字，人工复核 session_id=session",
        ),
        (
            OCRResult(("123",), ("123",), (), (("capture-1",),)),
            "OCR_NO_SELECTED_IMAGES",
            "本轮没有选中图片，人工复核 session_id=session",
        ),
    ],
)
def test_process_session_frames_rejects_empty_final_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    result: OCRResult,
    error_code: str,
    warning_text: str,
) -> None:
    """空结果保留人工复核日志并交付原有的本轮失败码。

    Args:
        monkeypatch: 替换 OCR 模型和终选函数。
        caplog: 记录人工复核日志。
        result: 模拟的终选结果。
        error_code: 预期的本轮失败码。
        warning_text: 预期的人工复核日志。

    Returns:
        返回示例：
            None  # 失败码和人工复核日志已通过断言验证
    """
    # 准备一帧相机图片和模型识别结果。
    frame = CameraFrame("camera", 1, 0, 0, 1.0, 1, 1, 0, 0, b"data")
    recognizer = TextRecognizer()
    monkeypatch.setattr(recognizer, "recognize_images", lambda images: [{"blocks": []}])
    monkeypatch.setattr(
        recognizer,
        "generate_final_text_and_images",
        lambda frame_results, frames: result,
    )

    # 检查本轮停止处理并记录对应的人工复核日志。
    with pytest.raises(ValueError, match=f"^{error_code}$"):
        recognizer.process_session_frames(
            "session", "capture", "camera", (frame,), lambda image: b"BM"
        )
    assert warning_text in caplog.text
