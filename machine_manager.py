"""串行处理一台机器的启动、关闭和后台结果。"""

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from camera import SessionCamera
from configuration import MachineConfiguration, MeasurementConfiguration
from frequency_adapter import FrequencyAdapter
from enums import OCRState, FrequencyState, MachineState, SessionState
from models import BeltSession, MeasurementEvent, PublishEvent
from recovery import run_blocking_operation, serialize_value
from text_recognition import TextRecognizer
from database import Database, DatabaseRequest


logger = logging.getLogger(__name__)


class MachineManager:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        camera: SessionCamera,
        frequency_adapter: FrequencyAdapter,
        text_recognizer: TextRecognizer,
        database: Database,
        publish_event: PublishEvent,
        state_changed: asyncio.Event,
    ) -> None:
        self.machine = machine
        self.configuration = configuration
        self.camera = camera
        self.frequency_adapter = frequency_adapter
        self.text_recognizer = text_recognizer
        self.database = database
        self.publish_event = publish_event
        self.state_changed = state_changed
        self.queue: asyncio.Queue[MeasurementEvent] = asyncio.Queue(
            configuration.event_queue_capacity
        )
        self.active_session_id: str | None = None
        self.sessions: dict[str, BeltSession] = {}
        self.waiting_cycle_reset = False
        self.interrupted_session_id: str | None = None
        self.deadline_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.recovery = database.recovery
        self.process_epoch = uuid4().hex
        self.device_faults: set[str] = set()
        self.capacity_available = True
        self.initialized = False
        self.background_tasks: set[asyncio.Task[None]] = set()

    @property
    def acceptance_state(self) -> str:
        if not self.initialized:
            return "INITIALIZING"
        if self.device_faults or not self.recovery.available or not self.camera.available:
            return "FAULT"
        if self.waiting_cycle_reset:
            return "WAIT_CYCLE_RESET"
        if self.active_session_id is not None:
            return "ACTIVE"
        if not self.capacity_available or self.camera.is_capturing:
            return "DEGRADED"
        return "READY"

    async def listen_and_process_events(self) -> None:
        """持续监听本机事件队列，按顺序处理事件并反馈处理结果。

        Args:
            无外部参数。

        Returns:
            None: 持续运行直到任务被取消，无返回数据。
            返回值形式示例：
                None  # 无返回数据
        """
        while True:
            # 等待并取出本机队列中的下一个事件。
            event = await self.queue.get()
            try:
                # 处理事件后，如果有回执，而且回执还没结束，就通知等待方：处理完成了。
                await self.process_event(event)
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_result(None)
            except Exception:
                # 登记业务处理故障并设置等待周期复位。
                self.device_faults.add("BUSINESS_PROCESSING")
                self.waiting_cycle_reset = True
                logger.exception(
                    "业务处理失败 machine_id=%s session_id=%s event=%s",
                    event.machine_id, event.session_id, event.event_type,
                )
                try:
                    # 保存异常审计并中断当前测量。
                    await run_blocking_operation(
                        self.recovery.audit, "BUSINESS_PROCESSING_FAILED", event,
                    )
                    await self.close_measurement(interrupted=True)
                except Exception:
                    # 标记本地运行库不可用并记录异常。
                    self.recovery.available = False
                    logger.exception("异常审计或中断处理失败 machine_id=%s", event.machine_id)
                # 向等待方报告本次事件处理失败。
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_exception(RuntimeError("测量处理失败。"))
            finally:
                # 通知状态已变化，并标记当前队列任务处理结束。
                self.state_changed.set()
                self.queue.task_done()

    async def start_measurement(self) -> None:
        """检查接收条件，创建本轮测量档案并启动采集窗口和超时任务。

        Args:
            无外部参数。

        Returns:
            None: 启动本轮测量，或在不满足接收条件时提前结束，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 忽略活动周期内的重复启动和未同步的启动。
        if self.active_session_id is not None or self.waiting_cycle_reset:
            return

        # 检查本机积压、存储容量、设备故障和恢复库状态。
        if (
            len(self.sessions) >= self.configuration.max_pending_sessions_per_machine
            or not self.capacity_available
            or self.device_faults
            or not self.recovery.available
            or not self.camera.available
            or self.camera.is_capturing
        ):
            # 标记等待周期复位并打印本轮未受理日志。
            self.waiting_cycle_reset = True
            logger.error("本轮未受理 machine_id=%s", self.machine.machine_id)
            return

        # 创建本轮档案，记录设备绑定、开始时间、配置和超时期限。
        session = BeltSession(
            session_id=uuid4().hex,
            machine_id=self.machine.machine_id,
            camera_id=self.machine.camera_id,
            frequency_source_id=self.machine.frequency_source_id,
            capture_id=uuid4().hex,
            start_time=datetime.now(timezone.utc).isoformat(),
            start_boundary=asyncio.get_running_loop().time(),
            process_epoch=self.process_epoch,
            configuration_snapshot=serialize_value(self.configuration),
            ocr_deadline=(
                datetime.now(timezone.utc) + timedelta(milliseconds=self.configuration.ocr_result_timeout_ms)
            ).isoformat(),
            cycle_deadline=(
                datetime.now(timezone.utc) + timedelta(milliseconds=self.configuration.max_cycle_open_ms)
            ).isoformat(),
        )

        # 登记本轮档案，保留本机配置并设置当前活动档案编号。
        self.sessions[session.session_id] = session
        session.configuration_snapshot["machines"] = [serialize_value(self.machine)]
        self.active_session_id = session.session_id

        # 记录本轮开始日志。
        logger.info("开始测量 machine_id=%s session_id=%s", session.machine_id, session.session_id)

        # 启动本轮图像采集，打开对应档案的频率窗口。
        self.camera.start_capture(session.session_id, session.capture_id, session.start_boundary)
        # 登记频率接收的当前周期，新测量按接收顺序交给本轮。
        self.frequency_adapter.active_session_id = session.session_id

        # 安排本轮运行超时和 OCR 超时事件。
        self.schedule_timeout(session, "CycleTimeout", self.configuration.max_cycle_open_ms)
        # 登记本轮 OCR 等待期限，此处只安排超时事件。
        self.schedule_timeout(session, "OCRTimeout", self.configuration.ocr_result_timeout_ms)

    async def close_measurement(self, interrupted: bool = False) -> None:
        """结束本轮采集，结算频率并检查完成条件。

        Args:
            interrupted: False 表示收到正常 CLOSE；True 表示因故障、超时或退出而中断本轮。

        Returns:
            None  # 本轮现场采集和频率接收已结束，OCR 与存储按各自状态继续处理
        """
        # 没有正在测量的 Session 时，收到正常 CLOSE 就清除等待复位和中断周期标记。
        if self.active_session_id is None:
            if not interrupted:
                self.waiting_cycle_reset = False
                self.interrupted_session_id = None
            return

        # 找到当前 Session，记录现场采集截止时刻。
        session = self.sessions[self.active_session_id]
        session.close_boundary = asyncio.get_running_loop().time()

        if interrupted:
            # 中断时登记错误和本轮编号，将机器设为等待关闭复位。
            session.errors.append("CYCLE_INTERRUPTED")
            self.waiting_cycle_reset = True
            self.interrupted_session_id = session.session_id
        else:
            # 正常 CLOSE 清除中断编号，并保存本轮关闭的 UTC 时间。
            self.interrupted_session_id = None
            session.close_time = datetime.now(timezone.utc).isoformat()

        # 清空适配器的当前周期，停止向本轮交付频率；同时封闭本轮频率列表。
        self.frequency_adapter.active_session_id = None
        session.frequency_window_sealed = True

        # 根据周期是否中断、频率是否异常以及已有读数，确定最终频率和状态。
        if interrupted or session.frequency_state == FrequencyState.FAILED:
            # 周期中断或频率已异常时，最终频率为空，已收到的明细继续保留。
            session.final_frequency = None
            session.frequency_state = FrequencyState.FAILED
        elif session.measurement_frequencies:
            # 本轮有有效读数时，取按接收顺序保存的最后一条，标记频率正常。
            session.final_frequency = session.measurement_frequencies[-1]
            session.frequency_state = FrequencyState.SUCCESS
        else:
            # 本轮没有有效读数时，标记频率异常并记录缺少测量的错误。
            session.frequency_state = FrequencyState.FAILED
            session.errors.append("FREQUENCY_NO_VALID_MEASUREMENT")

        # 通知相机按本轮截止时刻停止生产，等待停采完成后清空机器的活动 Session。
        await self.camera.seal_capture(session.capture_id, session.close_boundary)
        self.active_session_id = None

        # 从任务表移除本轮 CycleTimeout；任务仍存在时，取消它后续的超时通知。
        deadline_task = self.deadline_tasks.pop(
            (session.session_id, "CycleTimeout"), None,
        )
        if deadline_task is not None:
            deadline_task.cancel()

        # 判断本轮是否已失败，关闭后移除保留的现场周期身份。
        if session.state == SessionState.FAILED:
            self.sessions.pop(session.session_id)
            return

        # 判断本轮是否中断，中断只打印日志并清理资源。
        if interrupted:
            await self.fail_measurement(session)
            return

        # 检查正常关闭周期的结果，条件满足时提交数据库。
        await self.try_finalize(session)

    async def process_event(self, event: MeasurementEvent) -> None:
        """检查启停信号时效，并分派处理业务。

        Args:
            event: 待处理的测量事件，包含事件身份、来源信息和业务数据。

        Returns:
            None: 完成业务处理，或隔离事件后提前结束。
            返回示例：
                None  # 无返回数据
        """
        # 检查启动和关闭事件是否超出时限，登记并隔离超出时限的事件。
        if event.event_type in {"MachineStarted", "MachineClosed"}:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(event.occurred_at)
            if abs(age.total_seconds()) * 1000 > self.configuration.event_max_age_ms:
                await run_blocking_operation(self.recovery.audit, "STALE_CONTROL_EVENT", event)
                return

        # 分派事件并更新内存中的业务状态。
        await self.apply_event(event)

    async def apply_event(self, event: MeasurementEvent) -> None:
        """校验事件归属，按事件类型分派处理并检查本轮是否完成。

        Args:
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            None: 更新业务状态，不返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 检查事件所属机器。
        if event.machine_id != self.machine.machine_id:
            await run_blocking_operation(self.recovery.audit, "MACHINE_MISMATCH", event)
            logger.warning("隔离机器归属不符事件 event_id=%s", event.event_id)
            return

        # 分派不依赖测量档案的机器级事件。
        match event.event_type:
            case "MachineStarted":
                await self.start_measurement()
                return
            case "MachineClosed":
                # 核对关闭信号的周期身份，再关闭当前测量。
                if event.session_id and event.session_id not in {
                    self.active_session_id, self.interrupted_session_id,
                }:
                    await run_blocking_operation(self.recovery.audit, "CLOSE_SESSION_MISMATCH", event)
                    return
                await self.close_measurement()
                # 收到有效关闭信号后清除初始状态未知的故障。
                self.device_faults.discard("UNKNOWN_INITIAL_STATE")
                return
            case "Shutdown":
                await self.close_measurement(interrupted=True)
                return
            case "DeviceFault":
                # 登记设备故障并中断当前周期。
                self.device_faults.add(event.payload)
                await run_blocking_operation(self.recovery.audit, "DEVICE_FAULT", event)
                await self.close_measurement(interrupted=True)
                self.waiting_cycle_reset = True
                return
            case "DeviceRecovered":
                self.device_faults.discard(event.payload)
                await run_blocking_operation(self.recovery.audit, "DEVICE_RECOVERED", event)
                return
            case "MachineSynchronized":
                # 中断原活动周期，更新机器复位状态。
                if self.active_session_id is not None:
                    await self.close_measurement(interrupted=True)
                self.waiting_cycle_reset = event.payload != MachineState.CLOSED
                if event.payload == MachineState.CLOSED:
                    self.interrupted_session_id = None
                self.device_faults.discard("UNKNOWN_INITIAL_STATE")
                await run_blocking_operation(self.recovery.audit, "MACHINE_SYNCHRONIZED", event)
                return
            case "CapacityChanged":
                self.capacity_available = event.payload
                await run_blocking_operation(self.recovery.audit, "CAPACITY_CHANGED", event)
                return

        # 隔离没有周期身份的频率，不分配给当前或历史 Session。
        if event.event_type == "FrequencyMeasured" and not event.session_id:
            await run_blocking_operation(self.recovery.audit, "AMBIGUOUS_MEASUREMENT", event)
            return

        # 将异步结果定位到原 Session。
        session = self.sessions.get(event.session_id)
        if session is None:
            logger.warning(
                "隔离未知或已结算事件 machine_id=%s session_id=%s event=%s",
                event.machine_id,
                event.session_id,
                event.event_type,
            )
            return

        # 处理数据库提交回调。
        match event.event_type:
            case "CommitSucceeded" | "CommitFailed":
                await self.handle_commit_result(session, event)
                return

        # 判断活动周期是否超过关闭期限，超时后进入机器复位流程。
        if event.event_type == "CycleTimeout":
            if self.active_session_id == session.session_id:
                session.errors.append("CYCLE_TIMEOUT")
                await self.close_measurement(interrupted=True)
            return

        # 判断本轮是否仍在处理，丢弃失败或等待入库后的迟到结果。
        if session.state != SessionState.RUNNING:
            logger.warning(
                "忽略迟到结果 session_id=%s state=%s event=%s",
                session.session_id,
                session.state.value,
                event.event_type,
            )
            return

        # 分派采集结果，保留各事件是否继续结算的处理决定。
        should_finalize = True
        match event.event_type:
            case "FrameBatchSelected":
                # 丢弃 OCR 已失败或超时的迟到图片。
                if session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}:
                    return

                # 将采集端交付的完整图片批次送入 OCR 队列。
                accepted = self.text_recognizer.submit_batch(
                    machine_id=session.machine_id,
                    session_id=session.session_id,
                    frames=event.payload,
                )

                # 成功入队后登记本轮待处理数量，拒收批次只记录错误。
                if accepted:
                    session.pending_recognition_batches += 1
                    # 保留已受理原图，直到终选完成或周期异常结束。
                    session.memory_frames.update(
                        (frame.frame_id, frame) for frame in event.payload
                    )
                else:
                    session.errors.append("OCR_BATCH_REJECTED")
                    logger.error("识别批次被拒收 machine_id=%s session_id=%s", session.machine_id, session.session_id)

                # 结束批次转发，等待后续业务事件。
                return

            case "CaptureSealed":
                # 更新本轮采集封口状态，被忽略的封口事件直接结束处理。
                if not await self.handle_capture_sealed(session, event):
                    return

            case "RecognitionBatchCompleted":
                # 保存本批识别结果，减少本轮待处理批次数。
                session.recognition_results.extend(event.payload)
                session.pending_recognition_batches -= 1

            case "RecognitionBatchFailed":
                # 记录本批识别错误，减少本轮待处理批次数。
                session.errors.append(event.payload)
                session.pending_recognition_batches -= 1
                logger.error(
                    "识别批次失败 machine_id=%s session_id=%s error=%s",
                    session.machine_id,
                    session.session_id,
                    event.payload,
                )

            case "EvidenceValidated" | "EvidenceFailed":
                # 结算证据校验，登记成功状态或失败原因。
                session.evidence_validation_pending = False
                if event.event_type == "EvidenceValidated":
                    session.evidence_verified = True
                else:
                    session.ocr_state = OCRState.FAILED
                    session.errors.append("EVIDENCE_UNAVAILABLE")
            case "OCRFailed" | "CaptureFailed" | "OCRTimeout":
                # 忽略已有 OCR 终态，登记本次失败或超时。
                if session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}:
                    return
                if session.ocr_state == OCRState.SUCCESS:
                    return
                session.ocr_state = (
                    OCRState.TIMED_OUT
                    if event.event_type == "OCRTimeout"
                    else OCRState.FAILED
                )
                session.errors.append(event.payload or "OCR_TIMEOUT")
            case "FrequencyMeasured":
                should_finalize = await self.handle_frequency_measured(session, event)
            case "FrequencyFailed":
                # 登记本轮频率故障，保留明细但不确认最终频率。
                if session.frequency_window_sealed:
                    return
                session.frequency_state = FrequencyState.FAILED
                session.final_frequency = None
                session.errors.append(event.payload)
            case _:
                await run_blocking_operation(self.recovery.audit, "UNKNOWN_EVENT_TYPE", event)

        # 已忽略的事件不继续处理本轮结果。
        if not should_finalize:
            return

        # 判断 OCR 或频率是否整轮失败，停止本轮处理并清理资源。
        if (
            session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}
            or session.frequency_state == FrequencyState.FAILED
        ):
            await self.fail_measurement(session)
            return

        # 状态更新后，统一判断本 Session OCR 是否结束并触发一次文字和图片终选。
        if self.is_session_ocr_finished(session) and not session.text_postprocessing_started:
            session.text_postprocessing_started = True
            if session.ocr_state not in {OCRState.FAILED, OCRState.TIMED_OUT}:
                self.text_recognizer.select_final_text_and_img(
                    session.recognition_results, session.memory_frames
                )

        # 文字终选检查后，继续执行原有的测量结算检查。
        await self.try_finalize(session)

    async def handle_commit_result(self, session: BeltSession, event: MeasurementEvent) -> None:
        """处理提交成功或失败回调，更新原测量档案的提交状态。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            None: 更新业务状态，不返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 判断本轮是否正在等待数据库结果。
        if session.state != SessionState.WAITING_COMMIT_DB:
            return

        # 判断提交是否成功，标记已入库并移除本轮档案。
        if event.event_type == "CommitSucceeded":
            session.state = SessionState.COMMITTED
            logger.info(
                "已保存 machine_id=%s session_id=%s",
                session.machine_id,
                session.session_id,
            )
            self.sessions.pop(session.session_id)
        else:
            # 登记提交失败原因，打印日志并清理本轮档案。
            session.errors.append(event.payload["error_code"])
            await self.fail_measurement(session)

    async def fail_measurement(self, session: BeltSession) -> None:
        """标记本轮失败并清理资源，保留尚未关闭的现场周期身份。

        Args:
            session: 处理失败、中断或提交失败的测量档案。

        Returns:
            None  # 已打印失败日志并清理资源，活动周期保留至 CLOSE 或中断
        """
        # 标记本轮失败并打印机器、周期和错误明细。
        session.state = SessionState.FAILED
        session.finish_time = datetime.now(timezone.utc).isoformat()
        logger.error(
            "测量失败 machine_id=%s session_id=%s errors=%s",
            session.machine_id,
            session.session_id,
            session.errors,
        )

        # 释放本轮图片、识别结果和未消费批次。
        session.memory_frames.clear()
        session.selected_frames.clear()
        session.recognition_results.clear()
        self.text_recognizer.discard_session_batches(session.session_id)
        session.pending_recognition_batches = 0
        session.evidence_validation_pending = False
        session.frozen_payload = None
        session.payload_hash = None

        # 取消本轮处理期限，活动周期保留等待真实关闭的期限。
        for deadline_key in tuple(self.deadline_tasks):
            if deadline_key[0] != session.session_id:
                continue
            if (
                self.active_session_id == session.session_id
                and deadline_key[1] == "CycleTimeout"
            ):
                continue
            self.deadline_tasks.pop(deadline_key).cancel()

        # 停止活动周期的频率交付与相机生产，保留活动编号等待 CLOSE。
        if self.active_session_id == session.session_id:
            self.frequency_adapter.active_session_id = None
            session.frequency_window_sealed = True
            if session.frequency_state == FrequencyState.RUNNING:
                session.frequency_state = FrequencyState.FAILED
            await self.camera.seal_capture(session.capture_id)
        else:
            # 移除已经结束现场阶段的失败档案。
            self.sessions.pop(session.session_id)

    async def handle_capture_sealed(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """封口图像窗口并保存本轮采集统计和错误。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 忽略本轮重复到达的采集封口事件。
        if session.capture_sealed:
            return False

        # 校验封口摘要的采集编号，冲突事件只记录审计。
        summary = event.payload
        if summary.capture_id != session.capture_id:
            await run_blocking_operation(self.recovery.audit, "CAPTURE_IDENTITY_CONFLICT", event)
            return False

        # 标记图像清单已封闭，保存跳帧数量和完整采集统计。
        session.capture_sealed = True
        session.skipped_frame_count = summary.skipped_frame_count
        session.capture_statistics = summary.statistics

        # 采集或证据交付失败时，登记本轮识别失败和错误信息。
        if summary.errors:
            session.ocr_state = OCRState.FAILED
            session.errors.extend(summary.errors)

        # 返回封口处理结果，由事件主流程判断是否开始文字筛选。
        return True

    def is_session_ocr_finished(self, session: BeltSession) -> bool:
        """判断本轮采集是否已封口且已提交的 OCR 批次是否全部结算。

        Args:
            session: 保存采集封口状态和待处理批次数的测量周期。

        Returns:
            True  # 采集已封口且待处理批次数为零，包含已结算的失败批次
            False  # 采集尚未封口或仍有批次等待结算
        """
        # 只判断本轮识别是否结束，不修改周期状态。
        return session.capture_sealed and session.pending_recognition_batches <= 0

    async def handle_frequency_measured(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """按接收顺序保存黑盒交付的新有效测量。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 终态后到达的测量只记录审计，不修改已封闭列表。
        if session.frequency_window_sealed:
            await run_blocking_operation(self.recovery.audit, "LATE_FREQUENCY", event)
            return False

        # 按机器事件队列的接收顺序追加明细，不重复检查黑盒保证的数据约束。
        session.measurement_frequencies.append(event.payload)
        return True

    async def try_finalize(self, session: BeltSession) -> None:
        """检查本轮结果，失败时清理，正常结果冻结后提交数据库。

        Args:
            session: 待检查完成条件的本轮测量档案。

        Returns:
            None  # 条件不足时继续等待，否则冻结并提交本轮记录
        """

        # 判断本轮是否仍在处理。
        if session.state != SessionState.RUNNING:
            return

        # 判断 OCR 或频率是否失败，失败时只打印日志并清理资源。
        if (
            session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}
            or session.frequency_state == FrequencyState.FAILED
        ):
            await self.fail_measurement(session)
            return

        # 判断本轮是否正常关闭、采集是否封口、批次是否全部结算。
        if session.close_time is None:
            return
        if not session.capture_sealed or session.pending_recognition_batches != 0:
            return

        # 判断 OCR 和频率是否均已成功。
        if (
            session.ocr_state != OCRState.SUCCESS
            or session.frequency_state != FrequencyState.SUCCESS
        ):
            return

        # 判断证据是否已验证，启动尚未执行的验证任务。
        if not session.evidence_verified:
            if not session.evidence_validation_pending:
                session.evidence_validation_pending = True
                task = asyncio.create_task(self.validate_evidence(
                    session.session_id, session.ocr_result.evidence_refs,
                ))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
            return

        # 记录结算时间，获取最终频率和 OCR 结果。
        session.finish_time = datetime.now(timezone.utc).isoformat()
        final_frequency = session.final_frequency
        ocr_result = session.ocr_result
        # 组装本轮身份、测量结果、采集统计和配置数据。
        payload = {
            "session_id": session.session_id,
            "machine_id": session.machine_id,
            "camera_id": session.camera_id,
            "frequency_source_id": session.frequency_source_id,
            "start_time": session.start_time,
            "close_time": session.close_time,
            "finish_time": session.finish_time,
            "ordered_lines": list(ocr_result.ordered_lines) if ocr_result else [],
            "evidence_refs": list(ocr_result.evidence_refs) if ocr_result else [],
            "final_frequency_hz": final_frequency.value_hz if final_frequency else None,
            "final_measurement_id": (
                final_frequency.measurement_id if final_frequency else None
            ),
            "selected_frames": [
                {
                    name: value for name, value in asdict(frame).items()
                    if name != "image_data"
                }
                for frame in session.selected_frames.values()
            ],
            "measurement_frequencies": [
                asdict(measurement)
                for measurement in session.measurement_frequencies
            ],
            "skipped_frame_count": session.skipped_frame_count,
            "capture_statistics": session.capture_statistics,
            "outcome": "COMPLETE",
            "error_codes": session.errors.copy(),
            "is_simulated": True,
            "configuration_version": (
                session.configuration_snapshot["configuration_version"]
            ),
            "process_epoch": session.process_epoch,
            "configuration_snapshot": session.configuration_snapshot,
            "model_version": None,
            "software_version": "0.1.0",
        }

        # 冻结提交内容并计算内容哈希。
        session.frozen_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.payload_hash = hashlib.sha256(
            session.frozen_payload.encode()
        ).hexdigest()
        # 释放本轮内存图片并移除剩余排队批次。
        session.memory_frames.clear()
        session.pending_recognition_batches -= (
            self.text_recognizer.discard_session_batches(session.session_id)
        )

        # 撤销已冻结周期的剩余期限任务。
        for deadline_key in tuple(self.deadline_tasks):
            if deadline_key[0] == session.session_id:
                self.deadline_tasks.pop(deadline_key).cancel()
        # 提交本轮冻结记录。
        await self.submit_frozen_record(session)

    async def validate_evidence(
        self, session_id: str, evidence_refs: tuple[str, ...]
    ) -> None:
        """在后台读取证据并把检查结果返回本机队列。"""
        try:
            for evidence_ref in evidence_refs:
                image_content = await asyncio.wait_for(
                    run_blocking_operation(Path(evidence_ref).read_bytes),
                    self.configuration.ocr_job_timeout_ms / 1000,
                )
                if not image_content:
                    raise ValueError("证据文件为空。")
            event_type = "EvidenceValidated"
        except Exception:
            logger.exception("最终证据检查失败 session_id=%s", session_id)
            event_type = "EvidenceFailed"
        await self.publish_event(MeasurementEvent(
            event_type, self.machine.machine_id, session_id,
        ))

    async def submit_frozen_record(self, session: BeltSession) -> None:
        """提交本轮正常结果，提交异常或队列满时结束本轮任务。

        Args:
            session: 已冻结正常结果的测量档案。

        Returns:
            None  # 本轮等待数据库回调，或已标记失败并完成清理
        """
        # 标记等待入库，组装本轮冻结记录。
        session.state = SessionState.WAITING_COMMIT_DB
        request = DatabaseRequest(
            session.machine_id, session.session_id,
            session.frozen_payload, session.payload_hash,
        )

        # 提交存储队列，登记入队异常或容量不足。
        try:
            accepted = await self.database.submit(request)
        except Exception:
            logger.exception("提交异常 session_id=%s", session.session_id)
            session.errors.append("DATABASE_SUBMIT_FAILED")
        else:
            if accepted:
                return
            session.errors.append("DATABASE_QUEUE_FULL")

        # 打印失败日志并清理本轮档案。
        await self.fail_measurement(session)

    def schedule_timeout(
        self, session: BeltSession, event_type: str, timeout_ms: int
    ) -> None:
        """安排本轮期限事件。"""
        async def publish_timeout() -> None:
            await asyncio.sleep(timeout_ms / 1000)
            await self.publish_event(MeasurementEvent(
                event_type, session.machine_id, session.session_id,
            ))

        self.deadline_tasks[(session.session_id, event_type)] = asyncio.create_task(
            publish_timeout()
        )
