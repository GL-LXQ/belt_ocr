"""封装 measurements 表的读写操作。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MeasurementRecord:
    """保存一轮测量需要写入数据库的业务字段。"""

    machine_id: str  # 机器编号
    session_id: str  # 测量周期编号
    start_time: str  # 本轮开始时间
    finish_time: str  # 本轮结算时间
    ordered_lines: tuple[str, ...]  # 最终文字
    final_frequency_hz: float | None  # 最终频率
    measurement_frequencies: tuple[dict, ...]  # 频率明细
    evidence_directory: Path  # 本轮证据图片目录
    needs_review: bool  # 是否需要人工复核
    review_reason: str | None  # 人工复核原因


class MeasurementRepo:
    """管理测量记录的幂等写入和提交确认查询。"""

    @staticmethod
    def create_table(connection: sqlite3.Connection) -> None:
        """创建测量结果表并保留已有数据。

        Args:
            connection: 初始化流程提供的数据库连接。

        Returns:
            返回示例：
                None  # 测量结果表已就绪
        """
        # 在初始化连接中创建测量结果表。
        connection.execute("""
            CREATE TABLE IF NOT EXISTS measurements (
                session_id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                finish_time TEXT NOT NULL,
                ordered_lines TEXT NOT NULL,
                final_frequency_hz REAL,
                measurement_frequencies TEXT NOT NULL DEFAULT '[]',
                evidence_directory TEXT NOT NULL,
                needs_review INTEGER NOT NULL DEFAULT 0,
                review_reason TEXT
            );
        """)

    def __init__(self, database_path: Path):
        """保存业务数据库路径。

        Args:
            database_path: 业务数据库文件路径。

        Returns:
            返回示例：
                None  # 初始化测量表访问对象
        """
        # 保存业务数据库路径。
        self.database_path = database_path

    def write_record(self, record: MeasurementRecord) -> None:
        """在事务中幂等写入冻结记录及频率明细和最终值。

        Args:
            record: 本轮测量的业务字段和证据图片目录。

        Returns:
            返回示例：
                None  # 记录已写入或已存在相同内容，冲突时抛出异常
        """
        # 将列表字段编码为对应列的 JSON，整理本轮业务内容。
        record_values = (
            record.machine_id,
            record.start_time,
            record.finish_time,
            json.dumps(record.ordered_lines, ensure_ascii=False),
            record.final_frequency_hz,
            str(record.evidence_directory),
            json.dumps(
                record.measurement_frequencies,
                ensure_ascii=False,
                sort_keys=True,
            ),
            int(record.needs_review),
            record.review_reason,
        )

        # 打开业务库连接。
        connection = sqlite3.connect(self.database_path, timeout=1)
        with closing(connection), connection:
            # 开启本轮写入事务。
            connection.execute("BEGIN IMMEDIATE")

            # 读取同一周期已保存的业务字段。
            existing_record = connection.execute(
                "SELECT machine_id, start_time, finish_time, ordered_lines, "
                "final_frequency_hz, evidence_directory, measurement_frequencies, "
                "needs_review, review_reason "
                "FROM measurements WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()

            # 已有记录时比较内容，不一致则拒绝，一致则跳过写入。
            if existing_record is not None:
                if existing_record != record_values:
                    raise ValueError("同一 Session 的提交内容不一致。")
                return

            # 将本轮业务字段写入测量表。
            connection.execute(
                "INSERT INTO measurements (session_id, machine_id, start_time, "
                "finish_time, ordered_lines, final_frequency_hz, evidence_directory, "
                "measurement_frequencies, needs_review, review_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.session_id, *record_values),
            )

    def exists_by_session_id(self, session_id: str) -> bool:
        """查询指定周期是否已提交测量记录。

        Args:
            session_id: 待查询的测量周期编号。

        Returns:
            返回示例：
                True  # 已存在对应周期的测量记录
                False  # 未找到对应周期的测量记录
        """
        # 按周期编号查询记录是否存在。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            record = connection.execute("SELECT 1 FROM measurements WHERE session_id = ?", (session_id,)).fetchone()

        # 按查询结果返回是否存在。
        return record is not None
