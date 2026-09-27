"""向异常事件页面提供查询结果和内容摘要。"""

import sqlite3

from src.repo.abnormal_event_repo import AbnormalEventRepo


class AbnormalEventServiceError(Exception):
    """表示异常事件读取失败。"""


class AbnormalEventService:
    """向异常事件页面提供只读记录。"""

    def __init__(self, abnormal_event_repo: AbnormalEventRepo) -> None:
        """保存异常事件表访问对象。

        Args:
            abnormal_event_repo: 读取异常事件表的 Repo。

        Returns:
            返回示例：
                None  # 异常事件服务已初始化
        """
        self.abnormal_event_repo = abnormal_event_repo

    def list_machine_ids(self) -> list[str]:
        """读取异常事件关联的机器编号。

        Args:
            无外部参数。

        Returns:
            返回示例：
                ["1", "2"]  # 有异常事件的机器编号
        """
        # 读取机器选项并转换数据库读取错误。
        try:
            return self.abnormal_event_repo.list_machine_ids()
        except sqlite3.Error as error:
            raise AbnormalEventServiceError(f"异常机器读取失败：{error}") from error

    def list_events(
        self, machine_id: str | None = None, session_id: str | None = None
    ) -> list[dict]:
        """查询异常列表并准备原始内容摘要。

        Args:
            machine_id: 指定机器编号，None 表示全部机器。
            session_id: 完整 Session ID，None 表示全部周期。

        Returns:
            返回示例：
                [{
                    "abnormal_event_id": 5,  # 异常事件主键
                    "created_at": 1790496570.9,  # 发生时间戳
                    "machine_id": "1",  # 机器编号
                    "session_id": "session-1",  # 周期编号
                    "reason": "OCR 识别超时",  # 异常原因描述
                    "payload_json": "{}",  # 完整原始内容
                    "payload_summary": "{}",  # 列表摘要
                }]
        """
        # 读取符合筛选条件的异常事件。
        try:
            events = self.abnormal_event_repo.list_events(machine_id, session_id)
        except sqlite3.Error as error:
            raise AbnormalEventServiceError(f"异常事件读取失败：{error}") from error

        # 为列表记录补充简短的原始内容摘要。
        for event in events:
            summary = event["payload_json"].replace("\n", " ").strip()
            event["payload_summary"] = (
                summary[:79] + "…" if len(summary) > 80 else summary
            )
        return events

    def get_event(self, abnormal_event_id: int) -> dict | None:
        """按主键读取一条完整异常事件。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            返回示例：
                {
                    "abnormal_event_id": 5,  # 异常事件主键
                    "created_at": 1790496570.9,  # 发生时间戳
                    "machine_id": "1",  # 机器编号
                    "session_id": "session-1",  # 周期编号
                    "reason": "OCR 识别超时",  # 异常原因描述
                    "payload_json": "{}",  # 完整原始内容
                }
                None  # 记录不存在
        """
        # 按主键读取记录并转换数据库读取错误。
        try:
            event = self.abnormal_event_repo.get_event(abnormal_event_id)
        except sqlite3.Error as error:
            raise AbnormalEventServiceError(f"异常详情读取失败：{error}") from error

        return event
