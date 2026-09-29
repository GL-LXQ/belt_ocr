"""验证单帧无数据和相机采集异常的交付边界。"""

import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from camera.camera import Camera, CaptureTask
from camera.hikrobot_sdk import (
    PIXEL_TYPE_MONO8,
    CameraFrame,
    MvsError,
    convert_mono8_frame_to_array,
)
from enums import EventType


def test_mono8_frame_conversion_checks_pixels_and_size() -> None:
    """确认 Mono8 帧转成灰度图并拒绝错误格式或字节数。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 像素内容、尺寸和异常边界已核对
    """
    # 建立包含两行像素的 Mono8 原始帧。
    frame = CameraFrame(
        camera_serial="camera-1",
        frame_number=1,
        device_timestamp=1,
        host_timestamp=1,
        received_monotonic=1.0,
        width=3,
        height=2,
        pixel_type=PIXEL_TYPE_MONO8,
        lost_packet_count=0,
        image_bytes=bytes([1, 2, 3, 4, 5, 6]),
    )

    # 核对数组的像素类型、尺寸和顺序。
    image_numpy = convert_mono8_frame_to_array(frame)
    assert image_numpy.dtype == np.uint8
    assert image_numpy.shape == (2, 3)
    np.testing.assert_array_equal(image_numpy, [[1, 2, 3], [4, 5, 6]])

    # 拒绝非 Mono8 像素格式。
    with pytest.raises(ValueError, match="仅支持 Mono8"):
        convert_mono8_frame_to_array(replace(frame, pixel_type=0))

    # 拒绝短于或长于图像尺寸的字节数据。
    for invalid_image_bytes in (frame.image_bytes[:-1], frame.image_bytes + b"\x07"):
        with pytest.raises(ValueError, match="字节数与尺寸不符"):
            convert_mono8_frame_to_array(
                replace(frame, image_bytes=invalid_image_bytes)
            )


def test_single_empty_frame_does_not_end_capture() -> None:
    """确认单次无帧后继续读取并保留随后得到的帧。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 无帧后成功读取下一帧
    """
    camera_frame = object()
    read_results = iter((None, camera_frame))
    sdk_camera = SimpleNamespace(
        received_frame_count=0,
        start_grabbing=Mock(),
        stop_grabbing=Mock(),
    )

    def read_frame(stop_requested: threading.Event, timeout_ms: int) -> object | None:
        """依次返回无帧和有效帧，并在有效帧后停止采集。

        Args:
            stop_requested: 本轮采集的停止通知。
            timeout_ms: 单次读取的期限。

        Returns:
            返回示例：
                None  # 第一次读取没有帧
                object()  # 第二次读取的有效帧
        """
        frame = next(read_results)
        if frame is not None:
            sdk_camera.received_frame_count += 1
            stop_requested.set()
        return frame

    # 执行整轮采集并核对有效帧与读取次数。
    sdk_camera.read_frame = Mock(side_effect=read_frame)
    capture_task = CaptureTask(sdk_camera, time.monotonic(), 1.0, 50)
    result = capture_task.run_capture()
    assert result.frames == (camera_frame,)
    assert result.statistics["received_frame_count"] == 1
    assert sdk_camera.read_frame.call_count == 2
    sdk_camera.stop_grabbing.assert_called_once()


@pytest.mark.asyncio
async def test_mvs_capture_error_publishes_machine_failure() -> None:
    """确认 SDK 设备故障在采集资源释放后交付本机。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # CAPTURE_FAILED 已交付且相机不可用
    """
    capture_lock = threading.Lock()
    capture_lock.acquire()
    sdk_camera = SimpleNamespace(
        serial="camera-1",
        closed=False,
        faulted=False,
        received_frame_count=0,
        capture_lock=capture_lock,
        start_grabbing=Mock(),
        read_frame=Mock(side_effect=MvsError("GetImageBuffer 失败")),
        stop_grabbing=Mock(),
    )
    publish_event = AsyncMock()
    on_system_failure = Mock()
    camera = Camera("1", 1000, 50, publish_event, on_system_failure)
    camera.sdk_camera = sdk_camera
    capture_task = CaptureTask(sdk_camera, time.monotonic(), 1.0, 50)
    camera.current_capture = capture_task

    # 执行采集交付并核对事件、资源和相机状态。
    await camera.capture_and_deliver_result("session-1", capture_task)
    event = publish_event.await_args.args[0]
    assert event.event_type == EventType.CAPTURE_FAILED
    assert event.session_id == "session-1"
    assert event.payload == "GetImageBuffer 失败"
    assert capture_task.capture_finished.is_set()
    assert camera.current_capture is None
    assert not capture_lock.locked()
    assert not camera.available
    on_system_failure.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_capture_error_uses_system_failure_callback() -> None:
    """确认未知采集异常继续进入原有全局故障回调。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 未交付 CAPTURE_FAILED，原始异常已交给系统故障入口
    """
    sdk_camera = SimpleNamespace(
        serial="camera-1",
        closed=False,
        faulted=False,
        received_frame_count=0,
        capture_lock=threading.Lock(),
        start_grabbing=Mock(),
        read_frame=Mock(side_effect=RuntimeError("程序处理错误")),
        stop_grabbing=Mock(),
    )
    publish_event = AsyncMock()
    on_system_failure = Mock()
    camera = Camera("1", 1000, 50, publish_event, on_system_failure)
    camera.sdk_camera = sdk_camera

    # 启动采集并等待任务完成回调处理原始异常。
    camera.start_capture("session-1", time.monotonic())
    delivery_task = camera.delivery_task
    await asyncio.gather(delivery_task, return_exceptions=True)
    await asyncio.sleep(0)
    publish_event.assert_not_awaited()
    assert isinstance(on_system_failure.call_args.args[0], RuntimeError)
