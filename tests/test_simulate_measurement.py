"""验证开发模拟的图片输入和证据编码。"""

import threading
from pathlib import Path

import cv2
import numpy as np

from camera.hikrobot_sdk import PIXEL_TYPE_MONO8, convert_mono8_frame_to_array
from scripts.simulate_measurement import SimulatedCameraSdk, load_camera_frames


def test_bmp_and_jpg_are_loaded_as_mono8_frames(tmp_path: Path) -> None:
    """验证 BMP 和 JPG 都只把灰度原始像素交给相机帧。

    Args:
        tmp_path: pytest 提供的图片目录。

    Returns:
        返回示例：
            None  # 两种格式的原始像素、尺寸和帧编号已核对
    """
    # 在同一目录创建 BMP 和 JPG 测试图片。
    image = np.array(
        [[0, 40, 80, 120], [30, 90, 150, 210], [255, 200, 100, 50]],
        dtype=np.uint8,
    )
    bmp_path = tmp_path / "first.bmp"
    jpg_path = tmp_path / "second.jpg"
    assert cv2.imwrite(str(bmp_path), image)
    assert cv2.imwrite(str(jpg_path), image)

    # 按实际解码结果核对两张 Mono8 相机帧。
    frames = load_camera_frames(tmp_path, "camera-1")
    assert len(frames) == 2
    assert [frame.frame_number for frame in frames] == [1, 2]
    assert all(frame.pixel_type == PIXEL_TYPE_MONO8 for frame in frames)
    for frame, image_path in zip(frames, (bmp_path, jpg_path)):
        expected_image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        assert (frame.width, frame.height) == (4, 3)
        assert len(frame.data) == frame.width * frame.height
        np.testing.assert_array_equal(
            convert_mono8_frame_to_array(frame), expected_image
        )


def test_simulated_camera_delivers_frames_and_encodes_jpg(tmp_path: Path) -> None:
    """验证假 SDK 按顺序交付 Mono8 帧并编码有效 JPG。

    Args:
        tmp_path: pytest 提供的图片目录。

    Returns:
        返回示例：
            None  # 交付计数、结束状态和证据 JPG 已核对
    """
    # 准备两张编号不同的 BMP 输入帧。
    image = np.arange(80, dtype=np.uint8).reshape(8, 10)
    assert cv2.imwrite(str(tmp_path / "first.bmp"), image)
    assert cv2.imwrite(str(tmp_path / "second.bmp"), image)
    frames = load_camera_frames(tmp_path, "camera-1")
    camera_sdk = SimulatedCameraSdk("camera-1", frames)

    # 按顺序取出全部帧并确认无帧时返回 None。
    camera_sdk.start_grabbing()
    stop_requested = threading.Event()
    first_frame = camera_sdk.read_frame(stop_requested, 1)
    second_frame = camera_sdk.read_frame(stop_requested, 1)
    assert [first_frame.frame_number, second_frame.frame_number] == [1, 2]
    assert first_frame.received_monotonic > 0
    assert camera_sdk.read_frame(stop_requested, 1) is None
    assert camera_sdk.received_frame_count == 2
    camera_sdk.stop_grabbing()

    # 将已交付原始帧编码成可读取的 JPG 文件内容。
    jpg_data = camera_sdk.encode_image(first_frame)
    encoded_image = np.frombuffer(jpg_data, dtype=np.uint8)
    decoded_image = cv2.imdecode(encoded_image, cv2.IMREAD_GRAYSCALE)
    assert jpg_data.startswith(b"\xff\xd8")
    assert decoded_image.shape == (8, 10)
