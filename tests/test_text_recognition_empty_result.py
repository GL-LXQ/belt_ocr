"""验证整轮 OCR 缺少最终文字或证据图片时记录复核并结束本轮。"""

import pytest

from camera.hikrobot_sdk import CameraFrame
from models import CapturedFrame, OCRResult
from text_recognition import TextRecognizer


def test_process_session_frames_passes_camera_frames_to_recognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """将完整相机帧交给识别流程并保留在终选图片中。

    Args:
        monkeypatch: 替换 OCR 模型和终选函数。

    Returns:
        返回示例：
            None  # 相机帧身份和元数据已通过断言验证
    """
    # 准备一帧没有 BMP 文件头的原始图像。
    frame = CameraFrame("camera", 1, 0, 0, 1.0, 1, 1, 0, 0, b"\x01\x02")
    recognizer = TextRecognizer()
    received_frames = []

    # 让识别和终选流程返回当前帧。
    def recognize_camera_frames(frames: list[CameraFrame]) -> list[dict]:
        """登记模型收到的相机帧并返回空文字块。

        Args:
            frames: 本轮传给模型的相机原始帧。

        Returns:
            返回示例：
                [
                    {
                        "blocks": [],  # 当前图片的文字块
                    },
                ]
        """
        received_frames.extend(frames)
        return [{"blocks": []}]

    def select_raw_frame(
        frame_results: list[dict],
        captured_frames: tuple[CapturedFrame, ...],
    ) -> OCRResult:
        """选取本轮第一张原始图片。

        Args:
            frame_results: 与图片对应的模型结果。
            captured_frames: 本轮传入终选的原始图片。

        Returns:
            返回示例：
                OCRResult(
                    ordered_lines=("123",),  # 最终文字
                    normalized_lines=("123",),  # 去空白文字
                    selected_frames=(captured_frames[0],),  # 选中图片
                    line_frame_ids=(("capture-1",),),  # 来源图片编号
                )
        """
        return OCRResult(("123",), ("123",), (captured_frames[0],), (("capture-1",),))

    monkeypatch.setattr(recognizer, "recognize_images", recognize_camera_frames)
    monkeypatch.setattr(recognizer, "generate_final_text_and_images", select_raw_frame)

    # 核对模型输入与终选图片都保留完整相机帧。
    result = recognizer.process_session_frames("session", "capture", "camera", (frame,))
    assert received_frames == [frame]
    assert result.selected_frames[0].camera_frame is frame


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
        recognizer.process_session_frames("session", "capture", "camera", (frame,))
    assert warning_text in caplog.text
