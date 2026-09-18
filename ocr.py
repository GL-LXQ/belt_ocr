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
class OCRBatch:
    """保存送入 OCR 模块的机器、周期和图片批次。"""

    machine_id: str
    session_id: str
    frames: tuple[CapturedFrame, ...]


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

        # 创建待处理批次队列，容量按批次数计算。
        self.batch_queue: asyncio.Queue[OCRBatch] = asyncio.Queue(maxsize=configuration.ocr_queue_capacity)

    def submit_batch(self, machine_id: str, session_id: str, frames: tuple[CapturedFrame, ...]) -> bool:
        """将图片批次放入 OCR 队列，暂不执行识别。

        Args:
            machine_id: 本批图片所属机器编号。
            session_id: 本批图片所属测量周期编号。
            frames: 上游筛选并保存后交付的图片元组。

        Returns:
            bool: 批次是否成功入队。
            返回示例：
                True  # 本批图片已入队
                False  # 已停止接收或批次队列已满
        """
        # 停止接收时返回提交失败。
        if not self.accepting_jobs:
            return False

        # 将完整批次非阻塞入队，队列满时返回提交失败。
        batch = OCRBatch(machine_id, session_id, frames)
        try:
            self.batch_queue.put_nowait(batch)
        except asyncio.QueueFull:
            return False

        # 返回批次受理结果。
        return True

    def submit(self, job: OCRJob) -> bool:
        """将单张图片的识别任务加入对应机器的 OCR 队列。

        Args:
            job: 包含机器编号、测量编号、任务编号、图片和模拟识别参数的任务。

        Returns:
            bool: 返回任务是否已受理，不表示识别是否成功。
            返回示例：
                True  # 任务已入队或已登记
                False  # 已停止接收任务或未完成任务数达到上限
        """
        # 已登记的任务直接返回受理成功。
        if job.job_id in self.registered_jobs:
            return True

        # 停止接收任务或容量已满时拒收。
        if not self.accepting_jobs or self.pending_count >= self.configuration.ocr_queue_capacity:
            return False

        # 将任务追加到所属机器的 OCR 队列。
        self.machine_queues[job.machine_id].append(job)

        # 更新未完成任务数量并登记任务编号。
        self.pending_count += 1
        self.registered_jobs.add(job.job_id)

        # 通知后台有任务可处理，并返回受理成功。
        self.jobs_available.set()
        return True

    async def run(self) -> None:
        """按机器轮转执行 OCR 任务，并通过事件报告识别结果。

        Args:
            无外部参数。

        Returns:
            None: 正常停止时不返回数据，识别结果通过事件发送。
            返回示例：
                None  # 无返回数据
        """
        while not self.stopping:
            # 等待队列中有可处理的任务。
            await self.jobs_available.wait()

            # 按机器轮转，从首个非空队列取出最早入队的图片任务。
            job = None
            for machine_number in range(len(self.machine_order)):
                machine_id = self.machine_order[0]
                self.machine_order.rotate(-1)
                if self.machine_queues[machine_id]:
                    job = self.machine_queues[machine_id].popleft()
                    break
            # 所有队列均为空时清除通知，返回等待。
            if job is None:
                self.jobs_available.clear()
                continue

            # 初始化重新入队标记，并读取任务已完成的尝试次数。
            requeued = False
            attempt = job.completed_attempts
            try:
                # 尝试次数已达上限时发送失败事件，跳过当前任务。
                if attempt >= self.configuration.ocr_retry_attempts:
                    await self.publish_event(
                        MeasurementEvent(
                            "OCRFrameFailed",
                            job.machine_id,
                            job.session_id,
                            {
                                "job_id": job.job_id,
                                "frame_id": job.frame.frame_id,
                                "attempt": attempt,
                                "error_code": "OCR_RETRIES_EXHAUSTED",
                            },
                        )
                    )
                    continue

                # 从下一次尝试开始执行，最多尝试到配置上限。
                for attempt in range(job.completed_attempts + 1, self.configuration.ocr_retry_attempts + 1):
                    # 准备本次尝试的任务编号、帧编号和次数。
                    payload = {
                        "job_id": job.job_id,
                        "frame_id": job.frame.frame_id,
                        "attempt": attempt,
                    }

                    # 发送开始识别事件，等待机器管理员处理回执。
                    acknowledgement = asyncio.get_running_loop().create_future()
                    await self.publish_event(
                        MeasurementEvent(
                            "OCRFrameStarted",
                            job.machine_id,
                            job.session_id,
                            payload,
                            acknowledgement=acknowledgement,
                        )
                    )
                    await acknowledgement
                    try:
                        # 执行单帧识别，并限制本次识别的等待时长。
                        lines = await asyncio.wait_for(
                            self.recognize_frame(job),
                            self.configuration.ocr_job_timeout_ms / 1000,
                        )
                        # 按文字是否为空组装单帧失败或完成事件的数据。
                        if not lines:
                            payload = {
                                **payload,
                                "error_code": "OCR_NO_VALID_TEXT",
                            }
                            event_type = "OCRFrameFailed"
                        else:
                            payload = {
                                **payload,
                                "ordered_lines": lines,
                            }
                            event_type = "OCRFrameCompleted"

                        # 将结果发回原测量档案，结束当前任务的尝试循环。
                        await self.publish_event(MeasurementEvent(event_type, job.machine_id, job.session_id, payload))
                        break
                    except Exception:
                        # 记录本次识别异常。
                        logger.exception(
                            "模拟 OCR 失败 machine_id=%s session_id=%s job_id=%s",
                            job.machine_id,
                            job.session_id,
                            job.job_id,
                        )

                        # 最后一次尝试失败时发布失败事件，其余情况继续重试。
                        if attempt == self.configuration.ocr_retry_attempts:
                            payload = {
                                **payload,
                                "error_code": "OCR_PROCESSING_FAILED",
                            }
                            await self.publish_event(
                                MeasurementEvent("OCRFrameFailed", job.machine_id, job.session_id, payload)
                            )
            except (asyncio.CancelledError, Exception):
                # 意外退出时把当前帧交给下一次工作任务继续尝试。
                if not self.stopping:
                    self.machine_queues[job.machine_id].appendleft(replace(job, completed_attempts=attempt))
                    self.pending_count += 1
                    requeued = True
                raise
            finally:
                # 扣除本次处理的任务计数，移除未重新入队的任务登记。
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
