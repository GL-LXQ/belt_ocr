"""向异常事件页面提供查询结果和内容摘要。"""

import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from src.repo.abnormal_event_repo import AbnormalEventRepo


logger = logging.getLogger(__name__)


ERROR_DESCRIPTIONS = {
    "CAPTURE_EMPTY": "本轮未采集到图像",
    "CAPTURE_FAILED": "相机采集失败",
    "OCR_FAILED": "OCR 识别执行失败",
    "OCR_LOCK_WAIT_TIMEOUT": "OCR 识别资源等待超时",
    "OCR_TIMEOUT": "OCR 识别超时",
    "CYCLE_TIMEOUT": "测量周期超时",
    "FREQUENCY_NO_VALID_MEASUREMENT": "没有有效频率读数",
    "EVIDENCE_ENCODING_FAILED": "证据图片编码失败",
}

EVENT_DESCRIPTIONS = {
    "MachineStarted": "机器启动",
    "MachineClosed": "机器关闭",
    "Shutdown": "应用退出",
    "IOInterrupted": "IO 通信中断",
    "CaptureCompleted": "图像采集完成",
    "CaptureFailed": "相机采集失败",
    "OCRCompleted": "OCR 识别完成",
    "OCRFailed": "OCR 识别执行失败",
    "OCRLockWaitTimeout": "OCR 识别资源等待超时",
    "OCRTimeout": "OCR 识别超时",
    "FrequencyMeasured": "收到频率读数",
    "CycleTimeout": "测量周期超时",
}


def format_event_summary(payload_json: str) -> str:
    """从已保存的错误、消息和业务事件字段生成简短中文摘要。

    Args:
        payload_json: 不改写的原始事件 JSON。

    Returns:
        返回示例：
            "OCR 识别超时"  # 已知历史错误码转换为中文
            "事件：收到频率读数；频率：50.0 Hz；频率仪：FM01"  # 已保存的事件和读数
            "其他事件内容，请查看原始数据"  # 未识别的内容保留在详情中
    """
    # 解析已保存的 JSON，格式异常时保留原始内容供详情查看。
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return "原始内容格式异常，请查看原始数据"

    # 读取周期错误和独立消息，只使用已保存的文字。
    parts = []
    if isinstance(payload, dict):
        errors = payload.get("session_errors")
        if isinstance(errors, list):
            parts.extend(ERROR_DESCRIPTIONS.get(error, error) for error in errors if isinstance(error, str) and error)
        message = payload.get("message")
        if isinstance(message, str) and message:
            parts.append(message)

        # 标注业务事件类型并保留未知类型的原始名称。
        event_type = payload.get("event_type")
        if isinstance(event_type, str) and event_type:
            parts.append(f"事件：{EVENT_DESCRIPTIONS.get(event_type, event_type)}")
        event_payload = payload.get("payload")
        if isinstance(event_payload, str) and event_payload:
            parts.append(ERROR_DESCRIPTIONS.get(event_payload, event_payload))

        # 从业务事件内容中提取消息和已保存的频率读数。
        if isinstance(event_payload, dict):
            message = event_payload.get("message")
            if isinstance(message, str) and message:
                parts.append(message)
            if event_type == "FrequencyMeasured":
                frequency = event_payload.get("value_hz")
                if isinstance(frequency, (int, float)) and not isinstance(frequency, bool):
                    parts.append(f"频率：{frequency} Hz")
                serial = event_payload.get("frequency_meter_serial")
                if isinstance(serial, str) and serial:
                    parts.append(f"频率仪：{serial}")
    elif isinstance(payload, str) and payload:
        parts.append(payload)

    # 将摘要压缩为单行，空内容和未知结构使用可读提示。
    summary = " ".join("；".join(parts).split())
    if not summary:
        summary = "无附加信息" if payload == {} else "其他事件内容，请查看原始数据"
    return summary[:79] + "…" if len(summary) > 80 else summary


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

    def list_machine_ids(self) -> dict[str, list[str]]:
        """读取异常事件关联的机器编号。

        Args:
            无外部参数。

        Returns:
            返回示例：
                {
                    "machine_ids": ["1", "2"],  # 有异常事件的机器编号
                }
        """
        # 读取机器选项并转换数据库读取错误。
        try:
            return {
                "machine_ids": self.abnormal_event_repo.list_machine_ids(),
            }
        except sqlite3.Error as error:
            # 记录数据库读取故障。
            logger.exception("异常机器读取失败")

            # 抛出可直接展示的业务提示。
            raise AbnormalEventServiceError("异常机器读取失败。") from error

    def list_events(
        self,
        machine_id: str | None = None,
        session_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, list[dict]]:
        """按机器、周期和本地日期查询异常列表并准备可读摘要。

        Args:
            machine_id: 指定机器编号，None 表示全部机器。
            session_id: 完整 Session ID，None 表示全部周期。
            start_date: 可选的本地记录开始日期。
            end_date: 可选的本地记录结束日期，包含整天。

        Returns:
            返回示例：
                {
                    "events": [{  # 异常事件列表
                        "abnormal_event_id": 5,  # 异常事件主键
                        "created_at": 1790496570.9,  # 记录写入时间戳
                        "machine_id": "1",  # 机器编号
                        "session_id": "session-1",  # 周期编号
                        "reason": "OCR 识别超时",  # 异常原因描述
                        "payload_json": "{}",  # 完整原始内容
                        "payload_summary": "无附加信息",  # 可读列表摘要
                    }],
                }
        """
        # 将本地开始日零点转换为 UTC 时间戳下界。
        start_created_at = None
        if start_date is not None:
            start_created_at = datetime.combine(start_date, time.min).astimezone(timezone.utc).timestamp()

        # 将本地结束日的次日零点转换为 UTC 时间戳上界。
        end_created_at = None
        if end_date is not None:
            end_created_at = datetime.combine(
                end_date + timedelta(days=1), time.min
            ).astimezone(timezone.utc).timestamp()

        # 读取符合机器、周期和记录时间条件的异常事件。
        try:
            events = self.abnormal_event_repo.list_events(
                machine_id,
                session_id,
                start_created_at=start_created_at,
                end_created_at=end_created_at,
            )
        except sqlite3.Error as error:
            # 记录数据库读取故障。
            logger.exception("异常事件读取失败")

            # 抛出可直接展示的业务提示。
            raise AbnormalEventServiceError("异常事件读取失败。") from error

        # 为列表补充可读摘要，保留原始原因和完整 JSON。
        for event in events:
            event["payload_summary"] = format_event_summary(event["payload_json"])
        return {
            "events": events,
        }

    def get_event(self, abnormal_event_id: int) -> dict[str, dict | None]:
        """按主键读取一条完整异常事件。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            返回示例：
                {
                    "event": {  # 异常事件详情；不存在时为 None
                        "abnormal_event_id": 5,  # 异常事件主键
                        "created_at": 1790496570.9,  # 记录写入时间戳
                        "machine_id": "1",  # 机器编号
                        "session_id": "session-1",  # 周期编号
                        "reason": "OCR 识别超时",  # 异常原因描述
                        "payload_json": "{}",  # 完整原始内容
                    },
                }
                {
                    "event": None,  # 主键没有对应事件
                }
        """
        # 按主键读取记录并转换数据库读取错误。
        try:
            event = self.abnormal_event_repo.get_event(abnormal_event_id)
        except sqlite3.Error as error:
            # 记录数据库读取故障。
            logger.exception("异常详情读取失败")

            # 抛出可直接展示的业务提示。
            raise AbnormalEventServiceError("异常详情读取失败。") from error

        return {"event": event}
