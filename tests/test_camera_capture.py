"""验证单帧无数据和相机采集异常的交付边界。"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from camera.camera import Camera, CaptureTask
from camera.hikrobot_sdk import MvsError
from enums import EventType


def test_single_empty_frame_does_not_end_capture() -> None:
    """确认单次无帧后继续读取并保留随后得到的帧。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 无帧后成功读取下一帧
    """
    captured_frame = object()
    read_results = iter((None, captured_frame))
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
    assert result.frames == (captured_frame,)
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
    on_fatal_error = Mock()
    camera = Camera("1", 1000, 50, publish_event, on_fatal_error)
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
    on_fatal_error.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_capture_error_uses_fatal_callback() -> None:
    """确认未知采集异常继续进入原有全局故障回调。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 未交付 CAPTURE_FAILED，原始异常已交给致命故障入口
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
    on_fatal_error = Mock()
    camera = Camera("1", 1000, 50, publish_event, on_fatal_error)
    camera.sdk_camera = sdk_camera

    # 启动采集并等待任务完成回调处理原始异常。
    camera.start_capture("session-1", time.monotonic())
    delivery_task = camera.delivery_task
    await asyncio.gather(delivery_task, return_exceptions=True)
    await asyncio.sleep(0)
    publish_event.assert_not_awaited()
    assert isinstance(on_fatal_error.call_args.args[0], RuntimeError)
