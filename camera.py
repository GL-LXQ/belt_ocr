"""启动单线程整轮采集，并将内存帧一次性交付给所属测量周期。"""

import asyncio
import time
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from async_utils import run_blocking_operation
from configuration import MachineConfiguration, MeasurementConfiguration
from enums import EventType
from models import CaptureResult, MeasurementEvent, PublishEvent
from mvs_sdk import MvsCamera, MvsError


@dataclass(eq=False)
class CaptureTask:
    """保存采集参数、故障回调、停止信号和完成通知。"""

    camera: MvsCamera
    capture_start_time: float
    duration_seconds: float
    timeout_ms: int
    report_failure: Callable[[Exception], None]
    stop_requested: threading.Event = field(default_factory=threading.Event)
    capture_finished: asyncio.Event = field(default_factory=asyncio.Event)

    def run_capture(self) -> CaptureResult:
        """循环收集全部帧，停止相机后交付整轮结果。

        Args:
            无外部参数。

        Returns:
            CaptureResult(
                frames=(),  # 本轮原始帧集合
                statistics={
                    "capture_duration_seconds": 1.0,  # 采集耗时秒数
                    "received_frame_count": 0,  # SDK 接收帧数
                    "retained_frame_count": 0,  # 保存帧数
                    "camera_stopped": True,  # 停流是否成功
                    "capture_errors": [],  # 采集错误列表
                },
                errors=(),  # 采集错误集合
            )
        """
        # 准备本轮帧集合、截止时间和统计基线。
        frames = []   # 存放这一轮采集到的原始帧
        errors = []  # 记录采集或停止相机时的错误
        camera_stopped = True
        initial_frame_count = self.camera.received_frame_count
        deadline = self.capture_start_time + self.duration_seconds  # 本轮采集的截止时间
        try:
            # 未提前关闭且窗口未到期时启动取流。
            if not self.stop_requested.is_set() and time.monotonic() < deadline:
                # 没有收到停止信号；采集窗口还没过期，执行 start_grabbing
                self.camera.start_grabbing()

            # 本次采集未提前停止并且没过期，进入循环，每次读取一帧
            while not self.stop_requested.is_set() and time.monotonic() < deadline:
                # 按固定超时读取一帧，返回后在下一次循环检查是否结束。
                frame = self.camera.read_frame(self.stop_requested, self.timeout_ms)
                if frame is None:
                    continue
                # 保存本次读取的帧，允许停止信号或窗口到期时的尾帧。
                frames.append(frame)
        except Exception as error:
            # 保存设备故障并通知应用退出。
            self.camera.faulted = True
            errors.append(str(error))
            self.report_failure(error)
        finally:
            # 归还帧缓存后，在同一线程停止取流。
            try:
                self.camera.stop_grabbing()
            except Exception as error:
                camera_stopped = False
                self.camera.faulted = True
                errors.append(str(error))
                self.report_failure(error)

            # 一次性整理本轮帧、采集统计和错误。
            result = CaptureResult(
                frames=tuple(frames),
                statistics={
                    "capture_duration_seconds": time.monotonic() - self.capture_start_time,
                    "received_frame_count": self.camera.received_frame_count - initial_frame_count,
                    "retained_frame_count": len(frames),
                    "camera_stopped": camera_stopped,
                    "capture_errors": list(errors),
                },
                errors=tuple(errors),
            )

        # 返回完整采集结果。
        return result

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

    def start_capture(self, session_id: str, capture_start_time: float) -> None:
        """启动整轮采集并安排一次性结果交付。

        Args:
            session_id: 测量周期编号。
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

        # 取得相机采集锁，检查设备状态。
        camera = self.device
        if not camera.capture_lock.acquire(blocking=False):
            raise MvsError(f"相机正在采集：{camera.serial}")
        try:
            if camera.closed or camera.faulted:
                raise MvsError(f"相机不可用：{camera.serial}")

            # 创建本轮任务，登记采集窗口、单次取帧超时和故障入口。
            capture_task = CaptureTask(
                camera=camera,
                capture_start_time=capture_start_time,
                duration_seconds=self.configuration.capture_window_ms / 1000,
                timeout_ms=self.configuration.camera_timeout_ms,
                report_failure=lambda error: event_loop.call_soon_threadsafe(self.report_failure, error, component),
            )

        except Exception:
            # 启动失败时释放相机采集锁。
            camera.capture_lock.release()
            raise

        # 保存本轮采集任务，供停止采集时使用。
        self.current_capture = capture_task

        # 创建异步任务，等待采集结束并发布整轮采集结果。
        self.delivery_task = asyncio.create_task(self.capture_and_deliver_result(session_id, capture_task))

        # 结果交付任务结束后，清理任务引用并处理未捕获的异常。
        self.delivery_task.add_done_callback(self.handle_capture_task_finished)

    def handle_capture_task_finished(self, task: asyncio.Task) -> None:
        """移除交付任务并报告未处理异常。

        Args:
            task: 已结束的采集交付任务。

        Returns:
            None  # 任务引用已移除，异常已报告
        """
        # 清理尚未开始执行就被取消的任务，归还相机占用。
        if self.current_capture is not None:
            self.current_capture.camera.capture_lock.release()
            self.current_capture.capture_finished.set()
            self.current_capture = None
        self.delivery_task = None
        if task.cancelled():
            return
        # 将业务事件交付异常交给应用停止流程。
        error = task.exception()
        if error is not None:
            self.report_failure(error, f"采集交付 machine_id={self.machine.machine_id}")

    async def seal_capture(self) -> None:
        """发出停止信号并等待对应相机停止。

        Args:
            无外部参数。

        Returns:
            None  # 相机已停止，结果交付可能仍在排队
        """
        # 已结束的采集无需再次停止。
        capture_task = self.current_capture
        if capture_task is None:
            return
        # 发出停止通知并等待当前读取结束和硬件释放。
        capture_task.stop_requested.set()
        await capture_task.capture_finished.wait()

    async def capture_and_deliver_result(self, session_id: str, capture_task: CaptureTask) -> None:
        """在线程中完成采集，再发布本轮采集结果。

        Args:
            session_id: 帧集合所属周期。
            capture_task: 本轮底层采集任务。

        Returns:
            None  # 整轮结果已交付，采集引用已移除
        """
        # 在线程中执行采集，取消时等待实际采集结束。
        try:
            result = await run_blocking_operation(capture_task.run_capture)
        finally:
            # 采集结束后归还相机占用，唤醒等待停流的 CLOSE 处理。
            capture_task.camera.capture_lock.release()
            capture_task.capture_finished.set()
            self.current_capture = None
        # 根据采集错误发布一次整轮结果事件。
        event_type = EventType.CAPTURE_FAILED if result.errors else EventType.CAPTURE_COMPLETED
        await self.publish_event(MeasurementEvent(event_type, self.machine.machine_id, session_id, result))

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
