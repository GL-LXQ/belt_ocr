"""组织单相机固定窗口的生产、消费和收尾。"""

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from uuid import uuid4

from mvs_sdk import CameraFrame, MvsCamera, MvsError


@dataclass(frozen=True)
class CaptureFrame:
    """绑定任务身份和独立图像数据。"""

    capture_id: str
    session_id: str
    image: CameraFrame


@dataclass(frozen=True)
class FrameResult:
    """保存单帧回调返回值或失败信息。"""

    frame_number: int
    value: object = None
    error: str | None = None


@dataclass(frozen=True)
class CaptureResult:
    """保存封闭后的任务统计和逐帧结果。"""

    capture_id: str
    session_id: str
    camera_serial: str
    capture_duration_seconds: float
    received_frame_count: int
    enqueued_frame_count: int
    dropped_frame_count: int
    processed_frame_count: int
    failed_frame_count: int
    camera_stopped: bool
    capture_errors: tuple[str, ...]
    has_error: bool
    frame_results: tuple[FrameResult, ...]


@dataclass(eq=False)
class CaptureTask:
    """持有本轮专属队列、线程和结束信号。"""

    camera: MvsCamera
    process_frame: Callable[[CaptureFrame], object]
    duration_seconds: float
    frame_queue: queue.Queue
    timeout_ms: int
    session_id: str
    capture_id: str = field(default_factory=lambda: uuid4().hex)
    report_failure: Callable[[Exception], None] | None = None
    # 停止请求标记：set 发出通知，is_set 检查通知，wait 等待通知或超时。
    stop_requested: threading.Event = field(default_factory=threading.Event)
    acquisition_finished: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)
    acquisition_future: Future = field(default_factory=Future)
    completion_future: Future = field(default_factory=Future)
    controller_thread: threading.Thread | None = None
    result: CaptureResult | None = None
    _enqueued_count: int = 0
    _dropped_count: int = 0
    _processed_count: int = 0
    _failed_count: int = 0
    _capture_errors: list[str] = field(default_factory=list)
    _frame_results: list[FrameResult] = field(default_factory=list)

    def grab_frames(self) -> None:
        """持续取得独立帧并非阻塞入队，队满时丢弃当前新帧。

        Args:
            无外部参数。

        Returns:
            None  # 本轮生产线程已退出
        """
        try:
            # 每次取图前检查停止通知；未收到时继续取图，收到后退出循环。
            while not self.stop_requested.is_set():
                image = self.camera.read_frame(self.stop_requested, self.timeout_ms)
                # 本次未取得图像时，继续下一次取帧。
                if image is None:
                    continue
                # 绑定本轮身份，并立即尝试写入队列。
                frame = CaptureFrame(self.capture_id, self.session_id, image)
                try:
                    self.frame_queue.put_nowait(frame)
                    self._enqueued_count += 1
                except queue.Full:
                    # 队满直接丢弃当前新帧并计数，不等待队列空位。
                    self._dropped_count += 1
        except Exception as error:
            # 记录采集异常，并通知控制线程提前停止取流。
            self.camera.faulted = True
            self._capture_errors.append(f"采集失败：{error}")
            # 将原始异常通知应用，保留异常堆栈。
            if self.report_failure is not None:
                self.report_failure(error)
            self.stop_requested.set()

    def consume_frames(self) -> None:
        """持续处理队列中的帧，生产封口且队列排空后退出。

        Args:
            无外部参数。

        Returns:
            None  # 本轮入队帧均已处理或登记失败
        """
        while True:
            # 生产封口后再取队列，保留封口前的最后一次入队。
            producer_finished = self.acquisition_finished.is_set()
            try:
                frame = self.frame_queue.get(timeout=0.02)
            except queue.Empty:
                # 仅在已确认生产结束且队列为空时退出，否则继续等待。
                if producer_finished:
                    return
                continue

            # 逐帧调用同步处理函数，记录结果或异常。
            try:
                value = self.process_frame(frame)
                # 记录正常返回的回调结果，包含由业务回调正常跳过的帧。
                self._frame_results.append(FrameResult(frame.image.frame_number, value))
                self._processed_count += 1
            except Exception as error:
                # 保存本帧失败信息并计数，随后继续消费下一帧。
                self._frame_results.append(FrameResult(frame.image.frame_number, error=str(error)))
                self._failed_count += 1
                # 将原始异常通知应用，保留异常堆栈。
                if self.report_failure is not None:
                    self.report_failure(error)
            finally:
                # 登记当前出队帧已经处理结束。
                self.frame_queue.task_done()

    def run_capture(self) -> None:
        """启动生产消费、控制采集期限、停止取流并排空本轮结果。

        Args:
            无外部参数。

        Returns:
            None  # 封闭结果写入 result，完成后设置 completed
        """
        # 初始化本轮起止时间、停止状态和相机累计帧数基线。
        started_at = None
        stopped_at = None
        camera_stopped = True
        initial_frame_count = self.camera.received_frame_count
        # 创建本轮独立取帧线程和消费线程，登记各线程的启动状态。
        grabber = threading.Thread(target=self.grab_frames, name=f"Grabber-{self.capture_id}")
        consumer = threading.Thread(target=self.consume_frames, name=f"Consumer-{self.capture_id}")
        grabber_started = False
        consumer_started = False
        try:
            # 先启动消费者，再启动相机和独立抓帧线程。
            consumer.start()
            consumer_started = True
            # 未收到提前停止请求时，启动连续取流并记录实际开始时间。
            if not self.stop_requested.is_set():
                # 相机开始按照当前配置的帧率持续采集
                self.camera.start_grabbing()
                started_at = time.monotonic()
                # 启动取帧线程，与已启动的消费者同时工作。不断从 SDK 取出相机产生的图片，复制后放进队列
                grabber.start()
                grabber_started = True

                # 计算本轮连续采集还剩多少秒。
                remaining_seconds = max(0.0, started_at + self.duration_seconds - time.monotonic())
                # 控制线程等到时间用完，或有人调用 set 提前通知停止；期间取图和消费继续。
                self.stop_requested.wait(remaining_seconds)
        except Exception as error:
            # 保存启动阶段的异常，随后进入统一停止流程。
            self._capture_errors.append(f"启动失败：{error}")
            # 将原始异常通知应用，保留异常堆栈。
            if self.report_failure is not None:
                self.report_failure(error)
        finally:
            # 发出停止通知，让取帧线程不再取下一张图；正在取的那张继续完成。
            self.stop_requested.set()
            try:
                # 等当前 SDK Buffer 归还后，调用 SDK 实际停止相机连续取流。
                self.camera.stop_grabbing()
            except Exception as error:
                # 停止失败时标记相机故障，并记录本轮未确认停止。
                self.camera.faulted = True
                camera_stopped = False
                self._capture_errors.append(f"停止失败：{error}")
                # 将原始异常通知应用，保留异常堆栈。
                if self.report_failure is not None:
                    self.report_failure(error)
            # 记录停止操作完成的时间。
            stopped_at = time.monotonic()

            # 等待最后入队完成，释放本相机的现场采集位置。
            if grabber_started:
                grabber.join()
            # 结算本轮收到的帧数，释放相机供后续采集使用。
            received_count = self.camera.received_frame_count - initial_frame_count
            self.camera.capture_lock.release()
            # 通知消费者和业务等待方：本轮不会再有新帧入队。
            self.acquisition_finished.set()
            self.acquisition_future.set_result(None)

        # 等待消费者退出，确认没有排队帧或正在处理的帧。
        if consumer_started:
            consumer.join()
        # 汇总采集时长、入队及处理计数、异常和逐帧结果。
        duration = stopped_at - started_at if started_at is not None else 0.0
        self.result = CaptureResult(
            capture_id=self.capture_id,
            session_id=self.session_id,
            camera_serial=self.camera.serial,
            capture_duration_seconds=duration,
            received_frame_count=received_count,
            enqueued_frame_count=self._enqueued_count,
            dropped_frame_count=self._dropped_count,
            processed_frame_count=self._processed_count,
            failed_frame_count=self._failed_count,
            camera_stopped=camera_stopped,
            capture_errors=tuple(self._capture_errors),
            has_error=bool(self._capture_errors or self._failed_count),
            frame_results=tuple(self._frame_results),
        )
        # 通知同步等待方和异步收尾任务，本轮结果已封闭。
        self.completed.set()
        self.completion_future.set_result(self.result)

    def wait(self, timeout_seconds: float | None = None) -> CaptureResult:
        """等待本轮采集与消费全部结束，返回封闭结果。

        Args:
            timeout_seconds: 等待上限；None 表示等待完成，超时不取消任务。

        Returns:
            CaptureResult(
                capture_id="capture-a",  # 唯一采集任务编号
                session_id="session-a",  # 调用方业务周期编号
                camera_serial="CAM001",  # 相机序列号
                capture_duration_seconds=1.01,  # 开始到实际停止取流的秒数
                received_frame_count=3,  # SDK 成功返回的帧数
                enqueued_frame_count=2,  # 成功入队帧数
                dropped_frame_count=1,  # 队满丢弃的新帧数
                processed_frame_count=2,  # 回调正常返回的帧数
                failed_frame_count=0,  # 回调抛出异常的帧数
                camera_stopped=True,  # 是否确认停止取流
                capture_errors=(),  # 启动、取帧或停止异常
                has_error=False,  # 是否出现采集或处理异常
                frame_results=(  # 按消费顺序保存的逐帧结果
                    FrameResult(
                        frame_number=1,  # SDK 帧编号
                        value="result-1",  # 回调返回的轻量结果
                        error=None,  # 单帧处理错误
                    ),
                    FrameResult(
                        frame_number=2,  # SDK 帧编号
                        value="result-2",  # 回调返回的轻量结果
                        error=None,  # 单帧处理错误
                    ),
                ),
            )
        """
        if not self.completed.wait(timeout_seconds):
            raise TimeoutError(f"采集任务尚未完成：{self.capture_id}")
        # 等待控制线程释放局部资源，再交付最终结果。
        self.controller_thread.join()
        return self.result


def start_capture(
    camera: MvsCamera,
    process_frame: Callable[[CaptureFrame], object],
    session_id: str,
    duration_seconds: float = 1.0,
    queue_capacity: int = 32,
    timeout_ms: int = 50,
    capture_id: str | None = None,
    report_failure: Callable[[Exception], None] | None = None,
) -> CaptureTask:
    """响应采集信号，创建独立任务、队列和后台控制线程。

    Args:
        camera: 使用 MvsSdk.open_camera 打开的相机。
        process_frame: 同步逐帧回调；返回轻量结果，抛出异常时记录失败并继续。
        session_id: 调用方业务周期编号。
        duration_seconds: 固定采集窗口秒数。
        queue_capacity: 本轮最大排队帧数，队满丢新帧。
        timeout_ms: 单次 SDK 等帧上限，单位毫秒。
        capture_id: 调用方的采集编号，省略时自动生成。
        report_failure: 可选的线程故障通知入口，接收采集或处理异常。

    Returns:
        返回已启动的 CaptureTask，以下为字段示例；线程状态及计数随后台执行更新：
            CaptureTask(
                camera=camera,  # 已打开的相机对象
                process_frame=process_frame,  # 逐帧同步处理回调
                duration_seconds=1.0,  # 连续采集时长，单位秒
                frame_queue=queue.Queue(maxsize=32),  # 本轮独立有界图像队列
                timeout_ms=50,  # 单次 SDK 取帧超时，单位毫秒
                session_id="session-a",  # 所属测量编号
                capture_id="capture-a",  # 本轮采集编号
                report_failure=None,  # 线程故障通知入口
                stop_requested=threading.Event(),  # 提前停止请求
                acquisition_finished=threading.Event(),  # 生产端结束信号
                completed=threading.Event(),  # 整轮生产和消费完成信号
                acquisition_future=Future(),  # 向异步调用方通知生产结束
                completion_future=Future(),  # 向异步调用方交付最终 CaptureResult
                controller_thread=controller_thread,  # 已启动的本轮控制线程
                result=None,  # 完成后写入 CaptureResult，完整字段示例见 wait
                _enqueued_count=0,  # 已入队数量
                _dropped_count=0,  # 队满丢弃数量
                _processed_count=0,  # 回调正常返回数量
                _failed_count=0,  # 回调失败数量
                _capture_errors=[],  # 启动、取帧和停止异常
                _frame_results=[],  # 逐帧结果，完整字段示例见 wait
            )
    """
    # 检查采集时长、队列容量和单次取帧超时。
    if duration_seconds <= 0 or queue_capacity <= 0 or timeout_ms <= 0:
        raise ValueError("采集时长、队列容量和取帧超时必须大于零")
    # 立即尝试占用相机；已有采集时直接报忙。
    if not camera.capture_lock.acquire(blocking=False):
        raise MvsError(f"相机正在采集：{camera.serial}")
    try:
        # 检查设备状态，并为本轮建立独立有界队列。
        if camera.closed or camera.faulted:
            raise MvsError(f"相机不可用：{camera.serial}")
        # 创建本轮独立有界队列，保存处理回调和业务身份。
        task = CaptureTask(
            camera=camera,
            process_frame=process_frame,
            duration_seconds=duration_seconds,
            frame_queue=queue.Queue(maxsize=queue_capacity),
            timeout_ms=timeout_ms,
            session_id=session_id,
            capture_id=capture_id or uuid4().hex,
            report_failure=report_failure,
        )

        # 启动本轮控制线程，立即返回任务句柄。
        task.controller_thread = threading.Thread(target=task.run_capture, name=f"Capture-{task.capture_id}")
        task.controller_thread.start()
        return task
    except Exception:
        # 创建或启动任务失败时释放相机占用，并继续抛出异常。
        camera.capture_lock.release()
        raise
