"""提供有界的共享 OCR 调度和模拟识别结果。"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

from configuration import MeasurementConfiguration
from recovery import run_blocking_operation
from models import CapturedFrame, MeasurementEvent, PublishEvent


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OCRJob:
    machine_id: str
    session_id: str
    job_id: str
    frame: CapturedFrame
    simulated_lines: tuple[str, ...]
    completed_attempts: int = 0


class SimulatedOCR:
    def __init__(
        self, configuration: MeasurementConfiguration, publish_event: PublishEvent
    ) -> None:
        self.configuration = configuration
        self.publish_event = publish_event
        self.machine_queues: dict[str, deque[OCRJob]] = {
            machine.machine_id: deque() for machine in configuration.machines
        }
        self.machine_order = deque(self.machine_queues)
        self.jobs_available = asyncio.Event()
        self.pending_count = 0
        self.registered_jobs: set[str] = set()
        self.accepting_jobs = True
        self.stopping = False

    def submit(self, job: OCRJob) -> bool:
        """把本轮全部选中帧加入共享队列。"""
        if job.job_id in self.registered_jobs:
            return True
        if (
            not self.accepting_jobs
            or self.pending_count >= self.configuration.ocr_queue_capacity
        ):
            return False
        self.machine_queues[job.machine_id].append(job)
        self.pending_count += 1
        self.registered_jobs.add(job.job_id)
        self.jobs_available.set()
        return True

    async def run(self) -> None:
        """按机器轮转处理任务并返回模拟筛选后的文字行。"""
        while not self.stopping:
            await self.jobs_available.wait()

            # 从下一台有任务的机器取出最早提交的 Session。
            job = None
            for machine_number in range(len(self.machine_order)):
                machine_id = self.machine_order[0]
                self.machine_order.rotate(-1)
                if self.machine_queues[machine_id]:
                    job = self.machine_queues[machine_id].popleft()
                    break
            if job is None:
                self.jobs_available.clear()
                continue

            requeued = False
            attempt = job.completed_attempts
            try:
                if attempt >= self.configuration.ocr_retry_attempts:
                    await self.publish_event(MeasurementEvent(
                        "OCRFrameFailed", job.machine_id, job.session_id, {
                            "job_id": job.job_id, "frame_id": job.frame.frame_id,
                            "attempt": attempt, "error_code": "OCR_RETRIES_EXHAUSTED",
                        },
                    ))
                    continue
                # 为每次尝试发布任务身份，有限重试当前帧。
                for attempt in range(
                    job.completed_attempts + 1,
                    self.configuration.ocr_retry_attempts + 1,
                ):
                    payload = {
                        "job_id": job.job_id, "frame_id": job.frame.frame_id,
                        "attempt": attempt,
                    }
                    acknowledgement = asyncio.get_running_loop().create_future()
                    await self.publish_event(MeasurementEvent(
                        "OCRFrameStarted", job.machine_id, job.session_id, payload,
                        acknowledgement=acknowledgement,
                    ))
                    await acknowledgement
                    try:
                        lines = await asyncio.wait_for(
                            self.recognize_frame(job),
                            self.configuration.ocr_job_timeout_ms / 1000,
                        )
                        if not lines:
                            payload = {**payload, "error_code": "OCR_NO_VALID_TEXT"}
                            event_type = "OCRFrameFailed"
                        else:
                            payload = {**payload, "ordered_lines": lines}
                            event_type = "OCRFrameCompleted"
                        await self.publish_event(MeasurementEvent(
                            event_type, job.machine_id, job.session_id, payload,
                        ))
                        break
                    except Exception:
                        logger.exception(
                            "模拟 OCR 失败 machine_id=%s session_id=%s job_id=%s",
                            job.machine_id, job.session_id, job.job_id,
                        )
                        if attempt == self.configuration.ocr_retry_attempts:
                            payload = {
                                **payload, "error_code": "OCR_PROCESSING_FAILED",
                            }
                            await self.publish_event(MeasurementEvent(
                                "OCRFrameFailed", job.machine_id,
                                job.session_id, payload,
                            ))
            except (asyncio.CancelledError, Exception):
                # 意外退出时把当前帧交给下一次工作任务继续尝试。
                if not self.stopping:
                    self.machine_queues[job.machine_id].appendleft(replace(
                        job, completed_attempts=attempt,
                    ))
                    self.pending_count += 1
                    requeued = True
                raise
            finally:
                self.pending_count -= 1
                if not requeued:
                    self.registered_jobs.discard(job.job_id)

    async def recognize_frame(self, job: OCRJob) -> tuple[str, ...]:
        """读取一帧证据并返回模拟筛选后的有序行。"""
        await asyncio.sleep(self.configuration.simulated_ocr_delay_ms / 1000)
        image_content = await run_blocking_operation(
            Path(job.frame.image_path).read_bytes,
        )
        if not image_content:
            raise ValueError("证据图片为空。")
        return tuple(line.strip() for line in job.simulated_lines if line.strip())
