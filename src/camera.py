"""启动单线程整轮采集，并将内存帧一次性交付给所属测量周期。"""

import asyncio
import logging
import time
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from async_utils import run_blocking_operation
from enums import EventType
from models import CaptureResult, MeasurementEvent, PublishEvent
from mvs_sdk import MvsCamera, MvsError


logger = logging.getLogger(__name__)


@dataclass(eq=False)
class CaptureTask:
    """保存采集参数、停止信号和完成通知。"""

    sdk_camera: MvsCamera
    capture_start_time: float
    duration_seconds: float
    timeout_ms: int
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
                },
            )
        """
        # 准备本轮帧集合、截止时间和统计基线。
        frames = []   # 存放这一轮采集到的原始帧
        initial_frame_count = self.sdk_camera.received_frame_count
        deadline = self.capture_start_time + self.duration_seconds  # 本轮采集的截止时间
        try:
            # 未提前关闭且窗口未到期时启动取流。
            if not self.stop_requested.is_set() and time.monotonic() < deadline:
                # 没有收到停止信号；采集窗口还没过期，执行 start_grabbing
                self.sdk_camera.start_grabbing()

            # 本次采集未提前停止并且没过期，进入循环，每次读取一帧
            while not self.stop_requested.is_set() and time.monotonic() < deadline:
                # 按固定超时读取一帧，返回后在下一次循环检查是否结束。
                frame = self.sdk_camera.read_frame(self.stop_requested, self.timeout_ms)
                if frame is None:
                    continue
                # 保存本次读取的帧，允许停止信号或窗口到期时的尾帧。
                frames.append(frame)
        except Exception:
            # 记录采集异常，标记相机故障并继续抛出。
            self.sdk_camera.faulted = True
            logger.exception("相机采集失败 serial=%s", self.sdk_camera.serial)
            raise
        finally:
            # 在同一线程停止取流，停流异常保留此前的采集异常链。
            try:
                self.sdk_camera.stop_grabbing()
            except Exception:
                self.sdk_camera.faulted = True
                logger.exception("相机停流失败 serial=%s", self.sdk_camera.serial)
                raise

        # 成功停流后返回本轮全部帧和统计。
        return CaptureResult(
            frames=tuple(frames),
            statistics={
                "capture_duration_seconds": time.monotonic() - self.capture_start_time,
                "received_frame_count": self.sdk_camera.received_frame_count - initial_frame_count,
                "retained_frame_count": len(frames),
            },
        )


class Camera:
    """管理相机采集生命周期和周期身份。"""

    def __init__(
        self,
        machine_id: str,
        capture_window_ms: int,
        camera_timeout_ms: int,
        publish_event: PublishEvent,
        report_failure: Callable[[Exception], None],
    ) -> None:
        """登记机器编号、采集任务与业务事件入口。

        Args:
            machine_id: 采集结果归属的机器编号。
            capture_window_ms: 单轮采集窗口毫秒数。
            camera_timeout_ms: 单帧读取超时毫秒数。
            publish_event: 整轮结果交付入口。
            report_failure: 应用故障入口。

        Returns:
            None  # 相机适配器初始化完成
        """
        # 保存外部依赖和 SDK 相机对象。
        self.machine_id = machine_id
        self.capture_window_ms = capture_window_ms
        self.camera_timeout_ms = camera_timeout_ms
        self.publish_event = publish_event
        self.report_failure = report_failure
        self.sdk_camera: MvsCamera | None = None
        # 分别登记现场采集与结果交付任务。
        self.current_capture: CaptureTask | None = None
        self.delivery_task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        """返回相机是否可以采集。

        Args:
            无外部参数。

        Returns:
            True  # 相机已打开且无故障，否则为 False
        """
        return self.sdk_camera is not None and not self.sdk_camera.closed and not self.sdk_camera.faulted

    @property
    def is_capturing(self) -> bool:
        """返回相机是否被现场采集占用。

        Args:
            无外部参数。

        Returns:
            True  # 相机被占用，否则为 False
        """
        return self.sdk_camera is not None and self.sdk_camera.capture_lock.locked()

    def start_capture(self, session_id: str, capture_start_time: float) -> None:
        """启动整轮采集并安排一次性结果交付。

        Args:
            session_id: 测量周期编号。
            capture_start_time: START 受理时的单调时间。

        Returns:
            None  # 后台采集和结果交付已启动
        """
        # 取得相机采集锁，检查相机状态。
        sdk_camera = self.sdk_camera
        if not sdk_camera.capture_lock.acquire(blocking=False):
            raise MvsError(f"相机正在采集：{sdk_camera.serial}")
        try:
            if sdk_camera.closed or sdk_camera.faulted:
                raise MvsError(f"相机不可用：{sdk_camera.serial}")

            # 创建本轮任务，登记采集窗口和单次取帧超时。
            capture_task = CaptureTask(
                sdk_camera=sdk_camera,
                capture_start_time=capture_start_time,
                duration_seconds=self.capture_window_ms / 1000,
                timeout_ms=self.camera_timeout_ms,
            )

        except Exception:
            # 启动失败时释放相机采集锁。
            sdk_camera.capture_lock.release()
            raise

        # 创建采集任务，创建失败时关闭尚未启动的协程并归还相机占用。
        capture_workflow = self.capture_and_deliver_result(session_id, capture_task)
        try:
            self.delivery_task = asyncio.create_task(capture_workflow)
        except Exception:
            capture_workflow.close()
            sdk_camera.capture_lock.release()
            raise

        # 登记采集引用与结束回调。
        self.current_capture = capture_task
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
            self.current_capture.sdk_camera.capture_lock.release()
            self.current_capture.capture_finished.set()
            self.current_capture = None
        self.delivery_task = None
        if task.cancelled():
            return
        # 将采集或结果交付异常交给应用停止流程。
        error = task.exception()
        if error is not None:
            self.report_failure(error)

    async def inform_capture_workflow_stop(self) -> None:
        """通知采集线程停止，并等待采集结束。

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
        except Exception:
            # 采集线程尚未记录的调度异常在此记录。
            if not capture_task.sdk_camera.faulted:
                logger.exception("采集任务执行失败 machine_id=%s", self.machine_id)
            raise
        finally:
            # 释放相机占用，并通知等待方：本轮采集已结束，相机已完成停流处理。
            capture_task.sdk_camera.capture_lock.release()
            capture_task.capture_finished.set()
            self.current_capture = None
        # 将成功采集的整轮结果交回所属机器。
        try:
            await self.publish_event(MeasurementEvent(
                EventType.CAPTURE_COMPLETED, self.machine_id, session_id, result,
            ))
        except Exception:
            # 记录结果交付异常并结束后台任务。
            logger.exception("采集结果交付失败 machine_id=%s session_id=%s", self.machine_id, session_id)
            raise

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
