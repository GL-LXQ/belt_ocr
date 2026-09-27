"""读取测量历史并转换页面使用的记录字段。"""

import json
import sqlite3

from src.repo.measurement_repo import MeasurementRepo


class HistoryServiceError(Exception):
    """表示测量历史读取或文字解析失败。"""


class HistoryService:
    """向历史记录页提供机器选项、筛选结果和单条详情。"""

    def __init__(self, measurement_repo: MeasurementRepo) -> None:
        """保存测量结果表访问对象。

        Args:
            measurement_repo: 负责测量结果表结构和读取的 Repo。

        Returns:
            返回示例：
                None  # 历史记录服务已初始化
        """
        self.measurement_repo = measurement_repo

    def list_record_machines(self) -> list[dict]:
        """读取历史记录中出现过的机器。

        Args:
            无外部参数。

        Returns:
            返回示例：
                [{
                    "machine_id": "1",  # 机器编号
                    "machine_name": "皮带机 1",  # 机器名称或编号
                }]
        """
        # 读取机器选项并将数据库错误转换为页面错误。
        try:
            return self.measurement_repo.list_record_machines()
        except sqlite3.Error as error:
            raise HistoryServiceError(f"历史机器读取失败：{error}") from error

    def list_records(
        self, needs_review: bool | None = None, machine_id: str | None = None
    ) -> list[dict]:
        """读取筛选结果并转换文字与复核字段。

        Args:
            needs_review: None 表示全部，布尔值表示对应复核状态。
            machine_id: None 表示全部机器，否则筛选指定机器。

        Returns:
            返回示例：
                [{
                    "session_id": "session-1",  # 测量周期编号
                    "machine_id": "1",  # 机器编号
                    "machine_name": "皮带机 1",  # 机器名称或编号
                    "finish_time": "2026-09-27T08:00:00+00:00",  # 结束时间
                    "ordered_lines": ("ABC",),  # 完整 OCR 文字
                    "final_frequency_hz": 50.0,  # 最终频率
                    "needs_review": False,  # 是否待复核
                }]
        """
        # 查询测量记录并转换存储格式。
        try:
            records = self.measurement_repo.list_records(needs_review, machine_id)
            for record in records:
                self.decode_record_fields(record)
            return records
        except (sqlite3.Error, json.JSONDecodeError) as error:
            raise HistoryServiceError(f"历史记录读取失败：{error}") from error

    def get_record(self, session_id: str) -> dict | None:
        """读取一条测量记录并转换详情字段。

        Args:
            session_id: 待查看的测量周期编号。

        Returns:
            返回示例：
                {
                    "session_id": "session-1",  # 测量周期编号
                    "machine_id": "1",  # 机器编号
                    "machine_name": "皮带机 1",  # 机器名称或编号
                    "start_time": "2026-09-27T07:59:00+00:00",  # 开始时间
                    "finish_time": "2026-09-27T08:00:00+00:00",  # 结束时间
                    "ordered_lines": ("ABC",),  # 完整 OCR 文字
                    "final_frequency_hz": 50.0,  # 最终频率
                    "evidence_directory": "runtime/evidence/1",  # 证据目录
                    "needs_review": False,  # 是否待复核
                    "review_reason": None,  # 复核原因
                }
                None  # 周期编号没有对应记录
        """
        # 查询详情并转换存储格式。
        try:
            record = self.measurement_repo.get_record(session_id)
            if record is not None:
                self.decode_record_fields(record)
            return record
        except (sqlite3.Error, json.JSONDecodeError) as error:
            raise HistoryServiceError(f"历史详情读取失败：{error}") from error

    def decode_record_fields(self, record: dict) -> None:
        """将一条记录中的 JSON 文字和复核标志转成页面字段。

        Args:
            record: Repo 读取的测量记录字典。

        Returns:
            返回示例：
                None  # 记录中的 ordered_lines 和 needs_review 已原地转换
        """
        record["ordered_lines"] = tuple(json.loads(record["ordered_lines"]))
        record["needs_review"] = bool(record["needs_review"])
