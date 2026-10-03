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
    camera = Camera("1", "1号皮带机", 1000, 50, publish_event, on_system_failure)
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
    camera = Camera("1", "1号皮带机", 1000, 50, publish_event, on_system_failure)
    camera.sdk_camera = sdk_camera

    # 启动采集并等待任务完成回调处理原始异常。
    camera.start_capture("session-1", time.monotonic())
    delivery_task = camera.delivery_task
    await asyncio.gather(delivery_task, return_exceptions=True)
    await asyncio.sleep(0)
    publish_event.assert_not_awaited()
    assert isinstance(on_system_failure.call_args.args[0], RuntimeError)


def create_test_delivery_camera(publish_event) -> Camera:
    """建立保留真实采集锁和任务回调的测试相机。

    Args:
        publish_event: 控制交付顺序的异步事件入口。

    Returns:
        返回示例：
            Camera(...)  # 带模拟 SDK 的测试相机
    """
    # 准备单帧读取和真实采集锁。
    sdk_camera = SimpleNamespace(
        serial="camera-1", closed=False, faulted=False,
        received_frame_count=0, capture_lock=threading.Lock(),
        start_grabbing=Mock(), stop_grabbing=Mock(),
    )

    def read_frame(stop_requested: threading.Event, timeout_ms: int):
        """交付单帧后停止本轮取流。

        Args:
            stop_requested: 所属采集停止通知。
            timeout_ms: 单次读取期限。

        Returns:
            返回示例：
                object()  # 所属原始帧
        """
        sdk_camera.received_frame_count += 1
        stop_requested.set()
        return object()

    sdk_camera.read_frame = Mock(side_effect=read_frame)
    camera = Camera("1", "1号皮带机", 1000, 50, publish_event, Mock())
    camera.sdk_camera = sdk_camera
    return camera


@pytest.mark.asyncio
async def test_early_close_waits_for_stream_stop_not_delivery_or_old_callback() -> None:
    """确认早 CLOSE 只等待停流，旧轮交付回调不清除新采集或释放新锁。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 停流、交付和新旧任务引用分别保持正确
    """
    # 让第一轮结果交付等待放行。
    delivery_entered = asyncio.Event()
    delivery_release = asyncio.Event()
    first_read_entered = asyncio.Event()
    second_read_entered = asyncio.Event()
    stream_stop_entered = asyncio.Event()
    stream_stop_release = threading.Event()
    loop = asyncio.get_running_loop()

    async def deliver_after_release(event):
        """阻塞第一轮交付直到测试放行。

        Args:
            event: 所属相机结果。

        Returns:
            返回示例：
                None  # 所属结果已交付
        """
        if event.session_id == "first":
            delivery_entered.set()
            await delivery_release.wait()

    camera = create_test_delivery_camera(deliver_after_release)
    read_number = 0

    def wait_for_stop(stop_requested, timeout_ms):
        """保持读帧等待到对应现场 CLOSE。

        Args:
            stop_requested: 所属周期停止通知。
            timeout_ms: 单帧读取期限。

        Returns:
            返回示例：
                None  # 本轮没有收到帧
        """
        nonlocal read_number
        read_number += 1
        entered = first_read_entered if read_number == 1 else second_read_entered
        loop.call_soon_threadsafe(entered.set)
        assert stop_requested.wait(5)
        return None

    def stop_after_release():
        """等待测试放行第一轮真实停流。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 所属取流已停止
        """
        if read_number == 1:
            loop.call_soon_threadsafe(stream_stop_entered.set)
            assert stream_stop_release.wait(5)

    camera.sdk_camera.read_frame.side_effect = wait_for_stop
    camera.sdk_camera.stop_grabbing.side_effect = stop_after_release
    first_capture, first_delivery = camera.start_capture("first", time.monotonic())
    close_task = None
    try:
        # 第一轮 CLOSE 必须等待底层停流完成。
        await asyncio.wait_for(first_read_entered.wait(), 1)
        close_task = asyncio.create_task(
            camera.inform_capture_workflow_stop(first_capture)
        )
        await asyncio.wait_for(stream_stop_entered.wait(), 1)
        assert not close_task.done()
        assert camera.is_capturing
        stream_stop_release.set()
        await asyncio.wait_for(close_task, 1)
        await asyncio.wait_for(delivery_entered.wait(), 1)
        assert not first_delivery.done()
        assert not camera.is_capturing

        # 新轮取流期间，旧轮完成与重复回调均不能释放新锁或清除新引用。
        second_capture, second_delivery = camera.start_capture(
            "second", time.monotonic()
        )
        await asyncio.wait_for(second_read_entered.wait(), 1)
        delivery_release.set()
        await first_delivery
        camera.handle_capture_task_finished(first_delivery, first_capture)
        assert camera.current_capture is second_capture
        assert camera.delivery_task is second_delivery
        assert camera.is_capturing
        assert first_capture.lock_released
        assert not second_capture.lock_released
        assert not second_capture.stop_requested.is_set()
        await camera.inform_capture_workflow_stop(second_capture)
        await second_delivery
        camera.on_system_failure.assert_not_called()
    finally:
        stream_stop_release.set()
        delivery_release.set()
        await camera.stop()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_capture_cancelled_before_start_releases_its_lock_once() -> None:
    """确认交付任务尚未运行即取消时仍归还本轮锁且不影响下次采集。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 未执行 SDK 取流，本轮锁与引用已回收，下轮正常完成
    """
    camera = create_test_delivery_camera(AsyncMock())
    first_capture, first_delivery = camera.start_capture("first", time.monotonic())
    first_delivery.cancel()
    await asyncio.gather(first_delivery, return_exceptions=True)
    assert first_capture.capture_finished.is_set()
    assert first_capture.lock_released
    assert not camera.is_capturing
    assert camera.current_capture is None
    assert camera.delivery_task is None
    camera.sdk_camera.start_grabbing.assert_not_called()

    # 下轮开始后重复旧回调不能归还下轮的锁。
    second_capture, second_delivery = camera.start_capture("second", time.monotonic())
    camera.handle_capture_task_finished(first_delivery, first_capture)
    assert camera.is_capturing
    assert camera.current_capture is second_capture
    await second_delivery
    assert not camera.unfinished_delivery_tasks
    camera.on_system_failure.assert_not_called()


@pytest.mark.asyncio
async def test_camera_stop_waits_for_all_cycle_deliveries() -> None:
    """确认退出等待全部交付任务，后轮先交付也不会遗漏旧轮。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 两轮交付全部完成后 stop 才返回
    """
    delivery_entered = {
        session_id: asyncio.Event() for session_id in ("first", "second")
    }
    delivery_release = {
        session_id: asyncio.Event() for session_id in ("first", "second")
    }

    async def deliver_after_release(event):
        """等待所属周期的交付放行。

        Args:
            event: 带周期编号的结果事件。

        Returns:
            返回示例：
                None  # 所属结果交付结束
        """
        delivery_entered[event.session_id].set()
        await delivery_release[event.session_id].wait()

    camera = create_test_delivery_camera(deliver_after_release)
    shutdown_task = None
    try:
        # 两轮先完成停流，再各自等待交付。
        _, first_delivery = camera.start_capture("first", time.monotonic())
        await asyncio.wait_for(delivery_entered["first"].wait(), 1)
        _, second_delivery = camera.start_capture("second", time.monotonic())
        await asyncio.wait_for(delivery_entered["second"].wait(), 1)
        assert len(camera.unfinished_delivery_tasks) == 2
        shutdown_task = asyncio.create_task(camera.stop())
        delivery_release["second"].set()
        await second_delivery
        assert camera.delivery_task is None
        assert not shutdown_task.done()
        assert first_delivery in camera.unfinished_delivery_tasks

        # 旧轮交付结束后才完成退出。
        delivery_release["first"].set()
        await asyncio.wait_for(shutdown_task, 1)
        assert not camera.unfinished_delivery_tasks
        camera.on_system_failure.assert_not_called()
    finally:
        for release in delivery_release.values():
            release.set()
        await camera.stop()
        if shutdown_task is not None:
            await shutdown_task
