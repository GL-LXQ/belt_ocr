"""定义单台机器运行时实例及其事件处理流程。"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from camera.camera import Camera
from config_util import MachineConfig, AppConfig
from frequency_adapter import FrequencyAdapter
from enums import EventType, FrequencyState, OCRState, ProgressStage, ProgressStatus, SessionState
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
    """标记证据图片编码阶段的未知异常。"""


class EvidenceWriteError(RuntimeError):
    """标记证据图片文件写入失败。"""


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
        notify_ocr_result: Callable[[str, str, tuple[str, ...], tuple[str, ...]], None] | None = None,
        notify_cycle_closed: Callable[[str, str], None] | None = None,
    ) -> None:
        """初始化单台机器运行时及其业务依赖。

        Args:
            machine_config: 机器身份配置。
            config: 采集、超时和存储配置。
            camera: 当前机器的相机适配器。
            frequency_adapter: 当前机器的频率接收适配器。
            text_recognizer: 三台机器共享的 OCR 处理器。
            database: 数据库访问对象。
            publish_event: 业务事件发送入口。
            notify_measurement_progress: 可选进度通知函数，接收机器编号、Session ID、处理阶段和阶段状态。
            on_system_failure: 系统故障回调，把识别任务异常交给运行时处理。
            state_changed: 测量状态变化通知。
            notify_camera_state: 可选相机状态通知函数，接收机器编号、状态和原因。
            notify_ocr_result: 可选文字通知函数，接收机器编号、Session ID、原文字和去空格文字。
            notify_cycle_closed: 可选测量关闭通知函数，接收机器编号和 Session ID。

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

        # 保存系统故障回调和状态变化通知。
        self.on_system_failure = on_system_failure
        self.state_changed = state_changed

        # 创建本机事件队列，事件按进入顺序处理。
        self.queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(config.event_queue_capacity)

        # 初始化当前测量周期和等待复位状态。
        self.current_session: MeasurementSession | None = None
        self.waiting_cycle_reset = False

        # 初始化机器故障状态。
        self.machine_failure_reason: str | None = None

        # 初始化超时任务和启动状态。
        self.deadline_tasks: dict[EventType, asyncio.Task[None]] = {}
        self.initialized = False

        # 保存当前 OCR 任务，并跟踪所有尚未结束的 OCR 任务。
        self.current_recognition_task: asyncio.Task | None = None
        self.unfinished_recognition_tasks: set[asyncio.Task[None]] = set()

    @property
    def acceptance_state(self) -> str:
        """返回本机当前是否可以开始新的测量。

        Args:
            无外部参数。

        Returns:
            返回示例：
                "READY"  # 可以开始新测量
                "INITIALIZING"  # 本机尚未完成启动准备
                "FAULT"  # 机器故障或相机不可用
                "WAIT_CYCLE_RESET"  # 等待现场复位
                "ACTIVE"  # 当前已有测量
                "DEGRADED"  # 相机仍被上一轮占用
        """
        # 启动准备尚未完成，暂不接收新的测量。
        if not self.initialized:
            return "INITIALIZING"

        # 机器故障或相机不可用时，不能开始新的测量。
        if self.machine_failure_reason is not None or not self.camera.available:
            return "FAULT"

        # 现场状态还没有复位时，不能开始新的测量。
        if self.waiting_cycle_reset:
            return "WAIT_CYCLE_RESET"

        # 当前已有测量正在进行，不能重复启动。
        if self.current_session is not None:
            return "ACTIVE"

        # 相机仍在上一轮采集中，不能开始新的测量。
        if self.camera.is_capturing:
            return "DEGRADED"

        # 以上条件都正常，可以开始新的测量。
        return "READY"

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

    async def handle_machine_start(self) -> None:
        """检查接收条件，创建本轮测量档案并启动采集窗口和超时任务。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 启动本轮测量，或在不满足接收条件时提前结束
        """
        # 当前测量尚未结束时，忽略新的启动信号。
        if self.current_session is not None:
            logger.warning(
                "%s 收到启动信号，但当前测量尚未结束，已忽略本次启动 "
                "machine_id=%s current_session_id=%s",
                self.machine_config.machine_name,
                self.machine_config.machine_id,
                self.current_session.session_id,
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

        # 保存为当前测量周期。
        self.current_session = session

        # 记录测量开始。
        logger.info(
            "%s 开始新的测量 machine_id=%s session_id=%s "
            "unfinished_ocr_tasks=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            len(self.unfinished_recognition_tasks),
        )

        # 启动相机采集。
        self.camera.start_capture(session.session_id, session.capture_start_time)

        # 相机结果处理结束后，再检查当前测量是否可以释放。
        self.camera.delivery_task.add_done_callback(lambda task: self.release_finished_session())

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
        # 如果关闭事件属于旧 Session，只记录异常，不影响当前测量。
        if (
            close_event is not None
            and close_event.session_id is not None
            and self.current_session is not None
            and close_event.session_id != self.current_session.session_id
        ):
            await run_blocking_operation(
                self.database.save_abnormal_event,
                "关闭信号与当前 Session 不匹配",
                close_event,
            )
            return

        # 获取当前测量。
        session = self.current_session

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
        self.frequency_adapter.active_session_id = None

        # 标记本轮频率采集已经结束，后续读数按迟到数据处理。
        session.frequency_window_sealed = True

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

        # 通知相机停止本轮采集和结果发送。
        await self.camera.inform_capture_workflow_stop()

        # 取出本轮测量超时任务。
        deadline_task = self.deadline_tasks.pop(EventType.CYCLE_TIMEOUT, None)

        # 如果超时任务还在，取消它。
        if deadline_task is not None:
            deadline_task.cancel()

        # 如果本轮已经失败，只检查当前测量是否可以释放。
        if session.state == SessionState.FAILED:
            self.release_finished_session()
            return

        # 如果是异常中断，按测量失败流程清理。
        if interrupted:
            await self.handle_session_failure(session, failure_reason)
            return

        # 正常关闭后检查 OCR 和频率是否都已完成，满足条件时保存结果。
        await self.try_finalize(session)

    async def handle_event(self, event: RuntimeEvent) -> None:
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
                session = self.current_session
                # IO 轮询虽然已经判断过一次，这里仍再次确认当前测量尚未关闭。
                if session is not None and session.capture_stop_time is None:
                    await self.handle_machine_close(
                        interrupted=True, failure_reason="IO 通信中断"
                    )

                # IO 通信恢复后，需要重新确认现场状态才能开始下一轮测量。
                self.waiting_cycle_reset = True
                return

        # 相机采集失败时，先通知界面相机发生故障。
        if event.event_type == EventType.CAPTURE_FAILED and self.notify_camera_state is not None:
            self.notify_camera_state(self.machine_config.machine_id, "相机故障", event.payload)

        # 没有 Session ID 的频率读数无法确定属于哪次测量，只记录异常。
        if event.event_type == EventType.FREQUENCY_MEASURED and not event.session_id:
            await run_blocking_operation(
                self.database.save_abnormal_event,
                "频率读数缺少 Session ID",
                event,
            )
            return

        # 获取当前测量。
        session = self.current_session

        # 事件不属于当前 Session 时直接忽略，避免旧事件影响当前测量。
        if session is None or event.session_id != session.session_id:
            logger.warning(
                "%s 收到不属于当前测量周期的事件，已忽略 machine_id=%s "
                "event_session_id=%s current_session_id=%s event_type=%s",
                self.machine_config.machine_name,
                event.machine_id,
                event.session_id,
                session.session_id if session is not None else None,
                event.event_type,
            )
            return

        # 当前测量发生相机故障时，按机器故障处理。
        if event.event_type == EventType.CAPTURE_FAILED:
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
            if session.capture_stop_time is None:
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

                # 启动本轮 OCR 任务，同时保存当前任务并加入未结束任务集合。
                task = asyncio.create_task(self.run_ocr_pipeline(session, capture_result.frames))
                self.current_recognition_task = task
                self.unfinished_recognition_tasks.add(task)
                task.add_done_callback(self.handle_recognition_task_finished)
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
                        session.ocr_result.ordered_lines,
                        session.ocr_result.normalized_lines,
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
                deadline = self.deadline_tasks.pop(EventType.OCR_TIMEOUT, None)

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
                        self.current_recognition_task is not None,
                        len(self.unfinished_recognition_tasks),
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
        """将本轮测量标记为失败并清理相关资源，现场尚未关闭时继续保留当前 Session。

        Args:
            session: 处理失败、中断或保存失败的当前测量。
            failure_reason: 本轮失败的原因描述。
            system_error: 已发生的系统级异常，本轮清理完成后优先上报。

        Returns:
            返回示例：
                None  # 当前测量已标记失败并清理，系统级异常已上报
        """
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

        # 保存本轮失败记录；如果保存失败，先完成当前测量清理，再上报系统故障。
        audit_error: Exception | None = None
        try:
            await run_blocking_operation(
                self.database.save_abnormal_event,
                failure_reason,
                machine_id=session.machine_id,
                session_id=session.session_id,
                payload={"session_errors": list(session.errors)},
            )
        except Exception as error:
            # 失败记录保存异常时，先记下这个异常。
            audit_error = error
            logger.exception(
                "%s 保存测量失败记录时发生异常 machine_id=%s session_id=%s",
                self.machine_config.machine_name,
                session.machine_id,
                session.session_id,
            )

        # 取消当前 OCR 任务并解除当前 Session 的任务引用；尚未结束的任务仍由后台任务集合继续跟踪。
        current_recognition_task = self.current_recognition_task
        if current_recognition_task is not None:
            self.current_recognition_task = None
            current_recognition_task.cancel()

        # 清空本轮 OCR 结果。
        session.ocr_result = None

        # 取消本轮超时任务；如果现场还没有关闭，继续保留测量周期超时任务。
        for event_type in tuple(self.deadline_tasks):
            if session.capture_stop_time is None and event_type == EventType.CYCLE_TIMEOUT:
                continue
            self.deadline_tasks.pop(event_type).cancel()

        # 停止把频率读数归到本轮，并停止接收本轮新的频率数据。
        self.frequency_adapter.active_session_id = None
        session.frequency_window_sealed = True

        # 如果频率采集还没有结束，将它标记为失败。
        if session.frequency_state == FrequencyState.RUNNING:
            session.frequency_state = FrequencyState.FAILED

        # 停止本轮相机采集和结果发送，然后检查当前测量能否释放。
        await self.camera.inform_capture_workflow_stop()
        self.release_finished_session()

        # 如果已经有系统级异常就上报它，否则上报失败记录的保存异常。
        escalated_error = system_error if system_error is not None else audit_error
        if escalated_error is not None:
            self.on_system_failure(escalated_error)

    def release_finished_session(self) -> None:
        """在测量已关闭、OCR 和相机任务结束后释放当前测量。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 条件满足时清空当前测量并通知等待方，否则继续等待
        """
        # 只有已经收到关闭信号的测量才能释放。
        session = self.current_session
        if session is None or session.capture_stop_time is None:
            return

        # 只有已经保存成功或已经失败的测量才能释放。
        if session.state not in {SessionState.COMMITTED, SessionState.FAILED}:
            return

        # 当前 Session 仍绑定 OCR 任务时继续等待。
        if self.current_recognition_task is not None:
            return

        # 相机采集结果还在处理时继续等待。
        if self.camera.delivery_task is not None:
            return

        # 清空 OCR 结果，并取消剩余的超时任务。
        session.ocr_result = None
        for task in self.deadline_tasks.values():
            task.cancel()
        self.deadline_tasks.clear()

        # 记录当前测量已经满足释放条件。
        logger.info(
            "%s 当前测量周期已结束，运行时状态已释放 "
            "machine_id=%s session_id=%s final_state=%s",
            self.machine_config.machine_name,
            session.machine_id,
            session.session_id,
            session.state.value,
        )

        # 清空当前测量，并通知等待状态变化的任务。
        self.current_session = None
        self.state_changed.set()

    def handle_recognition_task_finished(self, task: asyncio.Task) -> None:
        """处理已经结束的 OCR 任务，上报未处理异常，并检查当前测量是否可以释放。

        Args:
            task: 已结束或取消的本轮 OCR 任务。

        Returns:
            返回示例：
                None  # OCR 任务已回收，当前测量已检查是否可以释放
        """
        # 从未结束 OCR 任务集合中移除这个任务。
        self.unfinished_recognition_tasks.discard(task)

        # 记录 OCR 后台任务结束后的状态。
        logger.info(
            "%s OCR后台任务已结束并回收 machine_id=%s is_current=%s "
            "cancelled=%s remaining_ocr_tasks=%s",
            self.machine_config.machine_name,
            self.machine_config.machine_id,
            self.current_recognition_task is task,
            task.cancelled(),
            len(self.unfinished_recognition_tasks),
        )

        # 任务不是被取消结束时，检查是否有未处理异常并按系统故障处理。
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.on_system_failure(error)

        # 只有这个任务仍是当前 Session 的 OCR 任务时，才清空当前任务并检查是否可以释放当前测量。
        if self.current_recognition_task is task:
            self.current_recognition_task = None
            self.release_finished_session()

    async def release_resources(self, shutdown_reason: str) -> None:
        """停止本机采集和后台任务，并将退出时未完成的测量按失败处理。

        Args:
            shutdown_reason: 未完成测量的退出原因。

        Returns:
            返回示例：
                None  # 相机和后台任务已结束，当前测量和频率归属已清空
        """
        # 停止相机并等待采集流程结束；停止失败时按系统故障处理。
        try:
            await self.camera.stop()
        except Exception as error:
            self.on_system_failure(error)

        # 收集并取消本机所有 OCR 任务和超时任务。
        machine_tasks = [
            *self.deadline_tasks.values(),
            *self.unfinished_recognition_tasks,
        ]
        self.deadline_tasks.clear()
        for task in machine_tasks:
            task.cancel()

        # 等待这些后台任务全部结束。
        await asyncio.gather(*machine_tasks, return_exceptions=True)

        # 系统退出时，如果当前测量还没有结束，就按退出原因处理为失败。
        session = self.current_session
        if (
            session is not None
            and session.state in {SessionState.RUNNING, SessionState.SAVING_RESULT}
        ):
            try:
                await self.handle_session_failure(session, shutdown_reason)
            except Exception as error:
                self.on_system_failure(error)

        # 最后清空当前测量和频率归属。
        self.current_session = None
        self.frequency_adapter.active_session_id = None

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
            try:
                await self.publish_event(RuntimeEvent(event_type, session.machine_id, session.session_id, payload))
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
        if session.frequency_window_sealed:
            await run_blocking_operation(
                self.database.save_abnormal_event, "迟到的频率读数", event
            )
            return

        # 按收到的顺序保存本轮频率读数。
        session.measurement_frequencies.append(event.payload)

    async def try_finalize(self, session: MeasurementSession) -> None:
        """检查本轮测量是否完成，条件满足后保存正常记录或待复核记录。

        Args:
            session: 待检查完成条件的本轮测量档案。

        Returns:
            返回示例：
                None  # 条件不足时继续等待，否则保存本轮记录
        """
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
            ordered_lines=tuple(ocr_result.ordered_lines),
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

        # 检查并释放已经完成的当前测量。
        self.release_finished_session()

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
        self.deadline_tasks[event_type] = asyncio.create_task(publish_timeout())
