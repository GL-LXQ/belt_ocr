"""串行处理一台机器的启动、关闭和后台结果。"""

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from camera import SessionCamera
from configuration import MachineConfiguration, MeasurementConfiguration
from frequency import SimulatedFrequency
from enums import MachineState
from models import BeltSession, MeasurementEvent, OCRResult, PublishEvent
from recovery import run_blocking_operation, serialize_value
from ocr import OCRJob, SimulatedOCR
from database import Database, DatabaseRequest


logger = logging.getLogger(__name__)


class MachineManager:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        camera: SessionCamera,
        frequency: SimulatedFrequency,
        ocr: SimulatedOCR,
        database: Database,
        publish_event: PublishEvent,
        state_changed: asyncio.Event,
    ) -> None:
        self.machine = machine
        self.configuration = configuration
        self.camera = camera
        self.frequency = frequency
        self.ocr = ocr
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
            # 标记等待周期复位，组装本轮未受理记录。
            self.waiting_cycle_reset = True
            rejection = {
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "reason": "CAPACITY_OR_STORAGE_UNAVAILABLE",
                "is_simulated": True,
            }

            # 提交未受理记录，记录日志并结束本次启动处理。
            await self.database.submit(
                DatabaseRequest(self.machine.machine_id, uuid4().hex, json.dumps(rejection), "", "rejected_cycle")
            )
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
        # 后台采集启动后，打开当前 Session 的频率接收窗口。
        self.frequency.open_window(session.session_id)

        # 安排本轮运行超时和 OCR 超时事件。
        self.schedule_timeout(session, "CycleTimeout", self.configuration.max_cycle_open_ms)
        # 登记本轮 OCR 等待期限，此处只安排超时事件。
        self.schedule_timeout(session, "OCRTimeout", self.configuration.ocr_result_timeout_ms)

    async def close_measurement(self, interrupted: bool = False) -> None:
        """关闭现场窗口、释放活动位置并检查本轮结果。"""
        if self.active_session_id is None:
            if not interrupted:
                self.waiting_cycle_reset = False
                self.interrupted_session_id = None
            return

        # 记录正常关闭或明确中断的现场边界。
        session = self.sessions[self.active_session_id]
        session.close_boundary = asyncio.get_running_loop().time()
        session.cycle_state = "INTERRUPTED" if interrupted else "CLOSED"
        if interrupted:
            session.errors.append("CYCLE_INTERRUPTED")
            self.waiting_cycle_reset = True
            self.interrupted_session_id = session.session_id
        else:
            self.interrupted_session_id = None
            session.close_time = datetime.now(timezone.utc).isoformat()

        # 封闭本轮窗口并释放本机活动位置。
        await self.camera.seal_capture(session.capture_id, session.close_boundary)
        self.frequency.seal_window(session.session_id)
        self.active_session_id = None
        deadline_task = self.deadline_tasks.pop(
            (session.session_id, "CycleTimeout"), None,
        )
        if deadline_task is not None:
            deadline_task.cancel()
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
            await run_blocking_operation(self.recovery.audit, "UNKNOWN_OR_SETTLED_SESSION", event)
            logger.warning(
                "隔离未知或已结算事件 machine_id=%s session_id=%s event=%s",
                event.machine_id,
                event.session_id,
                event.event_type,
            )
            return

        # 在冻结检查前处理提交回调和补交请求。
        match event.event_type:
            case "CommitSucceeded" | "CommitFailed":
                await self.handle_commit_result(session, event)
                return
            case "RetryCommit":
                if session.commit_state == "RETRY_PENDING":
                    await self.submit_frozen_record(session)
                return

        # 隔离档案冻结后到达的采集和识别结果。
        if session.frozen_payload is not None:
            await run_blocking_operation(self.recovery.audit, "LATE_FROZEN_RESULT", event)
            logger.warning("隔离冻结后的迟到事件 session_id=%s event=%s", session.session_id, event.event_type)
            return

        # 分派采集结果，保留各事件是否继续结算的处理决定。
        should_finalize = True
        match event.event_type:
            case "FrameSelected":
                should_finalize = await self.handle_frame_selected(session, event)
            case "FrameBatchSelected":
                # 按帧检查批次归属并登记图片，批次接收阶段不提交 OCR。
                should_finalize = False
                for frame in event.payload:
                    frame_event = MeasurementEvent("FrameSelected", event.machine_id, event.session_id, frame)
                    frame_selected = await self.handle_frame_selected(session, frame_event)
                    should_finalize = frame_selected or should_finalize
            case "CaptureSealed":
                should_finalize = await self.handle_capture_sealed(session, event)
            case "OCRFrameStarted" | "OCRFrameCompleted" | "OCRFrameFailed":
                await self.settle_ocr_frame(session, event)
            case "OCRCompleted":
                should_finalize = await self.handle_ocr_completed(session, event)
            case "EvidenceValidated" | "EvidenceFailed":
                # 结算证据校验，登记成功状态或失败原因。
                session.evidence_validation_pending = False
                if event.event_type == "EvidenceValidated":
                    session.evidence_verified = True
                else:
                    session.ocr_state = "FAILED"
                    session.errors.append("EVIDENCE_UNAVAILABLE")
            case "OCRFailed" | "CaptureFailed" | "OCRTimeout":
                # 忽略已有 OCR 终态，登记本次失败或超时。
                if session.ocr_state in {"FAILED", "TIMED_OUT"}:
                    return
                if session.ocr_state == "SUCCESS":
                    return
                session.ocr_state = "TIMED_OUT" if event.event_type == "OCRTimeout" else "FAILED"
                session.errors.append(event.payload or "OCR_TIMEOUT")
            case "FrequencyMeasured":
                should_finalize = await self.handle_frequency_measured(session, event)
            case "FrequencyWindowSealed":
                should_finalize = await self.handle_frequency_window_sealed(session, event)
            case "FrequencyFailed":
                if session.frequency_state != "FINAL_INVALID":
                    session.frequency_state = "FINAL_INVALID"
                    session.errors.append(event.payload)
            case "CycleTimeout":
                # 仅中断对应的现场活动周期。
                if self.active_session_id == session.session_id:
                    session.errors.append("CYCLE_TIMEOUT")
                    await self.close_measurement(interrupted=True)
                return
            case _:
                await run_blocking_operation(self.recovery.audit, "UNKNOWN_EVENT_TYPE", event)

        # 对需要继续结算的事件统一检查本轮结果。
        if should_finalize:
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
        # 忽略未处于提交或重试阶段的回调。
        if session.commit_state not in {"COMMITTING", "RETRY_PENDING"}:
            return

        # 提交成功时移除原档案，失败时登记重试或冲突状态。
        if event.event_type == "CommitSucceeded":
            session.commit_state = "COMMITTED"
            logger.info(
                "已保存 machine_id=%s session_id=%s outcome=%s",
                session.machine_id,
                session.session_id,
                session.outcome,
            )
            self.sessions.pop(session.session_id)
        else:
            session.commit_state = "CONFLICT" if event.payload.get("integrity_conflict") else "RETRY_PENDING"
            if session.commit_state == "CONFLICT":
                self.device_faults.add("COMMIT_INTEGRITY_CONFLICT")
            logger.error("记录待重试 machine_id=%s session_id=%s", session.machine_id, session.session_id)

    async def handle_frame_selected(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """校验图片归属并按帧编号登记本轮图片。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 检查图片的档案、设备、窗口和采集时间归属。
        frame = event.payload
        if (
            frame.session_id != session.session_id
            or frame.camera_id != session.camera_id
            or frame.capture_id != session.capture_id
            or frame.captured_monotonic < session.start_boundary
            or session.capture_sealed
            or (
                session.close_boundary is not None
                and frame.captured_monotonic > session.close_boundary
            )
        ):
            await run_blocking_operation(self.recovery.audit, "FRAME_OWNERSHIP_CONFLICT", event)
            logger.warning("隔离归属不符图像 session_id=%s", session.session_id)
            return False

        # 按帧编号登记图片，并继续本轮完成检查。
        session.selected_frames.setdefault(frame.frame_id, frame)
        return True

    async def handle_capture_sealed(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """封口图像窗口并提交本轮已收集的图片任务。

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
            session.ocr_state = "FAILED"
            session.errors.extend(summary.errors)

        # 等待识别时提交已选图片，没有图片则标记失败。
        if session.ocr_state == "WAITING":
            if not session.selected_frames:
                session.ocr_state = "FAILED"
                session.errors.append("CAPTURE_NO_FRAMES")
            else:
                await self.submit_ocr_frames(session)
        return True

    async def handle_ocr_completed(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """校验整轮 OCR 结果并更新识别状态。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 仅接收正在处理中的整轮 OCR 结果。
        if session.ocr_state != "RUNNING":
            return False

        # 核对文字、帧清单和证据路径，登记识别成功或失败。
        result = event.payload
        if (
            not result.ordered_lines or not result.evidence_refs
            or set(result.frame_ids) != set(session.selected_frames)
            or result.evidence_refs != tuple(frame.image_path for frame in session.selected_frames.values())
        ):
            session.ocr_state = "FAILED"
            session.errors.append("OCR_INVALID_RESULT")
        else:
            session.ocr_result = result
            session.ocr_state = "SUCCESS"
        return True

    async def handle_frequency_measured(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """校验频率归属和测量身份并收集有效候选值。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 读取本次频率测量。
        measurement = event.payload
        # 周期身份冲突时保留目标档案为待复核，不转交其他周期。
        if measurement.session_id != session.session_id:
            session.frequency_state = "FINAL_INVALID"
            if "AMBIGUOUS_MEASUREMENT" not in session.errors:
                session.errors.append("AMBIGUOUS_MEASUREMENT")
            await run_blocking_operation(self.recovery.audit, "AMBIGUOUS_MEASUREMENT", event)
            return True

        # 隔离窗口已封口、来源不符或超出周期边界的测量。
        if (
            session.frequency_window_sealed
            or measurement.frequency_source_id != session.frequency_source_id
            or measurement.measured_monotonic < session.start_boundary
            or (
                session.close_boundary is not None
                and measurement.measured_monotonic > session.close_boundary
            )
        ):
            await run_blocking_operation(self.recovery.audit, "AMBIGUOUS_MEASUREMENT", event)
            logger.warning("隔离归属不符频率 session_id=%s", session.session_id)
            return False

        # 对有效频率检查重复测量身份，登记冲突或保存候选值。
        if math.isfinite(measurement.value_hz) and (
            self.configuration.minimum_frequency_hz
            <= measurement.value_hz <= self.configuration.maximum_frequency_hz
        ):
            previous = session.frequency_candidates.get(measurement.measurement_id)
            if previous is not None and (
                previous.value_hz != measurement.value_hz
                or previous.source_sequence != measurement.source_sequence
                or previous.measured_at != measurement.measured_at
            ):
                session.frequency_state = "FINAL_INVALID"
                session.errors.append("AMBIGUOUS_MEASUREMENT")
                await run_blocking_operation(self.recovery.audit, "MEASUREMENT_ID_CONFLICT", event)
            else:
                session.frequency_candidates.setdefault(measurement.measurement_id, measurement)
        return True

    async def handle_frequency_window_sealed(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """封口频率窗口并确定本轮最后一次有效测量。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 忽略重复封口和周期尚未结束的封口事件。
        if session.frequency_window_sealed or session.cycle_state == "OPEN":
            return False

        # 关闭频率窗口，按来源序号选取最后一次有效测量。
        session.frequency_window_sealed = True
        if session.frequency_state != "FINAL_INVALID":
            if session.frequency_candidates:
                session.final_frequency = max(
                    session.frequency_candidates.values(),
                    key=lambda measurement: measurement.source_sequence,
                )
                session.frequency_state = "FINAL_VALID"
            else:
                session.frequency_state = "FINAL_INVALID"
                session.errors.append("FREQUENCY_NO_VALID_MEASUREMENT")
        return True

    async def try_finalize(self, session: BeltSession) -> None:
        """检查结果完整性并冻结本轮最终记录。"""
        if session.frozen_payload is not None or session.cycle_state == "OPEN":
            return

        # 确认本轮是中断、待复核还是完整结果。
        has_terminal_failure = (
            session.ocr_state in {"FAILED", "TIMED_OUT"}
            or session.frequency_state == "FINAL_INVALID"
        )
        if session.cycle_state == "INTERRUPTED":
            session.outcome = "INTERRUPTED"
        elif has_terminal_failure:
            session.outcome = "REVIEW_REQUIRED"
        elif session.ocr_done and session.frequency_done and session.cycle_closed:
            session.outcome = "COMPLETE"
        else:
            return

        # 在冻结正常记录前确认本地证据仍可读取。
        if session.outcome == "COMPLETE" and not session.evidence_verified:
            if not session.evidence_validation_pending:
                session.evidence_validation_pending = True
                task = asyncio.create_task(self.validate_evidence(
                    session.session_id, session.ocr_result.evidence_refs,
                ))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
            return

        # 组装查询字段、原始候选、证据和模拟标记。
        session.finish_time = datetime.now(timezone.utc).isoformat()
        final_frequency = session.final_frequency
        ocr_result = session.ocr_result
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
                asdict(frame) for frame in session.selected_frames.values()
            ],
            "frequency_candidates": [
                asdict(measurement)
                for measurement in session.frequency_candidates.values()
            ],
            "skipped_frame_count": session.skipped_frame_count,
            "capture_statistics": session.capture_statistics,
            "outcome": session.outcome,
            "error_codes": session.errors.copy(),
            "is_simulated": True,
            "configuration_version": (
                session.configuration_snapshot["configuration_version"]
            ),
            "process_epoch": session.process_epoch,
            "configuration_snapshot": session.configuration_snapshot,
            "ocr_jobs": session.ocr_jobs,
            "model_version": "simulated-ocr-v1",
            "software_version": "0.1.0",
        }

        # 固定提交内容并撤销本轮剩余期限任务。
        session.frozen_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.payload_hash = hashlib.sha256(
            session.frozen_payload.encode()
        ).hexdigest()
        for deadline_key in tuple(self.deadline_tasks):
            if deadline_key[0] == session.session_id:
                self.deadline_tasks.pop(deadline_key).cancel()
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

    async def submit_ocr_frames(self, session: BeltSession) -> None:
        """登记逐帧任务并将未完成任务提交给共享 OCR 调度器。

        Args:
            session: 本轮测量档案，包含已选图片、OCR 任务进度和配置快照。

        Returns:
            None: 不返回数据，更新档案中的 OCR 状态并提交任务。
            返回示例：
                None  # 无返回数据
        """
        # 为每张图片登记 OCR 任务，保留已有任务的处理进度。
        for frame in session.selected_frames.values():
            session.ocr_jobs.setdefault(
                frame.frame_id,
                {
                    "job_id": uuid4().hex,
                    "state": "WAITING",
                    "attempt": 0,
                    "ordered_lines": [],
                },
            )

        # 在内存中标记本轮进入 OCR 处理阶段。
        session.ocr_state = "RUNNING"

        # 从本轮配置快照中取出包含模拟文字的机器配置。
        machine_settings = next(
            machine for machine in session.configuration_snapshot["machines"]
            if machine["machine_id"] == session.machine_id
        )
        for frame_id, job_state in session.ocr_jobs.items():
            # 跳过已识别成功的图片任务。
            if job_state["state"] == "SUCCESS":
                continue

            # 达到尝试次数上限时，标记本轮失败并停止提交。
            if job_state["attempt"] >= self.configuration.ocr_retry_attempts:
                session.ocr_state = "FAILED"
                session.errors.append("OCR_RETRIES_EXHAUSTED")
                break

            # 组装携带机器、档案、任务编号和图片信息的 OCR 任务。
            job = OCRJob(
                session.machine_id,
                session.session_id,
                job_state["job_id"],
                session.selected_frames[frame_id],
                tuple(machine_settings["simulated_lines"]),
                job_state["attempt"],
            )

            # 提交到共享 OCR 队列，队列满时标记失败并停止提交。
            if not self.ocr.submit(job):
                session.ocr_state = "FAILED"
                session.errors.append("OCR_QUEUE_FULL")
                break

    async def settle_ocr_frame(self, session: BeltSession, event: MeasurementEvent) -> None:
        """登记单帧识别进度和结果，并在全部帧结算后更新本轮 OCR 状态。

        Args:
            session: 当前事件所属的测量档案，保存图片任务和本轮 OCR 状态。
            event: 包含事件类型、帧编号、任务编号、尝试次数和识别结果的事件。

        Returns:
            None: 更新测量档案中的任务记录和 OCR 状态，不返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 读取事件数据，找到对应图片的任务记录。
        payload = event.payload
        job_state = session.ocr_jobs.get(payload["frame_id"])

        # 计算当前事件应携带的尝试次数。
        expected_attempt = (
            job_state["attempt"] + (event.event_type == "OCRFrameStarted")
            if job_state is not None
            else None
        )

        # 核对本轮状态、任务身份和尝试次数，记录并忽略不匹配的事件。
        if (
            session.ocr_state != "RUNNING"
            or job_state is None
            or job_state["job_id"] != payload["job_id"]
            or job_state["state"] in {"SUCCESS", "FAILED"}
            or payload["attempt"] != expected_attempt
        ):
            await run_blocking_operation(self.recovery.audit, "STALE_OCR_ATTEMPT", event)
            return

        # 更新尝试次数，收到开始事件时登记运行状态并返回。
        job_state["attempt"] = payload["attempt"]
        if event.event_type == "OCRFrameStarted":
            job_state["state"] = "RUNNING"
            return

        # 登记单帧最终状态和识别文字。
        job_state["state"] = "SUCCESS" if event.event_type == "OCRFrameCompleted" else "FAILED"
        job_state["ordered_lines"] = payload.get("ordered_lines", [])

        # 将单帧识别失败的错误码写入本轮档案。
        if event.event_type == "OCRFrameFailed":
            session.errors.append(payload["error_code"])

        # 还有图片未结算时，等待后续识别事件。
        if any(job["state"] not in {"SUCCESS", "FAILED"} for job in session.ocr_jobs.values()):
            return

        # 全部图片结算后，只要存在失败任务就标记本轮 OCR 失败。
        if any(job["state"] == "FAILED" for job in session.ocr_jobs.values()):
            session.ocr_state = "FAILED"
            return

        # 取第一项任务的模拟文字，收集全部选中图片的路径和帧编号。
        first_job = next(iter(session.ocr_jobs.values()))
        session.ocr_result = OCRResult(
            tuple(first_job["ordered_lines"]),
            tuple(frame.image_path for frame in session.selected_frames.values()),
            tuple(session.selected_frames),
        )

        # 将本轮 OCR 标记为成功。
        session.ocr_state = "SUCCESS"

    async def submit_frozen_record(self, session: BeltSession) -> None:
        """提交同一份冻结记录，并保留未成功入队的记录。"""
        session.commit_state = "COMMITTING"
        request = DatabaseRequest(
            session.machine_id, session.session_id,
            session.frozen_payload, session.payload_hash,
        )
        if not await self.database.submit(request):
            session.commit_state = "RETRY_PENDING"
            logger.error("存储队列已满 session_id=%s", session.session_id)

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
