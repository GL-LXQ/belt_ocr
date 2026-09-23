"""验证 OCR 主流程组装最终文字与证据图片。"""

import time
from unittest.mock import Mock

from camera.hikrobot_sdk import CameraFrame
from text_recognition import TextRecognizer


def test_process_session_frames_builds_ocr_result() -> None:
    """确认主流程根据终选四项结果组装 OCRResult。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 最终文字及其证据图片已核对
    """
    # 准备一张带有完整相机元数据的原始帧。
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

    # 用固定模型结果运行整轮 OCR 主流程。
    recognizer = TextRecognizer()
    model_line = {
        "text": " 123 ",
        "confidence": 0.95,
    }
    recognizer.recognize_images = Mock(
        return_value=[{"blocks": [{"lines": [model_line]}]}]
    )
    result = recognizer.process_session_frames(
        "session-1", "capture-1", "camera-1", (camera_frame,)
    )

    # 核对文字、来源图片和正常结果标记。
    assert result.ordered_lines == (" 123 ",)
    assert result.normalized_lines == ("123",)
    assert result.selected_frames[0].camera_frame is camera_frame
    assert result.line_frame_ids == (("capture-1-1",),)
    assert result.review_frames == ()
    assert result.review_reason is None
