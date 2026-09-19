"""将单轮相机采集结果一次性交付给所属测量周期。"""

import asyncio
import time
from collections.abc import Callable

from configuration import MachineConfiguration, MeasurementConfiguration
from enums import EventType
from models import CaptureSummary, MeasurementEvent, PublishEvent
from mvs_capture import CaptureTask, start_capture
from mvs_sdk import MvsCamera


class SessionCamera:
    """管理相机采集生命周期和周期身份。"""

    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
        report_failure: Callable[[Exception, str], None],
    ) -> None:
        """登记设备配置、采集任务与业务事件入口。

        Args:
            machine: 机器和设备身份。
            configuration: 采集窗口和单帧超时配置。
            publish_event: 整轮结果交付入口。
            report_failure: 应用故障入口。

        Returns:
            None  # 相机适配器初始化完成
        """
        # 保存外部依赖和设备句柄。
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event
        self.report_failure = report_failure
        self.device: MvsCamera | None = None
        # 分别登记现场采集与结果交付任务。
        self.current_capture: CaptureTask | None = None
        self.delivery_task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        """返回设备是否可以采集。

        Args:
            无外部参数。

        Returns:
            True  # 设备已打开且无故障，否则为 False
        """
        return self.device is not None and not self.device.closed and not self.device.faulted

    @property
    def is_capturing(self) -> bool:
        """返回相机是否被现场采集占用。

        Args:
            无外部参数。

        Returns:
            True  # 相机被占用，否则为 False
        """
        return self.device is not None and self.device.capture_lock.locked()

    def start_capture(self, session_id: str, capture_id: str, capture_start_time: float) -> None:
        """启动整轮采集并安排一次性结果交付。

        Args:
            session_id: 测量周期编号。
            capture_id: 采集编号。
            capture_start_time: START 受理时的单调时间。

        Returns:
            None  # 后台采集和结果交付已启动
        """
        # 将采集线程的设备故障路由回应用事件循环。
        event_loop = asyncio.get_running_loop()
        component = (
            f"相机 machine_id={self.machine.machine_id} "
            f"camera_id={self.machine.camera_id} session_id={session_id}"
        )
        capture_task = start_capture(
            self.device,
            # 整轮采集窗口持续时间
            duration_seconds=self.configuration.capture_window_ms / 1000,
            # 一次取帧最多等多久
            timeout_ms=self.configuration.camera_timeout_ms,
            capture_id=capture_id,
            capture_start_time=capture_start_time,
            report_failure=lambda error: event_loop.call_soon_threadsafe(self.report_failure, error, component),
        )
        
        # 保存本轮采集任务，供停止采集时使用。
        self.current_capture = capture_task

        # 创建异步任务，等待采集结束并发布整轮采集结果。
        self.delivery_task = asyncio.create_task(self.finish_capture(session_id, capture_task))

        # 结果交付任务结束后，清理任务引用并处理未捕获的异常。
        self.delivery_task.add_done_callback(self.handle_capture_task_finished)

    def handle_capture_task_finished(self, task: asyncio.Task) -> None:
        """移除交付任务并报告未处理异常。

        Args:
            task: 已结束的采集交付任务。

        Returns:
            None  # 任务引用已移除，异常已报告
        """
        self.delivery_task = None
        if task.cancelled():
            return
        # 将业务事件交付异常交给应用停止流程。
        error = task.exception()
        if error is not None:
            self.report_failure(error, f"采集交付 machine_id={self.machine.machine_id}")

    async def seal_capture(self, capture_stop_time: float | None = None) -> None:
        """记录关闭边界并等待对应相机停止。

        Args:
            capture_stop_time: CLOSE 的单调时间，省略时取当前时间。

        Returns:
            None  # 相机已停止，结果交付可能仍在排队
        """
        # 已结束的采集无需再次停止。
        capture_task = self.current_capture
        if capture_task is None:
            return
        # 先保存截止时间，再发出停止通知并等待硬件释放。
        if capture_task.capture_stop_time is None:
            capture_task.capture_stop_time = capture_stop_time if capture_stop_time is not None else time.monotonic()
        capture_task.stop_requested.set()
        await asyncio.shield(asyncio.wrap_future(capture_task.completion_future))

    async def finish_capture(self, session_id: str, capture_task: CaptureTask) -> None:
        """等待采集结束并交付帧集合和统计。

        Args:
            session_id: 帧集合所属周期。
            capture_task: 本轮底层采集任务。

        Returns:
            None  # 整轮结果已交付，采集引用已移除
        """
        try:
            # 等待相机停止并准备不包含图像字节的统计信息。
            result = await asyncio.shield(asyncio.wrap_future(capture_task.completion_future))
            statistics = {
                "capture_duration_seconds": result.capture_duration_seconds,
                "received_frame_count": result.received_frame_count,
                "retained_frame_count": len(result.frames),
                "skipped_frame_count": result.skipped_frame_count,
                "camera_stopped": result.camera_stopped,
                "capture_errors": list(result.capture_errors),
            }
            summary = CaptureSummary(
                capture_id=capture_task.capture_id,
                frames=result.frames,
                skipped_frame_count=result.skipped_frame_count,
                statistics=statistics,
                errors=result.capture_errors,
            )
            # 正常和失败采集均只交付一个整轮事件。
            event_type = EventType.CAPTURE_FAILED if result.capture_errors else EventType.CAPTURE_COMPLETED
            await self.publish_event(MeasurementEvent(event_type, self.machine.machine_id, session_id, summary))
        finally:
            self.current_capture = None

    async def stop(self) -> None:
        """停止当前采集并等待结果交付结束。

        Args:
            无外部参数。

        Returns:
            None  # 本机采集和交付全部结束
        """
        # 停止唯一采集任务，再等待本轮结果交付。
        if self.current_capture is not None:
            self.current_capture.stop_requested.set()
        if self.delivery_task is not None:
            await self.delivery_task
