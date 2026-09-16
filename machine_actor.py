"""串行处理一台机器的启动、关闭和后台结果。"""

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from camera import FolderCamera
from configuration import MachineConfiguration, MeasurementConfiguration
from frequency import SimulatedFrequency
from models import BeltSession, MeasurementEvent, PublishEvent
from ocr import OCRJob, SimulatedOCR
from storage import SQLiteWriter, StorageRequest


logger = logging.getLogger(__name__)


class MachineActor:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        camera: FolderCamera,
        frequency: SimulatedFrequency,
        ocr: SimulatedOCR,
        storage: SQLiteWriter,
        publish_event: PublishEvent,
        state_changed: asyncio.Event,
    ) -> None:
        self.machine = machine
        self.configuration = configuration
        self.camera = camera
        self.frequency = frequency
        self.ocr = ocr
        self.storage = storage
        self.publish_event = publish_event
        self.state_changed = state_changed
        self.queue: asyncio.Queue[MeasurementEvent] = asyncio.Queue(
            configuration.event_queue_capacity
        )
        self.active_session_id: str | None = None
        self.sessions: dict[str, BeltSession] = {}
        self.waiting_cycle_reset = False
        self.deadline_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def run(self) -> None:
        """按队列顺序修改本机 Session 状态。"""
        while True:
            event = await self.queue.get()
            try:
                self.process_event(event)
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_result(None)
            except Exception:
                logger.exception(
                    "业务处理失败 machine_id=%s session_id=%s event=%s",
                    event.machine_id, event.session_id, event.event_type,
                )
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_exception(RuntimeError("测量处理失败。"))
            finally:
                self.state_changed.set()
                self.queue.task_done()

    def start_measurement(self) -> None:
        """检查接收条件并启动本轮图像和频率窗口。"""
        # 忽略活动周期内的重复启动和未同步的启动。
        if self.active_session_id is not None or self.waiting_cycle_reset:
            return
        if (
            len(self.sessions) >= self.configuration.max_pending_sessions_per_machine
            or not self.storage.available
        ):
            self.waiting_cycle_reset = True
            rejection = {
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "reason": "CAPACITY_OR_STORAGE_UNAVAILABLE",
                "is_simulated": True,
            }
            self.storage.submit(StorageRequest(
                self.machine.machine_id, uuid4().hex,
                json.dumps(rejection), "", "rejected_cycle",
            ))
            logger.error("本轮未受理 machine_id=%s", self.machine.machine_id)
            return

        # 创建本轮档案并登记活动位置。
        session = BeltSession(
            session_id=uuid4().hex,
            machine_id=self.machine.machine_id,
            camera_id=self.machine.camera_id,
            frequency_source_id=self.machine.frequency_source_id,
            capture_id=uuid4().hex,
            start_time=datetime.now(timezone.utc).isoformat(),
            start_boundary=asyncio.get_running_loop().time(),
        )
        self.sessions[session.session_id] = session
        self.active_session_id = session.session_id
        logger.info(
            "开始测量 machine_id=%s session_id=%s", session.machine_id, session.session_id,
        )

        # 启动独立采集并设置本轮最大运行期限。
        self.camera.start_capture(
            session.session_id, session.capture_id, session.start_boundary,
        )
        self.frequency.open_window(session.session_id)
        self.schedule_timeout(
            session, "CycleTimeout", self.configuration.max_cycle_open_ms,
        )
        self.schedule_timeout(
            session, "OCRTimeout", self.configuration.ocr_result_timeout_ms,
        )

    def close_measurement(self, interrupted: bool = False) -> None:
        """关闭现场窗口、释放活动位置并检查本轮结果。"""
        if self.active_session_id is None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return

        # 记录正常关闭或明确中断的现场边界。
        session = self.sessions[self.active_session_id]
        session.close_boundary = asyncio.get_running_loop().time()
        session.cycle_state = "INTERRUPTED" if interrupted else "CLOSED"
        if interrupted:
            session.errors.append("CYCLE_INTERRUPTED")
            self.waiting_cycle_reset = True
        else:
            session.close_time = datetime.now(timezone.utc).isoformat()

        # 封闭本轮窗口并释放本机活动位置。
        self.camera.seal_capture(session.capture_id)
        self.frequency.seal_window(session.session_id)
        self.active_session_id = None
        deadline_task = self.deadline_tasks.pop(
            (session.session_id, "CycleTimeout"), None,
        )
        if deadline_task is not None:
            deadline_task.cancel()
        self.try_finalize(session)

    def process_event(self, event: MeasurementEvent) -> None:
        """校验事件归属，更新状态并触发完成检查。"""
        if event.machine_id != self.machine.machine_id:
            logger.warning("隔离机器归属不符事件 event_id=%s", event.event_id)
            return

        # 处理不依赖已有 Session 的入口事件。
        if event.event_type == "MachineStarted":
            self.start_measurement()
            return
        if event.event_type == "MachineClosed":
            self.close_measurement()
            return
        if event.event_type == "Shutdown":
            self.close_measurement(interrupted=True)
            return

        # 将异步结果定位到原 Session。
        session = self.sessions.get(event.session_id)
        if session is None:
            logger.warning(
                "隔离未知或已结算事件 machine_id=%s session_id=%s event=%s",
                event.machine_id, event.session_id, event.event_type,
            )
            return

        # 提交回调只更新目标档案，不修改活动位置。
        if event.event_type in {"CommitSucceeded", "CommitFailed"}:
            if session.commit_state != "COMMITTING":
                return
            if event.event_type == "CommitSucceeded":
                session.commit_state = "COMMITTED"
                logger.info(
                    "已保存 machine_id=%s session_id=%s outcome=%s",
                    session.machine_id, session.session_id, session.outcome,
                )
                self.sessions.pop(session.session_id)
            else:
                session.commit_state = "RETRY_PENDING"
                logger.error(
                    "记录待重试 machine_id=%s session_id=%s",
                    session.machine_id, session.session_id,
                )
            return
        if event.event_type == "RetryCommit":
            if session.commit_state == "RETRY_PENDING":
                self.submit_frozen_record(session)
            return
        if session.frozen_payload is not None:
            logger.warning(
                "隔离冻结后的迟到事件 session_id=%s event=%s",
                session.session_id, event.event_type,
            )
            return

        # 登记采集窗口内的帧，按帧编号去重。
        if event.event_type == "FrameSelected":
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
                logger.warning("隔离归属不符图像 session_id=%s", session.session_id)
                return
            session.selected_frames.setdefault(frame.frame_id, frame)
        elif event.event_type == "CaptureSealed":
            if session.capture_sealed:
                return
            session.capture_sealed = True
            session.skipped_frame_count = event.payload
            if session.ocr_state == "WAITING":
                job = OCRJob(
                    session.machine_id, session.session_id,
                    tuple(session.selected_frames.values()),
                    self.machine.simulated_lines,
                )
                if not session.selected_frames:
                    session.ocr_state = "FAILED"
                    session.errors.append("CAPTURE_NO_FRAMES")
                elif self.ocr.submit(job):
                    session.ocr_state = "RUNNING"
                else:
                    session.ocr_state = "FAILED"
                    session.errors.append("OCR_QUEUE_FULL")

        # 接收 OCR 终态结果，忽略重复或超时后的结果。
        elif event.event_type == "OCRCompleted":
            if session.ocr_state != "RUNNING":
                return
            result = event.payload
            if (
                not result.ordered_lines or not result.evidence_refs
                or set(result.frame_ids) != set(session.selected_frames)
                or result.evidence_refs != tuple(
                    frame.image_path for frame in session.selected_frames.values()
                )
            ):
                session.ocr_state = "FAILED"
                session.errors.append("OCR_INVALID_RESULT")
            else:
                session.ocr_result = result
                session.ocr_state = "SUCCESS"
        elif event.event_type in {"OCRFailed", "CaptureFailed", "OCRTimeout"}:
            if session.ocr_state in {"SUCCESS", "FAILED", "TIMED_OUT"}:
                return
            session.ocr_state = (
                "TIMED_OUT" if event.event_type == "OCRTimeout" else "FAILED"
            )
            session.errors.append(event.payload or "OCR_TIMEOUT")

        # 按测量身份收集有效频率，并校验现场窗口边界。
        elif event.event_type == "FrequencyMeasured":
            measurement = event.payload
            if (
                session.frequency_window_sealed
                or measurement.session_id != session.session_id
                or measurement.frequency_source_id != session.frequency_source_id
                or measurement.measured_monotonic < session.start_boundary
                or (
                    session.close_boundary is not None
                    and measurement.measured_monotonic > session.close_boundary
                )
            ):
                logger.warning("隔离归属不符频率 session_id=%s", session.session_id)
                return
            if math.isfinite(measurement.value_hz) and (
                self.configuration.minimum_frequency_hz
                <= measurement.value_hz <= self.configuration.maximum_frequency_hz
            ):
                session.frequency_candidates.setdefault(
                    measurement.measurement_id, measurement,
                )
        elif event.event_type == "FrequencyWindowSealed":
            if session.frequency_window_sealed or session.cycle_state == "OPEN":
                return
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
        elif event.event_type == "FrequencyFailed":
            if session.frequency_state != "FINAL_INVALID":
                session.frequency_state = "FINAL_INVALID"
                session.errors.append(event.payload)

        # 长时间未关闭时记录中断，不生成正常 CLOSE。
        elif event.event_type == "CycleTimeout":
            if self.active_session_id == session.session_id:
                session.errors.append("CYCLE_TIMEOUT")
                self.close_measurement(interrupted=True)
            return
        self.try_finalize(session)

    def try_finalize(self, session: BeltSession) -> None:
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
        if session.outcome == "COMPLETE":
            try:
                for evidence_ref in session.ocr_result.evidence_refs:
                    with Path(evidence_ref).open("rb") as evidence_file:
                        if not evidence_file.read(1):
                            raise ValueError("证据文件为空。")
            except Exception:
                logger.exception("最终证据检查失败 session_id=%s", session.session_id)
                session.ocr_state = "FAILED"
                session.outcome = "REVIEW_REQUIRED"
                session.errors.append("EVIDENCE_UNAVAILABLE")

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
            "outcome": session.outcome,
            "error_codes": session.errors.copy(),
            "is_simulated": True,
            "configuration_version": self.configuration.configuration_version,
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
        self.submit_frozen_record(session)

    def submit_frozen_record(self, session: BeltSession) -> None:
        """提交同一份冻结记录，并保留未成功入队的记录。"""
        session.commit_state = "COMMITTING"
        request = StorageRequest(
            session.machine_id, session.session_id,
            session.frozen_payload, session.payload_hash,
        )
        if not self.storage.submit(request):
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
