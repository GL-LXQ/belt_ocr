"""读取测量记录并处理人工复核。"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from src.repo.measurement_record_repo import MeasurementRecordRepo


logger = logging.getLogger(__name__)


class MeasurementRecordServiceError(Exception):
    """表示测量记录读取、复核写入或文字解析失败。"""


class MeasurementReviewAlreadyCompletedError(MeasurementRecordServiceError):
    """表示这条记录已经完成复核或不属于待复核记录。"""


class MeasurementRecordService:
    """向历史记录页提供查询结果和人工复核操作。"""

    def __init__(self, measurement_record_repo: MeasurementRecordRepo) -> None:
        """保存测量结果表访问对象。

        Args:
            measurement_record_repo: 负责测量结果表结构和读取的 Repo。

        Returns:
            返回示例：
                None  # 测量记录服务已初始化
        """
        self.measurement_record_repo = measurement_record_repo

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
            return self.measurement_record_repo.list_record_machines()
        except sqlite3.Error as error:
            # 记录数据库读取故障。
            logger.exception("历史机器读取失败")

            # 抛出可直接展示的业务提示。
            raise MeasurementRecordServiceError("历史机器读取失败。") from error

    def list_records(
        self, review_status: str | None = None, machine_id: str | None = None
    ) -> list[dict]:
        """读取筛选结果并转换文字与复核字段。

        Args:
            review_status: None 表示全部，normal、pending、reviewed 表示查询状态。
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
                    "needs_review": False,  # 是否需要人工复核
                    "reviewed_at": None,  # 人工复核时间
                    "reviewed_lines": None,  # 人工修改后的文字
                }]
        """
        # 查询测量记录并转换存储格式。
        try:
            records = self.measurement_record_repo.list_records(
                review_status, machine_id
            )
            for record in records:
                self.decode_record_fields(record)
            return records
        except (sqlite3.Error, json.JSONDecodeError) as error:
            # 记录数据库或 JSON 读取故障。
            logger.exception("历史记录读取失败")

            # 抛出可直接展示的业务提示。
            raise MeasurementRecordServiceError("历史记录读取失败。") from error

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
                    "reviewed_at": None,  # 人工复核时间
                    "reviewed_lines": None,  # 人工修改后的文字
                }
                None  # 周期编号没有对应记录
        """
        # 查询详情并转换存储格式。
        try:
            record = self.measurement_record_repo.get_record(session_id)
            if record is not None:
                self.decode_record_fields(record)
            return record
        except (sqlite3.Error, json.JSONDecodeError) as error:
            # 记录数据库或 JSON 读取故障。
            logger.exception("历史详情读取失败")

            # 抛出可直接展示的业务提示。
            raise MeasurementRecordServiceError("历史详情读取失败。") from error

    def complete_review(self, session_id: str, edited_text: str | None = None) -> None:
        """确认原始文字或保存人工文字并完成一条记录的复核。

        Args:
            session_id: 待复核的测量周期编号。
            edited_text: 人工编辑的多行文字；None 表示确认原始文字。

        Returns:
            返回示例：
                None  # 已写入复核时间和可选人工结果
        """
        # 整理人工编辑内容，确认原结果时保持人工文字为空。
        reviewed_lines = None
        if edited_text is not None:
            lines = tuple(
                line.strip() for line in edited_text.splitlines() if line.strip()
            )
            if not lines:
                raise MeasurementRecordServiceError("人工复核结果至少需要一条有效文字。")
            reviewed_lines = json.dumps(lines, ensure_ascii=False)

        # 使用统一 UTC 时间提交复核并报告重复操作。
        reviewed_at = datetime.now(timezone.utc).isoformat()
        try:
            updated = self.measurement_record_repo.complete_review(
                session_id, reviewed_at, reviewed_lines
            )
        except sqlite3.Error as error:
            # 记录复核写入的数据库故障。
            logger.exception("人工复核保存失败")

            # 抛出可直接展示的业务提示。
            raise MeasurementRecordServiceError("人工复核保存失败。") from error
        if not updated:
            raise MeasurementReviewAlreadyCompletedError("该记录已完成复核。")

    def decode_record_fields(self, record: dict) -> None:
        """将一条记录中的 JSON 文字和复核标志转成页面字段。

        Args:
            record: Repo 读取的测量记录字典。

        Returns:
            返回示例：
                None  # 记录中的文字列表和 needs_review 已原地转换
        """
        record["ordered_lines"] = tuple(json.loads(record["ordered_lines"]))
        if record["reviewed_lines"] is not None:
            record["reviewed_lines"] = tuple(json.loads(record["reviewed_lines"]))
        record["needs_review"] = bool(record["needs_review"])
