"""接收图片批次，调用整批识别接口并回传图片识别结果。"""

import asyncio
from dataclasses import dataclass

from configuration import MeasurementConfiguration
from models import CapturedFrame, MeasurementEvent, PublishEvent
from recovery import run_blocking_operation


@dataclass(frozen=True)
class RecognitionBatch:
    """保存待识别批次的机器编号、测量周期编号和图片元组。"""

    machine_id: str
    session_id: str
    frames: tuple[CapturedFrame, ...]


class TextRecognizer:
    """管理图片批次队列和文字识别结果交付。"""

    def __init__(self, configuration: MeasurementConfiguration) -> None:
        """初始化待识别批次队列和接收状态。

        Args:
            configuration: 测量配置，ocr_queue_capacity 指定队列批次数上限。

        Returns:
            None  # 批次队列和接收状态已初始化
        """
        # 创建有界队列，按完整图片批次保存待识别数据。
        self.batch_queue: asyncio.Queue[RecognitionBatch] = asyncio.Queue(maxsize=configuration.ocr_queue_capacity)

        # 开放图片批次接收。
        self.accepting_batches = True

    def submit_batch(self, machine_id: str, session_id: str, frames: tuple[CapturedFrame, ...]) -> bool:
        """将图片批次放入 OCR 队列并返回受理结果。

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
        if not self.accepting_batches:
            return False

        # 将完整批次非阻塞入队，队列满时返回提交失败。
        batch = RecognitionBatch(machine_id, session_id, frames)
        try:
            self.batch_queue.put_nowait(batch)
        except asyncio.QueueFull:
            return False

        # 返回批次受理结果。
        return True

    async def listen_and_recognize_batches(self, publish_event: PublishEvent) -> None:
        """持续监听批次队列，执行整批识别并发送所属周期的结果。

        Args:
            publish_event: 接收批次完成或失败事件的异步业务入口。

        Returns:
            None  # 结果通过事件交付，监听任务由调用方取消
        """
        while True:
            # 等待下一批图片，并按原顺序准备模型输入路径。
            batch = await self.batch_queue.get()
            try:
                image_paths = [frame.image_path for frame in batch.frames]

                # 在线程中执行一次整批推理，失败时返回本批错误。
                try:
                    image_results = await run_blocking_operation(self.recognize_batch, image_paths)
                except Exception as error:
                    await publish_event(MeasurementEvent(
                        event_type="RecognitionBatchFailed",
                        machine_id=batch.machine_id,
                        session_id=batch.session_id,
                        payload=str(error),
                    ))
                    continue

                # 按输入顺序关联图片身份，保留模型原始文字块。
                frame_results = []
                for frame, image_result in zip(batch.frames, image_results):
                    frame_results.append({
                        "frame_id": frame.frame_id,
                        "image_path": frame.image_path,
                        "blocks": image_result["blocks"],
                    })

                # 将整批结果交付给原机器和测量周期。
                await publish_event(MeasurementEvent(
                    event_type="RecognitionBatchCompleted",
                    machine_id=batch.machine_id,
                    session_id=batch.session_id,
                    payload=frame_results,
                ))
            finally:
                # 结束本次队列消费记账。
                self.batch_queue.task_done()

    def recognize_batch(self, image_paths: list[str]) -> list[dict]:
        """为每张图片返回独立的空识别结果，临时用于联调。

        Args:
            image_paths: 按本批帧顺序排列的图片路径列表。

        Returns:
            list[dict]: 与输入等长的联调占位结果，示例如下。
            [
                {
                    "blocks": [],  # 当前图片的占位文字块，不代表真实识别结果
                },
            ]
        """
        # 按输入图片数量生成独立的空识别结果。
        return [
            {
                "blocks": [],
            }
            for image_path in image_paths
        ]
