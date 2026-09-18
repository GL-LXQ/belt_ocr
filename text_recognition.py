"""接收本系统采集的图片批次，供后续文字识别使用。"""

import asyncio
from dataclasses import dataclass

from configuration import MeasurementConfiguration
from models import CapturedFrame


@dataclass(frozen=True)
class RecognitionBatch:
    """保存待识别批次的机器编号、测量周期编号和图片元组。"""

    machine_id: str
    session_id: str
    frames: tuple[CapturedFrame, ...]


class TextRecognizer:
    """接收图片批次并保存到有界队列。"""

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
