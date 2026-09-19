"""在单个线程中收集一轮相机帧并释放取流资源。"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from uuid import uuid4

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
        frames = []
        errors = []
        skipped_count = 0
        camera_stopped = True
        initial_frame_count = self.camera.received_frame_count
        deadline = self.capture_start_time + self.duration_seconds
        try:
            # 未提前关闭时启动取流，循环保存边界内的独立内存帧。
            if not self.stop_requested.is_set() and time.monotonic() < deadline:
                self.camera.start_grabbing()
            while not self.stop_requested.is_set() and time.monotonic() < deadline:
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


def start_capture(
    camera: MvsCamera,
    duration_seconds: float = 1.0,
    timeout_ms: int = 50,
    capture_id: str | None = None,
    capture_start_time: float | None = None,
    report_failure: Callable[[Exception], None] | None = None,
) -> CaptureTask:
    """占用相机并启动单线程整轮采集。

    Args:
        camera: 已打开的相机。
        duration_seconds: 从业务起点计算的采集窗口秒数。
        timeout_ms: 单次取帧超时毫秒数。
        capture_id: 采集编号，未指定时生成。
        capture_start_time: 业务起点的单调时间，未指定时取当前时间。
        report_failure: 设备异常通知入口。

    Returns:
        CaptureTask(
            camera=camera,  # 已打开的设备
            capture_id="capture-1",  # 本轮采集编号
            capture_start_time=1.0,  # 单调采集起点
            duration_seconds=1.0,  # 采集窗口秒数
            timeout_ms=50,  # 单次取帧等待毫秒数
            report_failure=None,  # 设备故障通知入口
            capture_stop_time=None,  # 提前关闭的截止时间
            stop_requested=threading.Event(),  # 停止请求信号
            completion_future=Future(),  # 整轮结果，数据结构见 wait
            thread=capture_thread,  # 唯一采集线程
        )
    """
    # 校验公共采集参数并取得设备独占权。
    if duration_seconds <= 0 or timeout_ms <= 0:
        raise ValueError("采集时长和取帧超时必须大于零")
    if not camera.capture_lock.acquire(blocking=False):
        raise MvsError(f"相机正在采集：{camera.serial}")
    try:
        if camera.closed or camera.faulted:
            raise MvsError(f"相机不可用：{camera.serial}")
        # 创建本轮任务并启动唯一采集线程。
        task = CaptureTask(
            camera=camera,
            capture_id=capture_id or uuid4().hex,
            capture_start_time=capture_start_time if capture_start_time is not None else time.monotonic(),
            duration_seconds=duration_seconds,
            timeout_ms=timeout_ms,
            report_failure=report_failure,
        )
        task.thread = threading.Thread(target=task.run_capture, name=f"Capture-{task.capture_id}")
        task.thread.start()
        return task
    except Exception:
        camera.capture_lock.release()
        raise
