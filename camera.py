"""启动单线程整轮采集，并将内存帧一次性交付给所属测量周期。"""

import asyncio
import time
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from configuration import MachineConfiguration, MeasurementConfiguration
from enums import EventType
from models import CaptureSummary, MeasurementEvent, PublishEvent
from mvs_sdk import CameraFrame, MvsCamera, MvsError


@dataclass(frozen=True)
class CaptureResult:
    """保存一轮独立内存帧与采集统计。"""

    frames: tuple[CameraFrame, ...] = field(repr=False)
    capture_duration_seconds: float
    received_frame_count: int
    skipped_frame_count: int
    camera_stopped: bool
    capture_errors: tuple[str, ...]


@dataclass(eq=False)
class CaptureTask:
    """持有单个采集线程、停止信号和完成通知。"""

    camera: MvsCamera
    capture_id: str
    capture_start_time: float
    duration_seconds: float
    timeout_ms: int
    report_failure: Callable[[Exception], None] | None = None
    capture_stop_time: float | None = None
    stop_requested: threading.Event = field(default_factory=threading.Event)
    completion_future: Future = field(default_factory=Future)
    thread: threading.Thread | None = None

    def run_capture(self) -> None:
        """按时间边界收集全部帧，停止相机后交付整轮结果。

        Args:
            无外部参数。

        Returns:
            None  # 结果通过 completion_future 交付
        """
        # 准备本轮帧集合、截止时间和统计基线。
        frames = []   # 存放这一轮采集到的原始帧
        errors = []  # 记录采集或停止相机时的错误
        skipped_count = 0
        camera_stopped = True
        initial_frame_count = self.camera.received_frame_count
        deadline = self.capture_start_time + self.duration_seconds  # 本轮采集的截止时间
        try:
            # 未提前关闭时启动取流，循环保存边界内的独立内存帧。
            if not self.stop_requested.is_set() and time.monotonic() < deadline:
                # 没有收到停止信号；采集窗口还没过期，执行 start_grabbing
                self.camera.start_grabbing()

            # 本次采集未提前停止并且没过期，进入循环，每次读取一帧
            while not self.stop_requested.is_set() and time.monotonic() < deadline:
                # 读取本轮采集的剩余时间
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                frame = self.camera.read_frame(self.stop_requested, min(self.timeout_ms, remaining_ms))
                if frame is None:
                    continue
                stop_time = min(deadline, self.capture_stop_time) if self.capture_stop_time is not None else deadline
                if self.capture_start_time <= frame.received_monotonic <= stop_time:
                    frames.append(frame)
                else:
                    skipped_count += 1
        except Exception as error:
            # 保存设备故障并通知应用退出。
            self.camera.faulted = True
            errors.append(str(error))
            if self.report_failure is not None:
                self.report_failure(error)
        finally:
            # 归还帧缓存后的同一线程停止取流，最后释放相机占用。
            try:
                self.camera.stop_grabbing()
            except Exception as error:
                camera_stopped = False
                self.camera.faulted = True
                errors.append(str(error))
                if self.report_failure is not None:
                    self.report_failure(error)
            finally:
                self.camera.capture_lock.release()

            # 收集最终统计，将帧集合交给异步调用方。
            result = CaptureResult(
                frames=tuple(frames),
                capture_duration_seconds=time.monotonic() - self.capture_start_time,
                received_frame_count=self.camera.received_frame_count - initial_frame_count,
                skipped_frame_count=skipped_count,
                camera_stopped=camera_stopped,
                capture_errors=tuple(errors),
            )

            # 保存整轮结果，通知等待结果的异步任务继续执行。
            self.completion_future.set_result(result)

    def wait(self, timeout_seconds: float | None = None) -> CaptureResult:
        """等待采集线程退出并取得整轮结果。

        Args:
            timeout_seconds: 最长等待秒数，None 表示不限时。

        Returns:
            CaptureResult(
                frames=(),  # 独立内存帧，非空时元素为 CameraFrame
                capture_duration_seconds=1.0,  # 采集耗时
                received_frame_count=0,  # 实际接收数量
                skipped_frame_count=0,  # 时间边界排除数量
                camera_stopped=True,  # 相机是否停止
                capture_errors=(),  # 设备错误信息
            )
        """
        # 等待结果和线程退出，再返回封闭数据。
        result = self.completion_future.result(timeout_seconds)
        self.thread.join()
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
                capture_id=capture_id,
                capture_start_time=capture_start_time,
                duration_seconds=self.configuration.capture_window_ms / 1000,
                timeout_ms=self.configuration.camera_timeout_ms,
                report_failure=lambda error: event_loop.call_soon_threadsafe(self.report_failure, error, component),
            )
            # 启动后台线程，顺序完成取流、收集帧和停止取流。
            capture_task.thread = threading.Thread(
                target=capture_task.run_capture,
                name=f"Capture-{capture_id}",
            )
            capture_task.thread.start()
        except Exception:
            # 启动失败时释放相机采集锁。
            camera.capture_lock.release()
            raise

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
