"""接收图片批次，调用整批识别接口并回传图片识别结果。"""

import asyncio
import logging
from dataclasses import dataclass

from configuration import MeasurementConfiguration
from models import CapturedFrame, MeasurementEvent, PublishEvent
from recovery import run_blocking_operation


logger = logging.getLogger(__name__)


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
            frames: 上游筛选并在内存编码后交付的图片元组。

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
            # 等待下一批图片，并按原顺序准备模型输入字节。
            batch = await self.batch_queue.get()
            try:
                images = [frame.image_data for frame in batch.frames]

                # 在线程中执行一次整批推理，失败时返回本批错误。
                try:
                    image_results = await run_blocking_operation(self.recognize_batch, images)
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
                # 清除监听协程持有的原图引用，再等待下一批。
                batch = None
                images = None
                frame = None

    def recognize_batch(self, images: list[bytes]) -> list[dict]:
        """为每张图片返回独立的空识别结果，临时用于联调。

        Args:
            images: 按本批帧顺序排列的内存 BMP 文件字节列表。

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
            for image_data in images
        ]

    def select_final_text_and_img(
        self,
        frame_results: list[dict],
        frames: dict[str, CapturedFrame],
    ) -> None:
        """预留本轮文字终选及所属图片选择、保存的统一入口。

        Args:
            frame_results: 本轮成功图片的原始结果，包含 frame_id 和 blocks。
            frames: 按 frame_id 索引的本轮内存图片，供最终文字关联原图。

        Returns:
            None  # 文字终选和图片选择、保存待实现，不改变测量完成状态
        """
        # 选出含文字块的图片结果，没有可用文字时记录日志并结束。
        text_results = [result for result in frame_results if result["blocks"]]
        if not text_results:
            logger.warning("本轮无可用识别文字，跳过最终文字筛选。")
            return

        # 待实现：跨帧去重、聚合和业务筛选，确定最终保留的文字。
        # 待实现：按保留文字的信息量和置信度选择所属图片，并保存选中图片。
        # 待实现：没有保留文字、整轮 OCR 失败或超时时不保存图片。
        pass

    def discard_session_batches(self, session_id: str) -> int:
        """移除指定周期尚未消费的图片批次并保留其他周期顺序。

        Args:
            session_id: 需要释放图片的测量周期编号。

        Returns:
            2  # 已移除的待消费批次数，不包含正在推理的批次
        """
        # 逐批检查当前队列，移除目标周期并保留其他周期的原有顺序。
        queued_count = self.batch_queue.qsize()
        discarded_count = 0
        for batch_number in range(queued_count):
            batch = self.batch_queue.get_nowait()
            if batch.session_id == session_id:
                discarded_count += 1
            else:
                self.batch_queue.put_nowait(batch)
            # 保留批次先重新入队，再结算本次取出操作。
            self.batch_queue.task_done()
        return discarded_count
