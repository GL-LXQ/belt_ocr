"""向异常事件页面提供查询结果和原因文案。"""

import sqlite3

from src.repo.abnormal_event_repo import AbnormalEventRepo


# 将现有异常写入路径使用的原因码转换为页面文案。
REASON_LABELS = {
    "OCR_TIMEOUT": "OCR 识别超时",
    "IO_INTERRUPTED": "IO 通信中断",
    "CAPTURE_FAILED": "相机采集失败",
    "CLOSE_SESSION_MISMATCH": "关闭信号与当前 Session 不匹配",
    "AMBIGUOUS_MEASUREMENT": "频率读数缺少 Session ID",
    "CYCLE_INTERRUPTED": "测量周期中断",
    "CAPTURE_EMPTY": "本轮未采集到图像",
    "UNKNOWN_EVENT_TYPE": "未知事件类型",
    "LATE_FREQUENCY": "迟到的频率读数",
    "EVIDENCE_ENCODING_FAILED": "证据图片编码失败",
    "EVIDENCE_WRITE_FAILED": "证据图片保存失败",
    "COMMIT_INTEGRITY_CONFLICT": "测量记录提交冲突",
    "DATABASE_WRITE_FAILED": "测量结果入库失败",
    "PROGRAM_FAILED": "程序运行失败",
    "SHUTDOWN_TIMEOUT": "程序退出超时",
    "UNKNOWN_MACHINE": "未知机器",
}


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
        """查询异常列表并准备原因文案与摘要。

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
                    "reason": "OCR_TIMEOUT",  # 原始原因码
                    "payload_json": "{}",  # 完整原始内容
                    "reason_label": "OCR 识别超时",  # 中文原因文案
                    "payload_summary": "{}",  # 列表摘要
                }]
        """
        # 读取符合筛选条件的异常事件。
        try:
            events = self.abnormal_event_repo.list_events(machine_id, session_id)
        except sqlite3.Error as error:
            raise AbnormalEventServiceError(f"异常事件读取失败：{error}") from error

        # 为列表记录补充中文原因和简短内容。
        for event in events:
            self.prepare_event(event)
        return events

    def get_event(self, abnormal_event_id: int) -> dict | None:
        """读取一条异常事件并准备详情文案。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            返回示例：
                {
                    "abnormal_event_id": 5,  # 异常事件主键
                    "created_at": 1790496570.9,  # 发生时间戳
                    "machine_id": "1",  # 机器编号
                    "session_id": "session-1",  # 周期编号
                    "reason": "OCR_TIMEOUT",  # 原始原因码
                    "payload_json": "{}",  # 完整原始内容
                    "reason_label": "OCR 识别超时",  # 中文原因文案
                    "payload_summary": "{}",  # 列表摘要
                }
                None  # 记录不存在
        """
        # 按主键读取记录并转换数据库读取错误。
        try:
            event = self.abnormal_event_repo.get_event(abnormal_event_id)
        except sqlite3.Error as error:
            raise AbnormalEventServiceError(f"异常详情读取失败：{error}") from error

        # 为已有记录准备中文原因和摘要。
        if event is not None:
            self.prepare_event(event)
        return event

    def prepare_event(self, event: dict) -> None:
        """给原始异常记录添加用户可见的原因和内容摘要。

        Args:
            event: Repo 返回的异常事件字段字典。

        Returns:
            返回示例：
                None  # event 已添加 reason_label 和 payload_summary
        """
        # 根据现有原因码选择中文文案，缺失映射时保留原值。
        reason = event["reason"]
        event["reason_label"] = REASON_LABELS.get(reason, reason)

        # 将原始 payload 压成单行并限制列表显示长度。
        summary = event["payload_json"].replace("\n", " ").strip()
        event["payload_summary"] = summary[:79] + "…" if len(summary) > 80 else summary
