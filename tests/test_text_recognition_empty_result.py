"""验证整轮 OCR 返回正常结果或带全部原始帧的待复核结果。"""

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
                    review_frames=(),  # 正常结果没有待复核图片
                    review_reason=None,  # 正常结果没有复核原因
                )
        """
        return OCRResult(("123",), ("123",), (captured_frames[0],), (("capture-1",),))

    monkeypatch.setattr(recognizer, "recognize_images", recognize_camera_frames)
    monkeypatch.setattr(recognizer, "generate_final_text_and_images", select_raw_frame)

    # 核对模型输入与终选图片都保留完整相机帧。
    result = recognizer.process_session_frames("session", "capture", "camera", (frame,))
    assert received_frames == [frame]
    assert result.selected_frames[0].camera_frame is frame
    assert result.review_frames == ()
    assert result.review_reason is None


@pytest.mark.parametrize(
    ("review_stage", "review_reason"),
    [
        ("filter", "初筛后没有合格图片"),
        ("model_count", "模型识别结果数量与图片数量不一致"),
        ("final_text", "没有最终文字"),
    ],
)
def test_process_session_frames_returns_all_frames_for_review(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    review_stage: str,
    review_reason: str,
) -> None:
    """筛帧、模型数量或终选失败时返回全部原始帧和复核原因。

    Args:
        monkeypatch: 替换 OCR 模型和终选函数。
        caplog: 记录人工复核日志。
        review_stage: 本例触发人工复核的处理阶段。
        review_reason: 预期的人工复核原因。

    Returns:
        返回示例：
            None  # 本轮全部原始帧与复核原因已通过断言验证
    """
    # 准备两帧原始图像，确保复核保留初筛前的全部图片。
    first_frame = CameraFrame("camera", 1, 0, 0, 1.0, 1, 1, 0, 0, b"first")
    second_frame = CameraFrame("camera", 2, 0, 0, 2.0, 1, 1, 0, 0, b"second")
    recognizer = TextRecognizer()

    # 按测试阶段设置筛帧、模型结果和最终文字。
    if review_stage == "filter":
        monkeypatch.setattr(recognizer, "filter_qualified_frames", lambda frames: ())
    else:
        monkeypatch.setattr(
            recognizer, "filter_qualified_frames", lambda frames: frames[:1]
        )
        image_results = [] if review_stage == "model_count" else [{"blocks": []}]
        monkeypatch.setattr(
            recognizer, "recognize_images", lambda frames: image_results
        )
        monkeypatch.setattr(
            recognizer,
            "generate_final_text_and_images",
            lambda frame_results, frames: OCRResult((), (), (), ()),
        )

    # 核对待复核结果只保留全部原始帧和原因。
    result = recognizer.process_session_frames(
        "session", "capture", "camera", (first_frame, second_frame)
    )
    assert result.ordered_lines == ()
    assert result.normalized_lines == ()
    assert result.selected_frames == ()
    assert result.line_frame_ids == ()
    assert tuple(frame.camera_frame for frame in result.review_frames) == (
        first_frame, second_frame
    )
    assert result.review_reason == review_reason
    if review_stage == "final_text":
        assert "本轮没有最终文字，人工复核 session_id=session" in caplog.text
