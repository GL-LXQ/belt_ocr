"""定义单台机器运行时实例及其事件处理流程。"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from camera.camera import Camera, CaptureTask
from config_util import MachineConfig, AppConfig
from frequency_adapter import FrequencyAdapter
from enums import (
    EventType,
    FrequencyState,
    MachineOverallStatus,
    OCRState,
    ProgressStage,
    ProgressStatus,
    SessionState,
)
from camera.hikrobot_sdk import CameraFrame, MvsError
from models import MeasurementSession, MeasurementFrame, RuntimeEvent, PublishEvent
from async_utils import run_blocking_operation
from text_recognizer import (
    OCRProcessingError,
    OCRResourceWaitTimeoutError,
    TextRecognizer,
)
from database import (
    CommitIntegrityConflictError,
    Database,
    MeasurementRecord,
    save_evidence_image,
)


logger = logging.getLogger(__name__)


class ImageEncodingError(RuntimeError):
    """证据图片编码过程中发生的未知异常。"""


class EvidenceWriteError(RuntimeError):
    """证据图片写入失败。"""


@dataclass
class CycleContext:
    """保存一轮现场与后台处理的运行资源。"""

    session: MeasurementSession  # 本轮业务状态
    capture_task: CaptureTask | None = None  # 本轮现场采集
    delivery_task: asyncio.Task | None = None  # 本轮相机结果交付
    recognition_task: asyncio.Task | None = None  # 本轮 OCR 任务
    result_storage_task: asyncio.Task | None = None  # 本轮正式结果保存
    failure_audit_task: asyncio.Task | None = None  # 本轮失败审计
    deadline_tasks: dict[EventType, asyncio.Task] = field(default_factory=dict)  # 本轮超时
    capture_result_pending: bool = False  # 相机帧结果尚未消费或丢弃
    ocr_result_pending: bool = False  # 识别结果中的帧尚未消费或丢弃


class MachineRuntime:
    def __init__(
        self,
        machine_config: MachineConfig,
        config: AppConfig,
        camera: Camera,
        frequency_adapter: FrequencyAdapter,
        text_recognizer: TextRecognizer,
        database: Database,
        publish_event: PublishEvent,
        notify_measurement_progress: Callable[[str, str, ProgressStage, ProgressStatus], None] | None,
        on_system_failure: Callable[[Exception], None],
        state_changed: asyncio.Event,
        notify_camera_state: Callable[[str, str, str], None] | None = None,
        notify_ocr_result: Callable[[str, str, tuple[str, ...]], None] | None = None,
        notify_cycle_closed: Callable[[str, str], None] | None = None,
        notify_machine_status: Callable[[str, str], None] | None = None,
        notify_machine_warning: Callable[[str, str], None] | None = None,
    ) -> None:
        """初始化单台机器运行时及其业务依赖。

        Args:
            machine_config: 机器身份配置。
            config: 采集、超时和存储配置。
            camera: 当前机器的相机适配器。
            frequency_adapter: 当前机器的频率接收适配器。
            text_recognizer: 系统共享的 OCR 处理器。
            database: 数据库访问对象。
            publish_event: 业务事件发送入口。
            notify_measurement_progress: 可选进度通知函数，接收机器编号、Session ID、处理阶段和阶段状态。
            on_system_failure: 系统故障回调，把后台任务异常交给运行时处理。
            state_changed: 测量状态变化通知。
            notify_camera_state: 可选相机状态通知函数，接收机器编号、状态和原因。
            notify_ocr_result: 可选文字通知函数，接收机器编号、周期编号、正式识别文字。
            notify_cycle_closed: 可选测量关闭通知函数，接收机器编号和 Session ID。
            notify_machine_warning: 可选机器积压提示，接收机器编号和说明。
            notify_machine_status: 可选机器整体状态通知函数，接收机器编号和
                online、offline 或 fault。

        Returns:
            返回示例：
                None  # 本机事件队列、当前测量和后台任务引用已初始化
        """
        # 保存机器配置和公共运行配置。
        self.machine_config = machine_config
        self.config = config

        # 保存本机相机和频率适配器。
        self.camera = camera
        self.frequency_adapter = frequency_adapter

        # 保存共享 OCR 识别器、数据库和事件发送入口。
        self.text_recognizer = text_recognizer
        self.database = database
        self.publish_event = publish_event

        # 保存界面通知回调。
        self.notify_camera_state = notify_camera_state
        self.notify_measurement_progress = notify_measurement_progress
        self.notify_ocr_result = notify_ocr_result
        self.notify_cycle_closed = notify_cycle_closed
        self.notify_machine_status = notify_machine_status
        self.notify_machine_warning = notify_machine_warning

        # 保存系统故障回调和状态变化通知。
        self.on_system_failure = on_system_failure
        self.state_changed = state_changed

        # 创建本机事件队列，事件按进入顺序处理。
        self.queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(config.event_queue_capacity)

        # 登记唯一现场周期和所有未完整回收的后台周期。
        self.active_session_id: str | None = None
        self.cycles: dict[str, CycleContext] = {}
        self.waiting_cycle_reset = False

        # 登记机器故障、启动状态和有界拒收审计任务。
        self.machine_failure_reason: str | None = None
        self.initialized = False
        self.rejected_start_audit_task: asyncio.Task | None = None

    @property
    def active_session(self) -> MeasurementSession | None:
        """取得当前占用现场的测量。

        Args:
            无外部参数。

        Returns:
            返回示例：
                session  # 占用现场的业务周期
                None  # 现场空闲，后台周期仍可存在
        """
        cycle = self.cycles.get(self.active_session_id)
        return cycle.session if cycle is not None else None

    @property
    def overall_status(self) -> MachineOverallStatus:
        """返回当前机器用于实时监测展示的整体状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                MachineOverallStatus.OFFLINE  # 尚未初始化或相机不可用
                MachineOverallStatus.FAULT  # 初始化后已登记机器故障
                MachineOverallStatus.ONLINE  # 初始化后相机可用且没有机器故障
        """
        # 未初始化或已释放资源的机器显示离线。
        if not self.initialized:
            return MachineOverallStatus.OFFLINE

        # 已登记机器级故障的机器显示故障。
        if self.machine_failure_reason is not None:
            return MachineOverallStatus.FAULT

        # 尚未建立可用相机连接的机器显示离线。
        if not self.camera.available:
            return MachineOverallStatus.OFFLINE

        return MachineOverallStatus.ONLINE

    def notify_overall_status(self) -> None:
        """把当前机器整体状态发送给界面。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 已发送整体状态，未配置通知时直接返回
        """
        # 未配置界面通知时跳过发送。
        if self.notify_machine_status is None:
            return

        # 发送机器编号和当前整体状态。
        self.notify_machine_status(
            self.machine_config.machine_id,
            self.overall_status.value,
        )

    async def listen_events(self) -> None:
        """持续监听本机事件队列，按顺序处理事件并反馈处理结果。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 持续运行直到任务被取消
        """
        while True:
            # 等待本机的下一个事件。
            event = await self.queue.get()
            try:
                # 处理当前事件。
                await self.handle_event(event)

                # 如果有人在等待处理结果，通知本次事件已经处理完成。
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_result(None)
            except Exception as error:
                # 处理失败时，把异常返回给等待方，然后继续抛出。
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.set_exception(error)
                raise
            finally:
                # 通知运行状态发生变化，并标记当前事件已经处理完成。
                self.state_changed.set()
                self.queue.task_done()

                # 清空当前事件引用。
                event = None

    async def wait_until_event_queue_drained(self) -> None:
        """等待本机事件队列中已提交的事件全部处理结束。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 队列中不再有等待处理和正在处理的事件
        """
        # 等待队列中所有已提交的事件处理完成。
        await self.queue.join()

    def discard_pending_events(self) -> None:
        """移除队列中尚未处理的事件，并取消等待这些事件的任务。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 队列已清空，等待这些事件处理结果的任务已取消
        """
        # 逐条移除还没有开始处理的事件。
        while not self.queue.empty():
            pending_event = self.queue.get_nowait()

            # 如果有人在等待处理结果，同时取消这次等待。
            if pending_event.acknowledgement is not None and not pending_event.acknowledgement.done():
                pending_event.acknowledgement.cancel()

            # 更新队列的未完成任务计数。
            self.queue.task_done()

            # 丢弃所属帧载荷并检查容量回收。
            self.discard_event_payload(pending_event)
            pending_event = None

    async def handle_machine_start(self) -> None:
        """检查接收条件，创建本轮测量档案并启动采集窗口和超时任务。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 启动本轮测量，或在不满足接收条件时提前结束
        """
        # 现场仍被当前周期占用时，忽略重复启动信号。
        if self.active_session is not None:
            logger.warning(
                "%s 收到启动信号，但当前测量尚未结束，已忽略本次启动 "
                "machine_id=%s active_session_id=%s",
                self.machine_config.machine_name,
                self.machine_config.machine_id,
                self.active_session.session_id,
            )
            return

        # 机器已经发生故障时，忽略新的启动信号。
        if self.machine_failure_reason is not None:
            logger.warning(
                "%s 收到启动信号，但机器处于故障状态，已忽略本次启动 "
                "machine_id=%s reason=%s",
                self.machine_config.machine_name,
                self.machine_config.machine_id,
                self.machine_failure_reason,
            )
            return

        # 还在等待现场复位时，忽略新的启动信号。
        if self.waiting_cycle_reset:
            logger.warning(
                "%s 收到启动信号，但仍在等待现场关闭复位，暂不启动新测量 "
                "machine_id=%s",
                self.machine_config.machine_name,
                self.machine_config.machine_id,
            )
            return

        # 相机不可用或仍在采集时，不能开始新的测量。
        if not self.camera.available or self.camera.is_capturing:
            # 相机不可用时同步机器整体状态。
            if not self.camera.available:
                self.notify_overall_status()

            # 等待现场关闭信号完成复位。
            self.waiting_cycle_reset = True

            # 记录本次启动失败时的相机状态。
            logger.error(
                "%s 收到启动信号，但相机当前不可用于新测量，本轮未启动 "
                "machine_id=%s camera_available=%s camera_capturing=%s",
                self.machine_config.machine_name,
                self.machine_config.machine_id,
                self.camera.available,
                self.camera.is_capturing,
            )
            return

        # 达到周期容量时拒收本次启动并等待现场关闭复位。
        if len(self.cycles) >= self.config.max_inflight_cycles:
            self.waiting_cycle_reset = True
            message = "后台处理积压，本次启动未采集，请暂停换带"
            logger.warning(
                "%s %s machine_id=%s capacity=%s occupied=%s",
                self.machine_config.machine_name,
                message,
                self.machine_config.machine_id,
                self.config.max_inflight_cycles, len(self.cycles),
            )
            if self.notify_machine_warning is not None:
                self.notify_machine_warning(self.machine_config.machine_id, message)
            if self.rejected_start_audit_task is None:
                # 只保留一个拒收审计任务。
                self.rejected_start_audit_task = asyncio.create_task(
                    run_blocking_operation(
                        self.database.save_abnormal_event,
                        message,
                        machine_id=self.machine_config.machine_id,
                        payload={
                            "capacity": self.config.max_inflight_cycles,
                            "occupied": len(self.cycles),
                        },
                    )
                )
                self.rejected_start_audit_task.add_done_callback(
                    self.handle_rejected_start_audit_finished
                )
            return

        # 创建新的测量周期，并生成 Session ID、采集 ID 和开始时间。
        session = MeasurementSession(
            session_id=uuid4().hex,
            machine_id=self.machine_config.machine_id,
            camera_serial=self.machine_config.camera_serial,
            frequency_meter_serial=self.machine_config.frequency_meter_serial,
            capture_id=uuid4().hex,
            start_time=datetime.now(timezone.utc).isoformat(),
            capture_start_time=asyncio.get_running_loop().time(),
        )

        # 先登记本轮上下文和现场占用。
        cycle = CycleContext(session)
        self.cycles[session.session_id] = cycle
        self.active_session_id = session.session_id

        # 记录测量开始。
        logger.info(
            "%s 开始新的测量 machine_id=%s session_id=%s "
            "unfinished_ocr_tasks=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            sum(cycle.recognition_task is not None for cycle in self.cycles.values()),
        )

        # 启动相机采集。
        cycle.capture_result_pending = True
        cycle.capture_task, cycle.delivery_task = self.camera.start_capture(
            session.session_id,
            session.capture_start_time,
        )
        cycle.delivery_task.add_done_callback(
            lambda task: self.handle_delivery_finished(task, session.session_id)
        )

        # 让后续频率读数归到当前测量。
        self.frequency_adapter.active_session_id = session.session_id

        # 通知界面测量已经开始，相机采集和频率采集正在进行。
        if self.notify_measurement_progress is not None:
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.SESSION_START,
                ProgressStatus.SUCCESS,
            )
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.IMAGE_CAPTURE,
                ProgressStatus.RUNNING,
            )
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.FREQUENCY_COLLECTION,
                ProgressStatus.RUNNING,
            )

        # 启动本轮测量超时任务。
        self.schedule_timeout(session, EventType.CYCLE_TIMEOUT, self.config.max_cycle_open_ms)

    async def handle_machine_close(
        self,
        interrupted: bool = False,
        capture_stop_time: float | None = None,
        close_event: RuntimeEvent | None = None,
        failure_reason: str = "测量周期中断",
    ) -> None:
        """结束本轮采集，确定最终频率，并检查本轮测量是否可以完成。

        Args:
            interrupted: False 表示正常 CLOSE；True 表示故障、超时或退出中断。
            capture_stop_time: 关闭信号接收时的单调时间，省略时取当前时间。
            close_event: 触发本次关闭的事件，省略时表示关闭没有事件来源。
            failure_reason: 中断关闭时记录的失败原因。

        Returns:
            返回示例：
                None  # 本轮相机和频率采集已结束，OCR 与结果保存按各自状态继续处理
        """
        # 已关闭或未知周期的 CLOSE 不影响当前现场周期。
        if close_event is not None and close_event.session_id is not None:
            if close_event.session_id != self.active_session_id:
                return

        # 获取当前测量。
        session = self.active_session

        # 当前没有测量时，正常关闭信号只用于解除等待复位状态。
        if session is None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return

        # 当前测量已经处理过关闭信号时，不再重复结束频率采集。
        if session.capture_stop_time is not None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return

        # 记录本轮开始关闭；正常关闭不带失败原因。
        logger.info(
            "%s 开始结束当前测量 machine_id=%s session_id=%s "
            "interrupted=%s reason=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            interrupted,
            failure_reason if interrupted else None,
        )

        # 保存本轮采集停止时间。
        session.capture_stop_time = (
            capture_stop_time if capture_stop_time is not None else asyncio.get_running_loop().time()
        )

        # 通知界面当前测量已经收到关闭信号。
        if self.notify_cycle_closed is not None:
            self.notify_cycle_closed(session.machine_id, session.session_id)

        # 异常中断后，等待现场真正关闭后再允许开始下一轮测量。
        self.waiting_cycle_reset = interrupted

        # 停止把后续频率读数归到本轮测量。
        if self.frequency_adapter.active_session_id == session.session_id:
            self.frequency_adapter.active_session_id = None

        # 根据中断状态、频率状态和已有读数确定最终频率。
        if interrupted or session.frequency_state == FrequencyState.FAILED:
            # 中断或频率采集失败时不生成最终频率，已经收到的读数仍然保留。
            session.final_frequency = None
            session.frequency_state = FrequencyState.FAILED
        elif session.measurement_frequencies:
            # 正常结束且已有有效读数时，最后一条读数作为最终频率。
            session.final_frequency = session.measurement_frequencies[-1]
            session.frequency_state = FrequencyState.SUCCESS
        else:
            # 正常结束但没有有效读数时，频率采集按失败处理。
            session.frequency_state = FrequencyState.FAILED
            session.errors.append("FREQUENCY_NO_VALID_MEASUREMENT")

        # 记录本轮最终频率。
        logger.info(
            "%s 频率采集已结束，确定本轮最终频率 machine_id=%s session_id=%s "
            "reading_count=%s result=%s final_frequency_hz=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            len(session.measurement_frequencies),
            session.frequency_state.value,
            session.final_frequency.value_hz if session.final_frequency is not None else None,
        )

        # 通知界面频率采集的最终状态。
        if self.notify_measurement_progress is not None:
            # 根据频率结果确定界面状态。
            frequency_progress_status = (
                ProgressStatus.SUCCESS
                if session.frequency_state == FrequencyState.SUCCESS
                else ProgressStatus.FAILED
            )

            # 更新界面的频率采集状态。
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.FREQUENCY_COLLECTION,
                frequency_progress_status,
            )

        # 取消所属周期的现场关闭超时，定向等待本轮停流。
        cycle = self.cycles[session.session_id]
        deadline = cycle.deadline_tasks.get(EventType.CYCLE_TIMEOUT)
        if deadline is not None:
            deadline.cancel()
        await self.camera.inform_capture_workflow_stop(cycle.capture_task)

        # 停流后解除现场占用，后台资源继续由本轮上下文持有。
        if self.active_session_id == session.session_id:
            self.active_session_id = None
        if interrupted:
            await self.handle_session_failure(session, failure_reason)
        else:
            await self.try_finalize(session)
        self.release_finished_session(session.session_id)
        self.state_changed.set()

    async def handle_event(self, event: RuntimeEvent) -> None:
        """处理所属周期事件并回收已消费的相机载荷。

        Args:
            event: 带有机器与周期编号的业务事件。

        Returns:
            返回示例：
                None  # 事件已处理，所属载荷消费状态已更新
        """
        try:
            await self.process_event(event)
        finally:
            self.discard_event_payload(event)

    async def process_event(self, event: RuntimeEvent) -> None:
        """按事件类型处理机器信号和测量数据，并检查本轮是否完成。

        Args:
            event: 待处理的测量事件，包含事件身份、来源信息和业务数据。

        Returns:
            返回示例：
                None  # 完成业务处理，或忽略不属于当前测量的事件
        """
        # 先处理不依赖当前 Session 的机器级事件。
        match event.event_type:
            # 处理启动信号。
            case EventType.MACHINE_STARTED:
                await self.handle_machine_start()
                return

            # 处理正常关闭信号。
            case EventType.MACHINE_CLOSED:
                await self.handle_machine_close(capture_stop_time=event.received_monotonic, close_event=event)
                return

            # 系统退出时中断当前测量。
            case EventType.SHUTDOWN:
                await self.handle_machine_close(interrupted=True)
                return

            # IO 通信中断时，结束仍在采集的当前测量。
            case EventType.IO_INTERRUPTED:
                if (
                    event.session_id is not None
                    and event.session_id != self.active_session_id
                ):
                    return
                session = self.active_session
                # IO 轮询虽然已经判断过一次，这里仍再次确认当前测量尚未关闭。
                if session is not None and session.capture_stop_time is None:
                    await self.handle_machine_close(
                        interrupted=True, failure_reason="IO 通信中断"
                    )

                # IO 通信恢复后，需要重新确认现场状态才能开始下一轮测量。
                self.waiting_cycle_reset = True
                return

        # 没有 Session ID 的频率读数无法确定属于哪次测量，只记录异常。
        if event.event_type == EventType.FREQUENCY_MEASURED and not event.session_id:
            await run_blocking_operation(
                self.database.save_abnormal_event,
                "频率读数缺少 Session ID",
                event,
            )
            return

        # 按事件编号取得所属后台周期。
        cycle = self.cycles.get(event.session_id)
        session = cycle.session if cycle is not None else None

        # 未知周期事件直接忽略，避免旧事件影响当前测量。
        if session is None:
            logger.warning(
                "%s 收到不属于当前测量周期的事件，已忽略 machine_id=%s "
                "event_session_id=%s active_session_id=%s event_type=%s",
                self.machine_config.machine_name,
                event.machine_id,
                event.session_id,
                self.active_session_id,
                event.event_type,
            )
            return

        # 当前测量发生相机故障时，按机器故障处理。
        if event.event_type == EventType.CAPTURE_FAILED:
            # 所属周期仍保留时通知本机相机故障。
            if self.notify_camera_state is not None:
                self.notify_camera_state(
                    self.machine_config.machine_id, "相机故障", event.payload
                )

            if session.state == SessionState.RUNNING:
                # 通知界面本轮图像采集失败。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.IMAGE_CAPTURE,
                        ProgressStatus.FAILED,
                    )

                # 保存相机返回的具体错误。
                session.errors.append(event.payload)

            # 标记机器故障，并结束仍在运行的当前测量。
            await self.handle_machine_failure(session, "相机采集失败")
            return

        # 收到测量超时时，只处理尚未关闭的当前测量。
        if event.event_type == EventType.CYCLE_TIMEOUT:
            if (
                self.active_session_id == session.session_id
                and session.capture_stop_time is None
            ):
                await self.handle_machine_close(
                    interrupted=True,
                    failure_reason="测量周期超时",
                )
            return

        # 当前测量已经结束时，忽略之后到达的结果。
        if session.state != SessionState.RUNNING:
            logger.warning(
                "%s 收到已经结束测量周期的迟到结果，已忽略 "
                "machine_id=%s session_id=%s session_state=%s event_type=%s",
                self.machine_config.machine_name,
                session.machine_id,
                session.session_id,
                session.state.value,
                event.event_type,
            )
            return

        # 处理当前 Session 的采集、OCR 和频率事件。
        match event.event_type:
            # 处理相机采集完成事件。
            case EventType.CAPTURE_COMPLETED:
                # 只有尚未开始 OCR 时才接收这次采集结果。
                if session.ocr_state != OCRState.WAITING:
                    return

                # 保存本轮采集统计。
                capture_result = event.payload
                session.capture_summary = capture_result.statistics

                # 记录本轮采集帧数和统计信息。
                logger.info(
                    "%s 相机采集结果已进入测量流程 machine_id=%s session_id=%s "
                    "frame_count=%s capture_summary=%s",
                    self.machine_config.machine_name,
                    session.machine_id,
                    session.session_id,
                    len(capture_result.frames),
                    capture_result.statistics,
                )

                # 本轮没有采到图片时，通知界面采集失败。
                if not capture_result.frames:
                    if self.notify_measurement_progress is not None:
                        self.notify_measurement_progress(
                            session.machine_id,
                            session.session_id,
                            ProgressStage.IMAGE_CAPTURE,
                            ProgressStatus.FAILED,
                        )

                    # 本轮没有采到图片时，按测量失败处理。
                    await self.handle_session_failure(session, "本轮未采集到图像")
                    return

                # 通知界面图像采集完成，OCR 开始。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.IMAGE_CAPTURE,
                        ProgressStatus.SUCCESS,
                    )
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.CHARACTER_RECOGNITION,
                        ProgressStatus.RUNNING,
                    )

                # 将 OCR 状态改为识别中。
                session.ocr_state = OCRState.RUNNING

                # 登记本轮 OCR 任务和所属周期的完成回调。
                task = asyncio.create_task(self.run_ocr_pipeline(session, capture_result.frames))
                cycle.recognition_task = task
                task.add_done_callback(
                    lambda task: self.handle_recognition_task_finished(
                        task,
                        session.session_id,
                    )
                )
                logger.info(
                    "%s 本轮OCR流程已启动 machine_id=%s session_id=%s",
                    self.machine_config.machine_name,
                    session.machine_id,
                    session.session_id,
                )
                return

            # 处理 OCR 完成事件。
            case EventType.OCR_COMPLETED:
                # 只有 OCR 仍在处理中时才接收结果。
                if session.ocr_state != OCRState.RUNNING:
                    return

                # 保存 OCR 结果，并将 OCR 状态改为已完成。
                session.ocr_result = event.payload
                session.ocr_state = OCRState.COMPLETED

                # 把本轮最终识别文字发送给界面。
                if self.notify_ocr_result is not None:
                    self.notify_ocr_result(
                        session.machine_id,
                        session.session_id,
                        session.ocr_result.recognized_lines,
                    )

                # 通知界面 OCR 已完成。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.CHARACTER_RECOGNITION,
                        ProgressStatus.SUCCESS,
                    )

                # 取出本轮 OCR 结果超时任务。
                deadline = cycle.deadline_tasks.get(EventType.OCR_TIMEOUT)

                # 如果超时任务还在，取消它。
                if deadline is not None:
                    deadline.cancel()

            # 处理 OCR 排队超时。
            case EventType.OCR_LOCK_WAIT_TIMEOUT:
                # 只有 OCR 仍在处理中时才处理这次排队超时。
                if session.ocr_state != OCRState.RUNNING:
                    return

                # 通知界面 OCR 失败，并按测量失败处理当前周期。
                session.ocr_state = OCRState.FAILED
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.CHARACTER_RECOGNITION,
                        ProgressStatus.FAILED,
                    )
                await self.handle_session_failure(session, "OCR 识别资源等待超时")
                return

            # 处理 OCR 执行失败或结果等待超时。
            case EventType.OCR_FAILED | EventType.OCR_TIMEOUT:
                # OCR 已经结束时，忽略重复到达的失败或超时事件。
                if session.ocr_state not in {OCRState.WAITING, OCRState.RUNNING}:
                    return

                # OCR 超时时记录当前后台任务情况。
                if event.event_type == EventType.OCR_TIMEOUT:
                    logger.info(
                        "%s OCR处理超过结果等待期限，当前测量将按超时处理 "
                        "machine_id=%s session_id=%s timeout_ms=%s "
                        "current_task_exists=%s unfinished_ocr_tasks=%s",
                        self.machine_config.machine_name,
                        session.machine_id,
                        session.session_id,
                        self.config.ocr_result_timeout_ms,
                        cycle.recognition_task is not None,
                        sum(
                            cycle.recognition_task is not None
                            for cycle in self.cycles.values()
                        ),
                    )

                # 更新 OCR 状态；执行失败时同时保存错误信息。
                session.ocr_state = OCRState.TIMED_OUT if event.event_type == EventType.OCR_TIMEOUT else OCRState.FAILED
                if event.event_type == EventType.OCR_FAILED:
                    session.errors.append(event.payload)

                # 通知界面 OCR 失败。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.CHARACTER_RECOGNITION,
                        ProgressStatus.FAILED,
                    )

            # 处理新的有效频率读数。
            case EventType.FREQUENCY_MEASURED:
                await self.handle_frequency_measured(session, event)
                return

            # 未识别的事件只记录异常，不参与当前测量。
            case _:
                await run_blocking_operation(
                    self.database.save_abnormal_event, "未知事件类型", event
                )
                return

        # 事件处理完成后，统一检查本轮是否失败或可以保存结果。
        await self.try_finalize(session)

    async def handle_machine_failure(
        self, session: MeasurementSession, failure_reason: str
    ) -> None:
        """记录机器故障，并结束仍在运行的当前测量。

        Args:
            session: 发生设备故障的当前测量。
            failure_reason: 本机设备故障原因。

        Returns:
            返回示例：
                None  # 机器故障已记录，运行中的测量已按失败处理
        """
        # 保存机器故障原因。
        self.machine_failure_reason = failure_reason

        # 发布登记故障后的机器整体状态。
        self.notify_overall_status()

        # 记录机器故障日志。
        logger.error(
            "%s 发生机器故障 machine_id=%s session_id=%s reason=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            failure_reason,
        )

        # 当前测量已经结束时，不再重复处理失败。
        if session.state != SessionState.RUNNING:
            return

        # 当前测量仍在运行时，按测量失败流程清理。
        await self.handle_session_failure(session, failure_reason)

    async def handle_session_failure(
        self,
        session: MeasurementSession,
        failure_reason: str,
        *,
        system_error: Exception | None = None,
    ) -> None:
        """清理失败测量并启动失败记录保存，现场关闭且保存结束后释放当前 Session。

        Args:
            session: 处理失败、中断或保存失败的当前测量。
            failure_reason: 本轮失败的原因描述。
            system_error: 已发生的系统级异常，失败记录保存结束后优先上报。

        Returns:
            返回示例：
                None  # 当前测量已清理，失败记录由本机后台任务继续保存
        """
        cycle = self.cycles.get(session.session_id)
        if cycle is None or session.state in {
            SessionState.FAILED, SessionState.COMMITTED,
        }:
            if system_error is not None:
                self.on_system_failure(system_error)
            return

        # 保存本轮失败原因。
        session.errors.append(failure_reason)

        # 将当前测量标记为失败，并记录结束时间。
        session.state = SessionState.FAILED
        session.finish_time = datetime.now(timezone.utc).isoformat()

        # 记录当前测量的失败信息。
        logger.error(
            "%s 当前测量失败 machine_id=%s session_id=%s errors=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            session.errors,
        )

        # 仅取消所属 OCR，实际任务结束前仍占用本轮容量。
        if cycle.recognition_task is not None:
            cycle.recognition_task.cancel()

        # 清空本轮 OCR 结果。
        session.ocr_result = None

        # 取消本轮超时任务；如果现场还没有关闭，继续保留测量周期超时任务。
        for event_type in tuple(cycle.deadline_tasks):
            if session.capture_stop_time is None and event_type == EventType.CYCLE_TIMEOUT:
                continue
            cycle.deadline_tasks[event_type].cancel()

        # 仅清除仍属于本轮的现场频率归属。
        if self.frequency_adapter.active_session_id == session.session_id:
            self.frequency_adapter.active_session_id = None

        # 如果频率采集还没有结束，将它标记为失败。
        if session.frequency_state == FrequencyState.RUNNING:
            session.frequency_state = FrequencyState.FAILED

        # 在后台保存失败记录，保存结束前保留当前测量。
        cycle.failure_audit_task = asyncio.create_task(run_blocking_operation(
            self.database.save_abnormal_event,
            failure_reason,
            machine_id=session.machine_id,
            session_id=session.session_id,
            payload={"session_errors": list(session.errors)},
        ))

        # 停止本轮相机采集；停止异常或取消时仍回收失败记录任务。
        try:
            if self.active_session_id == session.session_id:
                await self.camera.inform_capture_workflow_stop(cycle.capture_task)
        finally:
            # 相机停止流程返回后，再允许保存回调释放当前测量或上报故障。
            cycle.failure_audit_task.add_done_callback(
                lambda task: self.handle_failure_audit_finished(task, session, system_error)
            )

    def handle_failure_audit_finished(
        self,
        task: asyncio.Task[None],
        session: MeasurementSession,
        system_error: Exception | None,
    ) -> None:
        """回收失败记录保存任务，优先上报原始故障并检查测量释放条件。

        Args:
            task: 已结束的失败记录保存任务。
            session: 失败记录所属的测量周期。
            system_error: 调用方已有的系统级故障，未发生时为 None。

        Returns:
            返回示例：
                None  # 保存任务已回收，故障已上报且当前测量已检查释放条件
        """
        # 清空已结束的失败记录保存任务引用。
        cycle = self.cycles.get(session.session_id)
        if cycle is not None and cycle.failure_audit_task is task:
            cycle.failure_audit_task = None

        # 读取保存异常，意外取消也按系统故障处理。
        audit_error = RuntimeError("测量失败记录保存任务意外取消") if task.cancelled() else task.exception()
        if audit_error is not None:
            logger.error(
                "%s 保存测量失败记录时发生异常 machine_id=%s session_id=%s",
                self.machine_config.machine_name,
                session.machine_id,
                session.session_id,
                exc_info=(type(audit_error), audit_error, audit_error.__traceback__),
            )

        # 保留调用方的原始系统级故障，审计异常不覆盖它。
        escalated_error = system_error if system_error is not None else audit_error
        if escalated_error is not None:
            self.on_system_failure(escalated_error)

        # 保存结束后检查测量释放条件，并通知状态等待方。
        self.release_finished_session(session.session_id)
        self.state_changed.set()

    def release_finished_session(self, session_id: str) -> None:
        """回收离开现场且业务、线程与帧交付全部结束的所属周期。

        Args:
            session_id: 待检查的周期编号。

        Returns:
            返回示例：
                None  # 条件满足时移除所属上下文并归还容量
        """
        cycle = self.cycles.get(session_id)
        if cycle is None or self.active_session_id == session_id:
            return
        if cycle.session.state not in {SessionState.COMMITTED, SessionState.FAILED}:
            return

        # 线程、交付和待消费帧仍占用所属容量。
        tasks = (
            cycle.delivery_task,
            cycle.recognition_task,
            cycle.result_storage_task,
            cycle.failure_audit_task,
        )
        if cycle.capture_result_pending or cycle.ocr_result_pending:
            return
        # 各任务的完成回调清空所属引用后才回收上下文。
        if any(task is not None for task in tasks):
            return
        if cycle.deadline_tasks:
            return

        # 清除所属图片引用和运行上下文。
        cycle.session.ocr_result = None
        del self.cycles[session_id]

        # 记录所属周期完整回收并通知状态等待方。
        logger.info(
            "%s 当前测量周期已结束，运行时状态已释放 machine_id=%s session_id=%s final_state=%s",
            self.machine_config.machine_name,
            cycle.session.machine_id,
            session_id,
            cycle.session.state.value,
        )
        self.state_changed.set()

    def discard_event_payload(self, event: RuntimeEvent) -> None:
        """登记相机事件已消费或丢弃并检查所属周期回收。

        Args:
            event: 已处理或退出时丢弃的业务事件。

        Returns:
            返回示例：
                None  # 所属帧载荷不再等待事件消费
        """
        cycle = self.cycles.get(event.session_id)
        if cycle is None:
            return

        # 清除已消费或丢弃的相机帧及识别帧标志。
        if event.event_type in {EventType.CAPTURE_COMPLETED, EventType.CAPTURE_FAILED}:
            cycle.capture_result_pending = False
        if event.event_type == EventType.OCR_COMPLETED:
            cycle.ocr_result_pending = False
        self.release_finished_session(cycle.session.session_id)

    def handle_delivery_finished(self, task: asyncio.Task, session_id: str) -> None:
        """回收所属交付任务，保留仍在队列里的帧载荷占用。

        Args:
            task: 已结束的相机交付任务。
            session_id: 所属周期编号。

        Returns:
            返回示例：
                None  # 所属交付已回收，新轮采集引用保持不变
        """
        cycle = self.cycles.get(session_id)
        if cycle is not None and cycle.delivery_task is task:
            cycle.delivery_task = None
            if task.cancelled() or task.exception() is not None:
                cycle.capture_result_pending = False
        self.release_finished_session(session_id)
        self.state_changed.set()

    def handle_deadline_finished(
        self,
        task: asyncio.Task,
        session_id: str,
        event_type: EventType,
    ) -> None:
        """回收所属周期已结束的超时任务。

        Args:
            task: 已发送事件或取消的超时任务。
            session_id: 所属周期编号。
            event_type: 所属超时类型。

        Returns:
            返回示例：
                None  # 仅移除仍对应该任务的超时引用
        """
        cycle = self.cycles.get(session_id)
        if cycle is not None and cycle.deadline_tasks.get(event_type) is task:
            cycle.deadline_tasks.pop(event_type)
        if not task.cancelled() and task.exception() is not None:
            self.on_system_failure(task.exception())
        self.release_finished_session(session_id)
        self.state_changed.set()

    def handle_recognition_task_finished(
        self,
        task: asyncio.Task,
        session_id: str,
    ) -> None:
        """回收所属 OCR 任务并上报未分类异常。

        Args:
            task: 实际线程已收尾的 OCR 任务。
            session_id: 所属周期编号。

        Returns:
            返回示例：
                None  # 所属 OCR 已回收，不修改其他周期
        """
        cycle = self.cycles.get(session_id)
        if cycle is not None and cycle.recognition_task is task:
            cycle.recognition_task = None
        if not task.cancelled() and task.exception() is not None:
            self.on_system_failure(task.exception())
        logger.info(
            "%s OCR后台任务已结束并回收 machine_id=%s session_id=%s cancelled=%s",
            self.machine_config.machine_name,
            self.machine_config.machine_id,
            session_id,
            task.cancelled(),
        )
        self.release_finished_session(session_id)
        self.state_changed.set()

    def handle_rejected_start_audit_finished(self, task: asyncio.Task) -> None:
        """回收单个拒收审计任务并上报审计故障。

        Args:
            task: 已结束的拒收审计任务。

        Returns:
            返回示例：
                None  # 拒收审计槽已清空，异常交给系统故障入口
        """
        if self.rejected_start_audit_task is task:
            self.rejected_start_audit_task = None
        error = RuntimeError("启动拒收审计任务意外取消") if task.cancelled() else task.exception()
        if error is not None:
            self.on_system_failure(error)
        self.state_changed.set()

    async def release_resources(self, shutdown_reason: str) -> None:
        """停流并等待全部周期的线程、保存、审计与交付收尾。

        Args:
            shutdown_reason: 退出时未完成业务周期的失败原因。

        Returns:
            返回示例：
                None  # 所有周期资源已回收，机器已通知离线
        """
        # 定向关闭仍占用现场的周期。
        if self.active_session_id is not None:
            try:
                await self.handle_machine_close(
                    interrupted=True,
                    failure_reason=shutdown_reason,
                )
            except Exception as error:
                self.on_system_failure(error)

        # 等待所有相机交付，退出分发会丢弃对应帧载荷。
        try:
            await self.camera.stop()
        except Exception as error:
            self.on_system_failure(error)
        contexts = tuple(self.cycles.values())

        # 取消识别及超时，实际工作线程结束前继续持有上下文。
        tasks = []
        for cycle in contexts:
            tasks.extend(cycle.deadline_tasks.values())
            if cycle.recognition_task is not None:
                tasks.append(cycle.recognition_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # 已经开始的正式保存继续执行，不取消文件或事务。
        storage_tasks = [
            cycle.result_storage_task for cycle in contexts
            if cycle.result_storage_task is not None
        ]
        await asyncio.gather(*storage_tasks, return_exceptions=True)

        # 未结算轮分别判失败，已提交轮不重复审计。
        for cycle in contexts:
            if cycle.session.state in {
                SessionState.RUNNING, SessionState.SAVING_RESULT,
            }:
                await self.handle_session_failure(cycle.session, shutdown_reason)
        audit_tasks = [
            cycle.failure_audit_task for cycle in contexts
            if cycle.failure_audit_task is not None
        ]
        if self.rejected_start_audit_task is not None:
            audit_tasks.append(self.rejected_start_audit_task)
        await asyncio.gather(*audit_tasks, return_exceptions=True)

        # 丢弃退出后的剩余事件。
        self.discard_pending_events()

        # 所有实际任务结束后回收所属上下文及载荷。
        for cycle in contexts:
            cycle.capture_result_pending = False
            cycle.ocr_result_pending = False
            self.release_finished_session(cycle.session.session_id)
        # 结束机器初始化状态并通知离线。
        self.initialized = False
        self.notify_overall_status()

    async def run_ocr_pipeline(
        self, session: MeasurementSession, frames: tuple[CameraFrame, ...]
    ) -> None:
        """执行本轮 OCR 流程，包括图片准备、帧筛选、OCR 排队、识别和结果发送。

        Args:
            session: 当前测量，处理前检查其状态。
            frames: 本轮全部原始帧。

        Returns:
            返回示例：
                None  # 最终结果或失败原因通过事件发送
        """
        try:
            # 在线程中准备本轮图片，并筛选需要进入 OCR 的帧。
            measurement_frames, qualified_frames = await run_blocking_operation(
                self.text_recognizer.prepare_frames_for_ocr,
                session.session_id,
                session.capture_id,
                session.camera_serial,
                frames,
            )

            # 图片准备完成后清空原始 frames 引用。
            frames = ()

            # 图片准备完成后，再确认当前测量仍在运行。
            if session.state != SessionState.RUNNING:
                return

            # 没有合格帧时，直接生成需要人工复核的 OCR 结果。
            if not qualified_frames:
                result = self.text_recognizer.create_no_qualified_frames_result(
                    measurement_frames
                )
                event_type, payload = EventType.OCR_COMPLETED, result
            else:
                # 记录开始 OCR 排队的时间，用于统计实际等待时长。
                wait_started = time.monotonic()
                logger.info(
                    "%s OCR识别正在排队，等待前一任务完成 "
                    "machine_id=%s session_id=%s",
                    self.machine_config.machine_name,
                    session.machine_id,
                    session.session_id,
                )

                try:
                    # 在配置的等待时间内申请 OCR 使用权。
                    async with self.text_recognizer.acquire_ocr_access(
                        self.config.ocr_lock_wait_timeout_ms / 1000
                    ):
                        # 取得 OCR 使用权后记录实际排队时长。
                        logger.info(
                            "%s OCR排队结束 machine_id=%s session_id=%s "
                            "wait_seconds=%.3f",
                            self.machine_config.machine_name,
                            session.machine_id,
                            session.session_id,
                            time.monotonic() - wait_started,
                        )

                        # 排队期间当前测量可能已经结束，取得 OCR 使用权后再次确认状态。
                        if session.state != SessionState.RUNNING:
                            logger.info(
                                "%s OCR排队结束时本轮测量已失效，已取消识别 "
                                "machine_id=%s session_id=%s session_state=%s",
                                self.machine_config.machine_name,
                                session.machine_id,
                                session.session_id,
                                session.state.value,
                            )
                            return

                        # 取得 OCR 使用权后，才启动 OCR 结果等待超时。
                        self.schedule_timeout(
                            session,
                            EventType.OCR_TIMEOUT,
                            self.config.ocr_result_timeout_ms,
                        )

                        # 记录 OCR 真正开始处理的时间，用于统计识别耗时。
                        processing_started = time.monotonic()
                        logger.info(
                            "%s OCR开始识别 machine_id=%s session_id=%s timeout_ms=%s",
                            self.machine_config.machine_name,
                            session.machine_id,
                            session.session_id,
                            self.config.ocr_result_timeout_ms,
                        )

                        # 在线程中识别合格帧，并生成最终文字和证据图片。
                        try:
                            result = await run_blocking_operation(
                                self.text_recognizer.recognize_qualified_frames,
                                session.session_id,
                                measurement_frames,
                                qualified_frames,
                            )
                        except OCRProcessingError:
                            # OCR 执行失败时记录已经处理的时间。
                            logger.info(
                                "%s OCR识别执行失败 machine_id=%s session_id=%s "
                                "elapsed_seconds=%.3f",
                                self.machine_config.machine_name,
                                session.machine_id,
                                session.session_id,
                                time.monotonic() - processing_started,
                            )
                            raise

                        # OCR 完成后记录本次识别耗时。
                        logger.info(
                            "%s OCR识别完成 machine_id=%s session_id=%s "
                            "elapsed_seconds=%.3f",
                            self.machine_config.machine_name,
                            session.machine_id,
                            session.session_id,
                            time.monotonic() - processing_started,
                        )
                        event_type, payload = EventType.OCR_COMPLETED, result
                except OCRResourceWaitTimeoutError:
                    # OCR 排队超时后，发送排队超时事件给当前测量。
                    logger.warning(
                        "%s OCR排队等待超时，本轮识别失败 "
                        "machine_id=%s session_id=%s "
                        "wait_timeout_ms=%s",
                        self.machine_config.machine_name,
                        session.machine_id,
                        session.session_id,
                        self.config.ocr_lock_wait_timeout_ms,
                    )
                    await self.publish_event(RuntimeEvent(
                        EventType.OCR_LOCK_WAIT_TIMEOUT,
                        session.machine_id,
                        session.session_id,
                    ))
                    return
        except OCRProcessingError as error:
            # 图片准备或 OCR 出现已知错误时，发送 OCR 失败事件。
            logger.exception(
                "%s OCR流程处理失败 machine_id=%s session_id=%s",
                self.machine_config.machine_name,
                session.machine_id,
                session.session_id,
            )
            await asyncio.sleep(0)
            event_type, payload = EventType.OCR_FAILED, str(error)

        # 清空原始 frames 引用。
        frames = ()

        # 只有当前测量仍在运行时才发送 OCR 结果。
        if session.state == SessionState.RUNNING:
            # 登记识别结果中的帧载荷，消费或丢弃前继续占用容量。
            cycle = self.cycles[session.session_id]
            cycle.ocr_result_pending = event_type == EventType.OCR_COMPLETED
            try:
                await self.publish_event(RuntimeEvent(event_type, session.machine_id, session.session_id, payload))
            except asyncio.CancelledError:
                # 入队等待取消时丢弃本轮未交付的识别帧。
                cycle.ocr_result_pending = False
                raise
            except Exception:
                # OCR 结果发送失败时记录异常，并结束当前后台任务。
                logger.exception(
                    "%s OCR结果交付到测量流程失败 machine_id=%s session_id=%s",
                    self.machine_config.machine_name,
                    session.machine_id,
                    session.session_id,
                )
                raise

    async def handle_frequency_measured(
        self, session: MeasurementSession, event: RuntimeEvent
    ) -> None:
        """按收到的顺序保存本轮有效频率读数。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            返回示例：
                None  # 已保存有效频率，或记录迟到读数后忽略事件
        """
        # 本轮频率采集已经结束时，后续读数只记录为迟到数据。
        if session.frequency_state != FrequencyState.RUNNING:
            await run_blocking_operation(
                self.database.save_abnormal_event, "迟到的频率读数", event
            )
            return

        # 按收到的顺序保存本轮频率读数。
        session.measurement_frequencies.append(event.payload)

    async def try_finalize(self, session: MeasurementSession) -> None:
        """检查本轮测量是否完成，条件满足后启动结果保存任务。

        Args:
            session: 待检查完成条件的本轮测量档案。

        Returns:
            返回示例：
                None  # 条件不足时继续等待，否则已启动本轮结果保存
        """
        cycle = self.cycles.get(session.session_id)
        if cycle is None:
            return

        # 当前测量已经不在运行时，不再继续保存结果。
        if session.state != SessionState.RUNNING:
            return

        # OCR 已失败或超时时，按测量失败流程处理。
        if session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}:
            failure_reason = (
                "OCR 识别超时" if session.ocr_state == OCRState.TIMED_OUT
                else "OCR 识别执行失败"
            )
            await self.handle_session_failure(session, failure_reason)
            return

        # 关闭信号和 OCR 结果没有同时到齐时，继续等待。
        if session.capture_stop_time is None or session.ocr_state != OCRState.COMPLETED:
            return

        # 记录本轮测量结束时间。
        session.finish_time = datetime.now(timezone.utc).isoformat()

        # 读取本轮最终频率和 OCR 结果。
        final_frequency = session.final_frequency
        ocr_result = session.ocr_result

        # 汇总需要人工复核的原因。
        review_reasons = []
        if ocr_result.review_reason is not None:
            review_reasons.append(ocr_result.review_reason)
        if final_frequency is None:
            review_reasons.append("没有找到最终频率，请人工复核。")
        needs_review = bool(review_reasons)
        review_reason = "；".join(review_reasons) if review_reasons else None

        # 根据是否需要人工复核，选择要保存的证据图片。
        if ocr_result.review_reason is not None:
            evidence_frames = ocr_result.review_frames
        else:
            evidence_frames = ocr_result.selected_frames

        # 根据测量开始时间确定证据图片的日期目录。
        local_start_time = datetime.fromisoformat(session.start_time).astimezone()
        local_start_date = local_start_time.strftime("%Y%m%d")

        # 按日期、机器和 Session 生成本轮证据图片目录。
        evidence_directory = (
            self.config.evidence_directory / local_start_date
            / session.machine_id / session.session_id
        )

        # 生成本轮要写入数据库的测量记录。
        record = MeasurementRecord(
            machine_id=session.machine_id,
            session_id=session.session_id,
            start_time=session.start_time,
            finish_time=session.finish_time,
            recognized_lines=tuple(ocr_result.recognized_lines),
            final_frequency_hz=final_frequency.value_hz if final_frequency else None,
            measurement_frequencies=tuple(
                asdict(measurement) for measurement in session.measurement_frequencies
            ),
            evidence_directory=evidence_directory,
            needs_review=needs_review,
            review_reason=review_reason,
        )

        # 将当前测量状态改为正在保存结果。
        session.state = SessionState.SAVING_RESULT

        # 通知界面开始保存测量结果。
        if self.notify_measurement_progress is not None:
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.EVIDENCE_STORAGE,
                ProgressStatus.RUNNING,
            )

        # 清空 Session 中的 OCR 结果引用。
        session.ocr_result = None

        # 记录即将保存的证据图片数量和测量结果。
        logger.info(
            "%s 开始保存本轮测量结果 machine_id=%s session_id=%s "
            "evidence_frame_count=%s needs_review=%s final_frequency_hz=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            len(evidence_frames),
            needs_review,
            record.final_frequency_hz,
        )

        # 启动本轮保存任务，不阻塞机器事件和 DI 轮询。
        cycle.result_storage_task = asyncio.create_task(
            self.store_measurement_result(session, record, evidence_frames)
        )
        cycle.result_storage_task.add_done_callback(
            lambda task: self.handle_result_storage_finished(task, session.session_id)
        )

    def handle_result_storage_finished(
        self,
        task: asyncio.Task[None],
        session_id: str,
    ) -> None:
        """回收结果保存任务，上报未处理异常并检查测量释放条件。

        Args:
            task: 已结束的结果保存任务。
            session_id: 保存任务所属周期。

        Returns:
            返回示例：
                None  # 保存任务已回收，异常已上报且当前测量已检查释放条件
        """
        # 清空已结束的保存任务引用。
        cycle = self.cycles.get(session_id)
        if cycle is not None and cycle.result_storage_task is task:
            cycle.result_storage_task = None

        # 保存任务意外取消或失败时通知系统故障入口。
        if task.cancelled():
            self.on_system_failure(RuntimeError("测量结果保存任务意外取消"))
        else:
            error = task.exception()
            if error is not None:
                self.on_system_failure(error)

        # 释放已完成的测量，并通知状态等待方。
        self.release_finished_session(session_id)
        self.state_changed.set()

    async def store_measurement_result(
        self,
        session: MeasurementSession,
        record: MeasurementRecord,
        evidence_frames: tuple[MeasurementFrame, ...],
    ) -> None:
        """保存证据和测量记录，并更新所属测量的最终状态。

        Args:
            session: 当前正在保存结果的测量周期。
            record: 本轮正式测量记录。
            evidence_frames: 本轮需要保存的证据帧。

        Returns:
            返回示例：
                None  # 本轮结果已保存，或已按现有失败流程处理
        """
        # 在线程中保存证据图片，然后写入测量记录。
        try:
            await run_blocking_operation(
                self.save_evidence_images_and_measurement_record,
                record,
                evidence_frames,
            )
        except MvsError as error:
            # 保存证据图片编码错误。
            session.errors.append(str(error))

            # 通知界面测量结果保存失败。
            if self.notify_measurement_progress is not None:
                self.notify_measurement_progress(
                    session.machine_id,
                    session.session_id,
                    ProgressStage.EVIDENCE_STORAGE,
                    ProgressStatus.FAILED,
                )

            # 证据图片编码失败时，本轮测量按失败处理。
            await self.handle_session_failure(session, "证据图片编码失败")
            return
        except EvidenceWriteError as error:
            # 记录证据图片保存失败。
            logger.exception(
                "%s 保存证据图片失败 machine_id=%s session_id=%s",
                self.machine_config.machine_name,
                session.machine_id,
                session.session_id,
            )

            # 通知界面测量结果保存失败。
            if self.notify_measurement_progress is not None:
                self.notify_measurement_progress(
                    session.machine_id,
                    session.session_id,
                    ProgressStage.EVIDENCE_STORAGE,
                    ProgressStatus.FAILED,
                )

            # 证据图片写入失败时，先结束本轮测量，再按系统故障处理。
            await self.handle_session_failure(session, "证据图片保存失败", system_error=error)
            return
        except (CommitIntegrityConflictError, sqlite3.Error) as error:
            # 确定数据库保存失败的具体原因。
            failure_reason = (
                "测量记录提交冲突"
                if isinstance(error, CommitIntegrityConflictError)
                else "测量结果入库失败"
            )
            logger.exception(
                "%s 保存测量记录到数据库失败 machine_id=%s session_id=%s",
                self.machine_config.machine_name,
                session.machine_id,
                session.session_id,
            )

            # 通知界面测量结果保存失败。
            if self.notify_measurement_progress is not None:
                self.notify_measurement_progress(
                    session.machine_id,
                    session.session_id,
                    ProgressStage.EVIDENCE_STORAGE,
                    ProgressStatus.FAILED,
                )

            # 数据库保存失败时，先结束本轮测量，再按系统故障处理。
            await self.handle_session_failure(session, failure_reason, system_error=error)
            return

        # 将当前测量状态改为已保存。
        session.state = SessionState.COMMITTED

        # 通知界面测量结果保存完成。
        if self.notify_measurement_progress is not None:
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.EVIDENCE_STORAGE,
                ProgressStatus.SUCCESS,
            )

        # 记录本轮测量结果保存完成。
        logger.info(
            "%s 本轮测量结果保存完成 machine_id=%s session_id=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
        )

    def save_evidence_images_and_measurement_record(
        self,
        record: MeasurementRecord,
        evidence_frames: tuple[MeasurementFrame, ...],
    ) -> None:
        """逐张保存证据图片，再写入本轮测量记录。

        Args:
            record: 本轮测量的业务字段和证据图片目录。
            evidence_frames: 本轮需要保存的原始帧。

        Returns:
            返回示例：
                None  # 证据图片和测量记录均已保存
        """
        # 记录本次实际新建的证据图片路径。
        created_image_paths = []

        try:
            # 依次编码并保存本轮证据图片。
            self.encode_and_save_evidence_images(record, evidence_frames, created_image_paths)
        except Exception as error:
            # 保存失败时，删除本次已经新建的证据图片。
            for image_path in created_image_paths:
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("清理证据图片失败 path=%s", image_path)

            # 文件写入失败时，转换为统一的证据图片写入异常。
            if isinstance(error, OSError):
                raise EvidenceWriteError("证据图片写入失败") from error
            raise

        # 所有证据图片保存成功后，再写入测量记录。
        self.database.write_measurement_record(record)

    def encode_and_save_evidence_images(
        self,
        record: MeasurementRecord,
        evidence_frames: tuple[MeasurementFrame, ...],
        created_image_paths: list[Path],
    ) -> None:
        """逐张编码并保存本轮尚不存在的证据图片。

        Args:
            record: 本轮测量的业务字段和证据图片目录。
            evidence_frames: 本轮需要保存的原始帧。
            created_image_paths: 记录本次新建的图片路径，供失败时清理。

        Returns:
            返回示例：
                None  # 本轮证据图片已保存，新建路径已记录
        """
        # 逐张处理证据图片，已经存在的文件直接跳过。
        for frame in evidence_frames:
            image_path = record.evidence_directory / f"{frame.frame_id}.jpg"
            if image_path.exists():
                continue

            # 将相机帧编码为 JPG 图片。
            try:
                image_data = self.camera.sdk_camera.encode_image(frame.camera_frame)
            except MvsError:
                raise
            except Exception as error:
                raise ImageEncodingError("相机图片编码失败") from error

            # 记录本次新建的图片路径，保存失败时用于清理。
            created_image_paths.append(image_path)

            # 原子写入当前证据图片。
            save_evidence_image(image_data, image_path)

    def schedule_timeout(
        self, session: MeasurementSession, event_type: EventType, timeout_ms: int
    ) -> None:
        """为当前测量启动指定类型的超时任务。

        Args:
            session: 当前测量。
            event_type: 测量关闭或 OCR 超时事件类型。
            timeout_ms: 等待毫秒数。

        Returns:
            返回示例：
                None  # 超时任务已按事件类型保存
        """
        async def publish_timeout() -> None:
            """等待超时时间到达，然后发送带有原 Session ID 的超时事件。

            Args:
                无外部参数。

            Returns:
                返回示例：
                    None  # 超时事件已发送，或任务被提前取消
            """
            # 等待配置的超时时间。
            await asyncio.sleep(timeout_ms / 1000)

            # 发送带有原 Session ID 的超时事件。
            await self.publish_event(RuntimeEvent(event_type, session.machine_id, session.session_id))

        # 保存本轮这个类型的超时任务。
        cycle = self.cycles[session.session_id]
        task = asyncio.create_task(publish_timeout())
        cycle.deadline_tasks[event_type] = task
        task.add_done_callback(
            lambda finished: self.handle_deadline_finished(
                finished,
                session.session_id,
                event_type,
            )
        )
