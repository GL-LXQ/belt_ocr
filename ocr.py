"""提供有界的共享 OCR 调度和模拟识别结果。"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from configuration import MeasurementConfiguration
from models import CapturedFrame, MeasurementEvent, OCRResult, PublishEvent


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OCRJob:
    machine_id: str
    session_id: str
    frames: tuple[CapturedFrame, ...]
    simulated_lines: tuple[str, ...]


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

    def submit(self, job: OCRJob) -> bool:
        """把本轮全部选中帧加入共享队列。"""
        if self.pending_count >= self.configuration.ocr_queue_capacity:
            return False
        self.machine_queues[job.machine_id].append(job)
        self.pending_count += 1
        self.jobs_available.set()
        return True

    async def run(self) -> None:
        """按机器轮转处理任务并返回模拟筛选后的文字行。"""
        while True:
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

            try:
                # 模拟识别耗时并读取全部选中帧的证据。
                await asyncio.sleep(self.configuration.simulated_ocr_delay_ms / 1000)
                for frame in job.frames:
                    image_content = await asyncio.to_thread(
                        Path(frame.image_path).read_bytes,
                    )
                    if not image_content:
                        raise ValueError("证据图片为空。")

                # 保留配置中的文字顺序及不同位置的相同文字。
                ordered_lines = tuple(
                    line.strip() for line in job.simulated_lines if line.strip()
                )
                if not job.frames or not ordered_lines:
                    await self.publish_event(MeasurementEvent(
                        "OCRFailed", job.machine_id, job.session_id,
                        "OCR_NO_VALID_TEXT",
                    ))
                    continue
                result = OCRResult(
                    ordered_lines,
                    tuple(frame.image_path for frame in job.frames),
                    tuple(frame.frame_id for frame in job.frames),
                )
                await self.publish_event(MeasurementEvent(
                    "OCRCompleted", job.machine_id, job.session_id, result,
                ))
            except Exception:
                logger.exception(
                    "模拟 OCR 失败 machine_id=%s session_id=%s",
                    job.machine_id, job.session_id,
                )
                await self.publish_event(MeasurementEvent(
                    "OCRFailed", job.machine_id, job.session_id,
                    "OCR_PROCESSING_FAILED",
                ))
            finally:
                self.pending_count -= 1
