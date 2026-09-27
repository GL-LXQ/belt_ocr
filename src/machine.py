"""串行处理一台机器的启动、关闭和后台结果。"""

import asyncio
import logging
import sqlite3
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
from models import BeltSession, CapturedFrame, RuntimeEvent, PublishEvent
from async_utils import run_blocking_operation
from text_recognition import OCRProcessingError, TextRecognizer
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


class Machine:
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
        on_fatal_error: Callable[[Exception], None],
        state_changed: asyncio.Event,
        notify_camera_state: Callable[[str, str, str], None] | None = None,
        notify_ocr_result: Callable[[str, str, tuple[str, ...], tuple[str, ...]], None] | None = None,
        notify_cycle_closed: Callable[[str, str], None] | None = None,
    ) -> None:
        """组装一台机器的唯一当前周期及其处理依赖。

        Args:
            machine_config: 机器身份配置。
            config: 采集、期限和存储配置。
            camera: 当前机器的相机适配器。
            frequency_adapter: 当前机器的频率接收适配器。
            text_recognizer: 三台机器共享的 OCR 处理器。
            database: 数据库访问对象。
            publish_event: 业务事件路由入口。
            notify_measurement_progress: 可选进度通知函数，接收机器编号、周期编号、处理阶段和阶段状态。
            on_fatal_error: 致命故障回调，把识别任务异常交给运行时处理。
            state_changed: 周期状态变化通知。
            notify_camera_state: 可选相机状态通知函数，接收机器编号、状态和原因。
            notify_ocr_result: 可选文字通知函数，接收机器编号、周期编号、原文字和去空格文字。
            notify_cycle_closed: 可选周期关闭通知函数，接收机器编号和周期编号。

        Returns:
            返回示例：
                None  # 本机事件队列、唯一周期空位和任务引用已初始化
        """
        # 登记机器身份与公共运行配置。
        self.machine_config = machine_config
        self.config = config

        # 登记本机采集器与频率接收适配器。
        self.camera = camera
        self.frequency_adapter = frequency_adapter

        # 登记共享 OCR 处理器、共享存储与事件路由入口。
        self.text_recognizer = text_recognizer
        self.database = database
        self.publish_event = publish_event

        # 登记相机状态、测量进度和最终文字通知。
        self.notify_camera_state = notify_camera_state
        self.notify_measurement_progress = notify_measurement_progress
        self.notify_ocr_result = notify_ocr_result
        self.notify_cycle_closed = notify_cycle_closed

        # 登记致命故障回调与状态变化通知。
        self.on_fatal_error = on_fatal_error
        self.state_changed = state_changed

        # 按配置容量创建本机串行事件队列。
        self.queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(config.event_queue_capacity)

        # 初始化唯一周期与机器复位标志。
        self.current_session: BeltSession | None = None
        self.waiting_cycle_reset = False

        # 初始化期限任务表、启动准备标志和识别任务引用。
        self.deadline_tasks: dict[EventType, asyncio.Task[None]] = {}
        self.initialized = False
        self.recognition_task: asyncio.Task | None = None

    @property
    def acceptance_state(self) -> str:
        """返回本机当前是否允许接收新周期。

        Args:
            无外部参数。

        Returns:
            返回示例：
                "READY"  # 可接收新周期
                "INITIALIZING"  # 本机尚未完成启动准备
                "FAULT"  # 相机不可用
                "WAIT_CYCLE_RESET"  # 等待现场周期复位
                "ACTIVE"  # 已有活动周期
                "DEGRADED"  # 相机仍被上一轮占用
        """
        # 尚未完成启动准备时不受理。
        if not self.initialized:
            return "INITIALIZING"

        # 相机不可用时不受理。
        if not self.camera.available:
            return "FAULT"

        # 等待现场复位时不受理。
        if self.waiting_cycle_reset:
            return "WAIT_CYCLE_RESET"

        # 已有活动周期时不受理。
        if self.current_session is not None:
            return "ACTIVE"

        # 相机仍在采集时不受理。
        if self.camera.is_capturing:
            return "DEGRADED"

        # 全部条件满足时允许接收新周期。
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
            # 等待并取出本机队列中的下一个事件。
            event = await self.queue.get()
            try:
                # 按顺序处理本次事件。
                await self.handle_event(event)

                # 回执未完成时通知请求方本次处理结束。
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_result(None)
            except Exception as error:
                # 事件处理异常交给请求方后继续抛出。
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.set_exception(error)
                raise
            finally:
                # 通知状态已变化并标记本次队列任务处理结束。
                self.state_changed.set()
                self.queue.task_done()

                # 释放本次事件的引用。
                event = None

    async def wait_until_event_queue_drained(self) -> None:
        """等待本机事件队列中已提交的事件全部处理结束。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 队列中不再有等待处理和正在处理的事件
        """
        # 等待全部已入队事件完成队列结算。
        await self.queue.join()

    def discard_pending_events(self) -> None:
        """丢弃队列中尚未处理的事件，并取消这些事件未完成的处理回执。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 队列已清空，被丢弃事件的回执已取消，不再有人等待
        """
        # 逐条取出队列中尚未被 worker 取走的事件。
        while not self.queue.empty():
            pending_event = self.queue.get_nowait()

            # 取消该事件尚未完成的处理回执。
            if pending_event.acknowledgement is not None and not pending_event.acknowledgement.done():
                pending_event.acknowledgement.cancel()

            # 结算本次取出对应的队列任务计数。
            self.queue.task_done()

    async def handle_machine_start(self) -> None:
        """检查接收条件，创建本轮测量档案并启动采集窗口和超时任务。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 启动本轮测量，或在不满足接收条件时提前结束
        """
        # 上一轮尚未结束时跳过本次 START。
        if self.current_session is not None:
            logger.warning("上一轮尚未结束，跳过 START machine_id=%s", self.machine_config.machine_id)
            return

        # 等待现场复位时跳过本次 START。
        if self.waiting_cycle_reset:
            return

        # 检查相机可用性与采集占用。
        if not self.camera.available or self.camera.is_capturing:
            # 标记等待周期复位。
            self.waiting_cycle_reset = True

            # 记录本轮未受理。
            logger.error("本轮未受理 machine_id=%s", self.machine_config.machine_id)
            return

        # 创建本轮测量档案，登记周期编号、采集编号和开始时间。
        session = BeltSession(
            session_id=uuid4().hex,
            machine_id=self.machine_config.machine_id,
            camera_serial=self.machine_config.camera_serial,
            frequency_meter_serial=self.machine_config.frequency_meter_serial,
            capture_id=uuid4().hex,
            start_time=datetime.now(timezone.utc).isoformat(),
            capture_start_time=asyncio.get_running_loop().time(),
        )

        # 登记本机唯一的当前周期。
        self.current_session = session

        # 记录本轮开始日志。
        logger.info("开始测量 machine_id=%s session_id=%s", session.machine_id, session.session_id)

        # 启动本轮图像采集。
        self.camera.start_capture(session.session_id, session.capture_start_time)

        # 采集交付结束后尝试释放本轮周期。
        self.camera.delivery_task.add_done_callback(lambda task: self.release_finished_session())

        # 登记频率接收的当前周期。
        self.frequency_adapter.active_session_id = session.session_id

        # 上报本轮启动，并标记图像采集与频率采集开始。
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

        # 安排本轮运行超时事件。
        self.schedule_timeout(session, EventType.CYCLE_TIMEOUT, self.config.max_cycle_open_ms)

        # 安排本轮 OCR 超时事件。
        self.schedule_timeout(session, EventType.OCR_TIMEOUT, self.config.ocr_result_timeout_ms)

    async def handle_machine_close(
        self,
        interrupted: bool = False,
        capture_stop_time: float | None = None,
        close_event: RuntimeEvent | None = None,
        failure_reason: str = "测量周期中断",
    ) -> None:
        """结束本轮采集，结算频率并检查完成条件。

        Args:
            interrupted: False 表示正常 CLOSE；True 表示故障、超时或退出中断。
            capture_stop_time: 关闭信号接收时的单调时间，省略时取当前时间。
            close_event: 触发本次关闭的事件，省略时表示关闭没有事件来源。
            failure_reason: 中断关闭时记录的失败原因。

        Returns:
            返回示例：
                None  # 本轮现场采集和频率接收已结束，OCR 与存储按各自状态继续处理
        """
        # 旧周期的关闭事件只写审计，不操作当前周期。
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

        # 取出本机当前周期。
        session = self.current_session

        # 空闲时的关闭只清除等待复位状态。
        if session is None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return

        # 已关闭的周期继续等待结果，不重复结算频率。
        if session.capture_stop_time is not None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return

        # 记录本轮关闭边界时间。
        session.capture_stop_time = (
            capture_stop_time if capture_stop_time is not None else asyncio.get_running_loop().time()
        )

        # 通知界面本轮周期首次进入关闭处理。
        if self.notify_cycle_closed is not None:
            self.notify_cycle_closed(session.machine_id, session.session_id)

        # 中断关闭时标记等待真实关闭复位。
        self.waiting_cycle_reset = interrupted

        # 清空适配器的当前周期，停止向本轮交付频率。
        self.frequency_adapter.active_session_id = None

        # 封闭本轮频率列表。
        session.frequency_window_sealed = True

        # 按是否中断、是否已有异常和已有读数结算最终频率。
        if interrupted or session.frequency_state == FrequencyState.FAILED:
            # 中断或已异常时最终频率为空，已收到的明细继续保留。
            session.final_frequency = None
            session.frequency_state = FrequencyState.FAILED
        elif session.measurement_frequencies:
            # 有有效读数时取最后收到的读数，标记频率正常。
            session.final_frequency = session.measurement_frequencies[-1]
            session.frequency_state = FrequencyState.SUCCESS
        else:
            # 没有有效读数时标记频率异常并记录缺少测量的错误。
            session.frequency_state = FrequencyState.FAILED
            session.errors.append("FREQUENCY_NO_VALID_MEASUREMENT")

        # 通知界面本轮频率采集的结算结果。
        if self.notify_measurement_progress is not None:
            # 按频率结算状态取上报状态。
            frequency_progress_status = (
                ProgressStatus.SUCCESS
                if session.frequency_state == FrequencyState.SUCCESS
                else ProgressStatus.FAILED
            )

            # 上报本轮频率采集状态。
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.FREQUENCY_COLLECTION,
                frequency_progress_status,
            )

        # 停止本轮采集交付。
        await self.camera.inform_capture_workflow_stop()

        # 从任务表移除本轮 CycleTimeout 任务。
        deadline_task = self.deadline_tasks.pop(EventType.CYCLE_TIMEOUT, None)

        # 任务仍存在时取消它的超时通知。
        if deadline_task is not None:
            deadline_task.cancel()

        # 本轮已失败时尝试释放周期并结束。
        if session.state == SessionState.FAILED:
            self.release_finished_session()
            return

        # 中断关闭按失败清理并结束。
        if interrupted:
            await self.handle_measurement_failure(session, failure_reason)
            return

        # 检查正常关闭周期的结果，条件满足时提交数据库。
        await self.try_finalize(session)

    async def handle_event(self, event: RuntimeEvent) -> None:
        """按事件类型处理机器和测量周期业务，并检查本轮是否完成。

        Args:
            event: 待处理的测量事件，包含事件身份、来源信息和业务数据。

        Returns:
            返回示例：
                None  # 完成业务处理，或隔离事件后提前结束
        """
        # 分派不依赖测量档案的机器级事件。
        match event.event_type:
            # 受理启动信号，建立本轮周期。
            case EventType.MACHINE_STARTED:
                await self.handle_machine_start()
                return

            # 受理正常关闭信号。
            case EventType.MACHINE_CLOSED:
                await self.handle_machine_close(capture_stop_time=event.received_monotonic, close_event=event)
                return

            # 退出时中断当前周期。
            case EventType.SHUTDOWN:
                await self.handle_machine_close(interrupted=True)
                return

            # IO 读取中断时结束尚未关闭的周期。
            case EventType.IO_INTERRUPTED:
                session = self.current_session
                # 与 IO 轮询的发送过滤是同一条件，这里再判一次让机器自己守住边界。
                if session is not None and session.capture_stop_time is None:
                    await self.handle_machine_close(
                        interrupted=True, failure_reason="IO 通信中断"
                    )

                # 等待通信恢复后重新确认现场状态。
                self.waiting_cycle_reset = True
                return

        # 相机采集设备故障时通知界面。
        if event.event_type == EventType.CAPTURE_FAILED and self.notify_camera_state is not None:
            self.notify_camera_state(self.machine_config.machine_id, "相机故障", event.payload)

        # 没有周期身份的频率只写审计，不分配给当前或历史周期。
        if event.event_type == EventType.FREQUENCY_MEASURED and not event.session_id:
            await run_blocking_operation(
                self.database.save_abnormal_event,
                "频率读数缺少 Session ID",
                event,
            )
            return

        # 取出本机当前周期。
        session = self.current_session

        # 事件不属于当前周期时隔离并结束。
        if session is None or event.session_id != session.session_id:
            logger.warning(
                "隔离未知或已结算事件 machine_id=%s session_id=%s event=%s",
                event.machine_id,
                event.session_id,
                event.event_type,
            )
            return

        # 周期未关闭时处理期限通知。
        if event.event_type == EventType.CYCLE_TIMEOUT:
            if session.capture_stop_time is None:
                await self.handle_machine_close(
                    interrupted=True,
                    failure_reason="测量周期超时",
                )
            return

        # 本轮不在处理中时丢弃迟到结果并结束。
        if session.state != SessionState.RUNNING:
            logger.warning(
                "忽略迟到结果 session_id=%s state=%s event=%s",
                session.session_id,
                session.state.value,
                event.event_type,
            )
            return

        # 分派采集、识别和频率事件。
        match event.event_type:
            # 整轮采集结果到达。
            case EventType.CAPTURE_COMPLETED:
                # 只接收等待阶段的采集结果。
                if session.ocr_state != OCRState.WAITING:
                    return

                # 保留整轮采集统计。
                capture_result = event.payload
                session.capture_summary = capture_result.statistics

                # 没有采集帧时上报图像采集失败。
                if not capture_result.frames:
                    if self.notify_measurement_progress is not None:
                        self.notify_measurement_progress(
                            session.machine_id,
                            session.session_id,
                            ProgressStage.IMAGE_CAPTURE,
                            ProgressStatus.FAILED,
                        )

                    # 按无采集帧原因结束本轮测量。
                    await self.handle_measurement_failure(session, "本轮未采集到图像")
                    return

                # 上报图像采集完成，并标记字符识别开始。
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

                # 标记本轮进入识别。
                session.ocr_state = OCRState.RUNNING

                # 启动整轮识别任务并登记完成回调。
                task = asyncio.create_task(self.recognize_session(session, capture_result.frames))
                self.recognition_task = task
                task.add_done_callback(self.handle_recognition_task_finished)
                return

            # 相机采集设备故障。
            case EventType.CAPTURE_FAILED:
                # 上报本轮图像采集失败。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.IMAGE_CAPTURE,
                        ProgressStatus.FAILED,
                    )

                # 记录设备错误明细。
                session.errors.append(event.payload)

                # 按相机故障结束本轮测量。
                await self.handle_measurement_failure(session, "相机采集失败")
                return

            # 识别结果到达。
            case EventType.OCR_COMPLETED:
                # 只接收正在处理周期的结果。
                if session.ocr_state != OCRState.RUNNING:
                    return

                # 保存整轮识别结果并标记处理完成。
                session.ocr_result = event.payload
                session.ocr_state = OCRState.COMPLETED

                # 向界面交付当前周期的最终文字。
                if self.notify_ocr_result is not None:
                    self.notify_ocr_result(
                        session.machine_id,
                        session.session_id,
                        session.ocr_result.ordered_lines,
                        session.ocr_result.normalized_lines,
                    )

                # 上报字符识别完成。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.CHARACTER_RECOGNITION,
                        ProgressStatus.SUCCESS,
                    )

                # 从任务表移除本轮 OCR 超时任务。
                deadline = self.deadline_tasks.pop(EventType.OCR_TIMEOUT, None)

                # 任务仍存在时取消它的超时通知。
                if deadline is not None:
                    deadline.cancel()

            # 识别失败或识别超时。
            case EventType.OCR_FAILED | EventType.OCR_TIMEOUT:
                # 已有终态时忽略本次失败或超时。
                if session.ocr_state not in {OCRState.WAITING, OCRState.RUNNING}:
                    return

                # 登记识别状态和执行异常明细。
                session.ocr_state = OCRState.TIMED_OUT if event.event_type == EventType.OCR_TIMEOUT else OCRState.FAILED
                if event.event_type == EventType.OCR_FAILED:
                    session.errors.append(event.payload)

                # 上报字符识别失败。
                if self.notify_measurement_progress is not None:
                    self.notify_measurement_progress(
                        session.machine_id,
                        session.session_id,
                        ProgressStage.CHARACTER_RECOGNITION,
                        ProgressStatus.FAILED,
                    )

            # 新的有效频率读数到达。
            case EventType.FREQUENCY_MEASURED:
                await self.handle_frequency_measured(session, event)
                return

            # 未知事件类型只写审计后结束。
            case _:
                await run_blocking_operation(
                    self.database.save_abnormal_event, "未知事件类型", event
                )
                return

        # 统一处理本轮失败或满足条件后的提交。
        await self.try_finalize(session)

    async def handle_measurement_failure(
        self, session: BeltSession, failure_reason: str
    ) -> None:
        """登记本轮失败并清理资源，保留尚未关闭的现场周期身份。

        Args:
            session: 处理失败、中断或提交失败的测量档案。
            failure_reason: 本轮失败的原因描述。

        Returns:
            返回示例：
                None  # 已登记失败并清理资源，活动周期保留至 CLOSE 或中断
        """
        # 登记本轮失败原因。
        session.errors.append(failure_reason)

        # 标记本轮失败并记录结算时间。
        session.state = SessionState.FAILED
        session.finish_time = datetime.now(timezone.utc).isoformat()

        # 打印机器、周期和错误明细。
        logger.error(
            "测量失败 machine_id=%s session_id=%s errors=%s",
            session.machine_id,
            session.session_id,
            session.errors,
        )

        # 将本轮失败原因和错误明细写入异常事件表。
        try:
            await run_blocking_operation(
                self.database.save_abnormal_event,
                failure_reason,
                machine_id=session.machine_id,
                session_id=session.session_id,
                payload={"session_errors": list(session.errors)},
            )
        except Exception:
            # 审计写入失败时记录日志。
            logger.exception(
                "记录测量失败事件失败 machine_id=%s session_id=%s",
                session.machine_id,
                session.session_id,
            )

        # 取消等待或正在执行的识别任务。
        recognition_task = self.recognition_task
        if recognition_task is not None:
            recognition_task.cancel()

        # 释放本轮识别结果。
        session.ocr_result = None

        # 逐个取消期限任务，未关闭的周期保留 CycleTimeout 等待真实 CLOSE。
        for event_type in tuple(self.deadline_tasks):
            if session.capture_stop_time is None and event_type == EventType.CYCLE_TIMEOUT:
                continue
            self.deadline_tasks.pop(event_type).cancel()

        # 停止向本轮交付频率并封闭频率窗口。
        self.frequency_adapter.active_session_id = None
        session.frequency_window_sealed = True

        # 频率仍在接收时按失败结算。
        if session.frequency_state == FrequencyState.RUNNING:
            session.frequency_state = FrequencyState.FAILED

        # 停止本轮采集交付并尝试释放周期。
        await self.camera.inform_capture_workflow_stop()
        self.release_finished_session()

    def release_finished_session(self) -> None:
        """在周期关闭且后台资源释放后清空唯一的当前周期。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 条件满足时清空周期并通知等待方，否则继续等待
        """
        # 只处理已关闭的周期。
        session = self.current_session
        if session is None or session.capture_stop_time is None:
            return

        # 只处理已入库成功或已失败的周期。
        if session.state not in {SessionState.COMMITTED, SessionState.FAILED}:
            return

        # 识别任务未结束时继续等待。
        if self.recognition_task is not None:
            return

        # 采集交付任务未结束时继续等待。
        if self.camera.delivery_task is not None:
            return

        # 释放识别结果并取消全部期限任务。
        session.ocr_result = None
        for task in self.deadline_tasks.values():
            task.cancel()
        self.deadline_tasks.clear()

        # 清空当前周期并通知等待方。
        self.current_session = None
        self.state_changed.set()

    def handle_recognition_task_finished(self, task: asyncio.Task) -> None:
        """释放本轮识别任务，报告交付异常并尝试结束当前周期。

        Args:
            task: 已结束或取消的整轮识别任务。

        Returns:
            返回示例：
                None  # 任务引用已释放，已关闭的结束周期已清理
        """
        # 释放本轮识别任务引用。
        self.recognition_task = None

        # 任务未被取消时读取异常并交给致命故障入口。
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.on_fatal_error(error)

        # 尝试释放本轮周期。
        self.release_finished_session()

    async def release_resources(self, shutdown_reason: str) -> None:
        """停止本机采集，取消本机任务，并结算退出时未完成的周期。

        Args:
            shutdown_reason: 未完成周期的退出原因描述。

        Returns:
            返回示例：
                None  # 相机已停止，本机任务已结束，周期与频率交付身份已清空
        """
        # 停止本机采集并等待结果交付结束，异常交给致命故障入口。
        try:
            await self.camera.stop()
        except Exception as error:
            self.on_fatal_error(error)

        # 收集并取消本机识别任务与全部期限任务。
        machine_tasks = [*self.deadline_tasks.values()]
        if self.recognition_task is not None:
            machine_tasks.append(self.recognition_task)
        self.deadline_tasks.clear()
        for task in machine_tasks:
            task.cancel()

        # 等待本机任务全部结束。
        await asyncio.gather(*machine_tasks, return_exceptions=True)

        # 退出时仍未结算的周期按退出原因执行失败清理。
        session = self.current_session
        if (
            session is not None
            and session.state in {SessionState.RUNNING, SessionState.SAVING_RESULT}
        ):
            try:
                await self.handle_measurement_failure(session, shutdown_reason)
            except Exception as error:
                self.on_fatal_error(error)

        # 清空退出后不再保留的周期身份与频率交付身份。
        self.current_session = None
        self.frequency_adapter.active_session_id = None

    async def recognize_session(self, session: BeltSession, frames: tuple[CameraFrame, ...]) -> None:
        """等待共享锁并执行整轮 OCR，向所属周期交付一次结果。

        Args:
            session: 原测量周期，任务执行前检查其状态。
            frames: 本轮全部原始帧。

        Returns:
            返回示例：
                None  # 最终结果或失败原因通过事件交付
        """
        # 等待共享识别锁。
        async with self.text_recognizer.processing_lock:
            # 周期已失效时直接结束。
            if session.state != SessionState.RUNNING:
                return

            # 在线程中执行原始帧整理、筛帧、识别和终选。
            try:
                result = await run_blocking_operation(
                    self.text_recognizer.process_session_frames,
                    session.session_id,
                    session.capture_id,
                    session.camera_serial,
                    frames,
                )
            except OCRProcessingError as error:
                # 记录整轮处理失败。
                logger.exception("OCR 处理失败 session_id=%s", session.session_id)

                # 让出一次事件循环控制权。
                await asyncio.sleep(0)

                # 按识别失败交付原因。
                event_type, payload = EventType.OCR_FAILED, str(error)
            else:
                # 按识别成功交付结果。
                event_type, payload = EventType.OCR_COMPLETED, result

        # 释放原始帧引用。
        frames = ()

        # 只向仍然有效的周期交付结果。
        if session.state == SessionState.RUNNING:
            try:
                await self.publish_event(RuntimeEvent(event_type, session.machine_id, session.session_id, payload))
            except Exception:
                # 记录结果交付异常并结束后台任务。
                logger.exception("OCR 结果交付失败 session_id=%s", session.session_id)
                raise

    async def handle_frequency_measured(self, session: BeltSession, event: RuntimeEvent) -> None:
        """按接收顺序保存黑盒交付的新有效测量。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            返回示例：
                None  # 已追加有效频率，或记录迟到频率审计后忽略事件
        """
        # 频率窗口已封闭时只写迟到频率审计。
        if session.frequency_window_sealed:
            await run_blocking_operation(
                self.database.save_abnormal_event, "迟到的频率读数", event
            )
            return

        # 按接收顺序追加本轮频率明细。
        session.measurement_frequencies.append(event.payload)

    async def try_finalize(self, session: BeltSession) -> None:
        """检查本轮结果，收到 OCR 结果后提交普通或待复核记录。

        Args:
            session: 待检查完成条件的本轮测量档案。

        Returns:
            返回示例：
                None  # 条件不足时继续等待，否则冻结并提交本轮记录
        """
        # 本轮不在处理中时直接结束。
        if session.state != SessionState.RUNNING:
            return

        # OCR 失败时执行本轮失败清理。
        if session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}:
            failure_reason = (
                "OCR 识别超时" if session.ocr_state == OCRState.TIMED_OUT
                else "OCR 识别执行失败"
            )
            await self.handle_measurement_failure(session, failure_reason)
            return

        # 周期未关闭或 OCR 未完成时继续等待。
        if session.capture_stop_time is None or session.ocr_state != OCRState.COMPLETED:
            return

        # 记录本轮结算时间。
        session.finish_time = datetime.now(timezone.utc).isoformat()

        # 取出最终频率与识别结果。
        final_frequency = session.final_frequency
        ocr_result = session.ocr_result

        # 汇总 OCR 和频率的人工复核原因。
        review_reasons = []
        if ocr_result.review_reason is not None:
            review_reasons.append(ocr_result.review_reason)
        if final_frequency is None:
            review_reasons.append("没有找到最终频率，请人工复核。")
        needs_review = bool(review_reasons)
        review_reason = "；".join(review_reasons) if review_reasons else None

        # 按复核状态选择本轮需要保存的图片。
        if ocr_result.review_reason is not None:
            evidence_frames = ocr_result.review_frames
        else:
            evidence_frames = ocr_result.selected_frames

        # 按本轮开始时间生成本地日期目录。
        local_start_time = datetime.fromisoformat(session.start_time).astimezone()
        local_start_date = local_start_time.strftime("%Y%m%d")

        # 按日期、机器和周期生成本轮证据图片目录。
        evidence_directory = (
            self.config.evidence_directory / local_start_date
            / session.machine_id / session.session_id
        )

        # 组装本轮测量记录。
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

        # 标记本轮正在保存证据与记录。
        session.state = SessionState.SAVING_RESULT

        # 上报证据入库开始。
        if self.notify_measurement_progress is not None:
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.EVIDENCE_STORAGE,
                ProgressStatus.RUNNING,
            )

        # 释放本轮识别结果引用。
        session.ocr_result = None

        # 在线程中依次保存证据图片和测量记录。
        try:
            await run_blocking_operation(
                self.save_evidence_images_and_measurement_record,
                record,
                evidence_frames,
            )
        except MvsError as error:
            # 记录证据图片编码失败明细。
            session.errors.append(str(error))

            # 上报证据入库失败。
            if self.notify_measurement_progress is not None:
                self.notify_measurement_progress(
                    session.machine_id,
                    session.session_id,
                    ProgressStage.EVIDENCE_STORAGE,
                    ProgressStatus.FAILED,
                )

            # 按证据图片编码失败结束本轮测量。
            await self.handle_measurement_failure(session, "证据图片编码失败")
            return
        except EvidenceWriteError as error:
            # 记录证据图片写入失败。
            logger.exception(
                "证据图片写入失败 machine_id=%s session_id=%s",
                session.machine_id,
                session.session_id,
            )

            # 上报证据入库失败。
            if self.notify_measurement_progress is not None:
                self.notify_measurement_progress(
                    session.machine_id,
                    session.session_id,
                    ProgressStage.EVIDENCE_STORAGE,
                    ProgressStatus.FAILED,
                )

            # 按证据图片写入失败结束本轮测量。
            await self.handle_measurement_failure(session, "证据图片保存失败")

            # 将证据图片写入故障交给全局退出流程。
            self.on_fatal_error(error)
            return
        except (CommitIntegrityConflictError, sqlite3.Error) as error:
            # 登记数据库提交失败或内容冲突。
            failure_reason = (
                "测量记录提交冲突"
                if isinstance(error, CommitIntegrityConflictError)
                else "测量结果入库失败"
            )
            logger.exception(
                "数据库提交失败 machine_id=%s session_id=%s",
                session.machine_id,
                session.session_id,
            )

            # 上报证据入库失败。
            if self.notify_measurement_progress is not None:
                self.notify_measurement_progress(
                    session.machine_id,
                    session.session_id,
                    ProgressStage.EVIDENCE_STORAGE,
                    ProgressStatus.FAILED,
                )

            # 清理本轮失败状态。
            await self.handle_measurement_failure(session, failure_reason)

            # 将数据库写入故障交给全局退出流程。
            self.on_fatal_error(error)
            return

        # 标记本轮已入库。
        session.state = SessionState.COMMITTED

        # 上报证据入库完成。
        if self.notify_measurement_progress is not None:
            self.notify_measurement_progress(
                session.machine_id,
                session.session_id,
                ProgressStage.EVIDENCE_STORAGE,
                ProgressStatus.SUCCESS,
            )

        # 记录本轮保存结果。
        logger.info(
            "已保存 machine_id=%s session_id=%s",
            session.machine_id,
            session.session_id,
        )

        # 释放已完成的周期。
        self.release_finished_session()

    def save_evidence_images_and_measurement_record(
        self,
        record: MeasurementRecord,
        evidence_frames: tuple[CapturedFrame, ...],
    ) -> None:
        """逐张保存证据图片，再写入本轮测量记录。

        Args:
            record: 本轮测量的业务字段和证据图片目录。
            evidence_frames: 本轮需要保存的原始帧。

        Returns:
            返回示例：
                None  # 证据图片和测量记录均已保存
        """
        # 记录本次新建的图片。
        created_image_paths = []

        try:
            # 逐帧编码并保存本轮证据图片。
            self.encode_and_save_evidence_images(record, evidence_frames, created_image_paths)
        except Exception as error:
            # 清理本次新建的证据图片。
            for image_path in created_image_paths:
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("清理证据图片失败 path=%s", image_path)

            # 标记图片阶段的文件操作失败。
            if isinstance(error, OSError):
                raise EvidenceWriteError("证据图片写入失败") from error
            raise

        # 图片全部保存后写入测量记录。
        self.database.write_measurement_record(record)

    def encode_and_save_evidence_images(
        self,
        record: MeasurementRecord,
        evidence_frames: tuple[CapturedFrame, ...],
        created_image_paths: list[Path],
    ) -> None:
        """逐帧编码并保存本轮尚不存在的证据图片。

        Args:
            record: 本轮测量的业务字段和证据图片目录。
            evidence_frames: 本轮需要保存的原始帧。
            created_image_paths: 记录本次新建的图片路径，供失败时清理。

        Returns:
            返回示例：
                None  # 本轮证据图片已保存，新建路径已登记
        """
        # 逐帧跳过已有证据图片。
        for frame in evidence_frames:
            image_path = record.evidence_directory / f"{frame.frame_id}.jpg"
            if image_path.exists():
                continue

            # 将相机原始帧编码为 JPG 图片。
            try:
                image_data = self.camera.sdk_camera.encode_image(frame.camera_frame)
            except MvsError:
                raise
            except Exception as error:
                raise ImageEncodingError("相机图片编码失败") from error

            # 登记本次需要新建的图片路径。
            created_image_paths.append(image_path)

            # 原子保存本帧证据图片。
            save_evidence_image(image_data, image_path)

    def schedule_timeout(self, session: BeltSession, event_type: EventType, timeout_ms: int) -> None:
        """为当前周期安排指定类型的期限通知。

        Args:
            session: 当前测量周期。
            event_type: 周期关闭或 OCR 超时事件类型。
            timeout_ms: 等待毫秒数。

        Returns:
            返回示例：
                None  # 期限任务已按事件类型登记
        """
        async def publish_timeout() -> None:
            """等待期限并交付携带原周期身份的超时事件。

            Args:
                无外部参数。

            Returns:
                返回示例：
                    None  # 超时事件已交付，或任务被提前取消
            """
            # 等待期限到期。
            await asyncio.sleep(timeout_ms / 1000)

            # 交付携带原周期身份的超时事件。
            await self.publish_event(RuntimeEvent(event_type, session.machine_id, session.session_id))

        # 保存本轮该类型的唯一期限任务。
        self.deadline_tasks[event_type] = asyncio.create_task(publish_timeout())
