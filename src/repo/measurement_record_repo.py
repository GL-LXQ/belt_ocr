"""查询和复核测量历史，并创建测量结果表。"""

import sqlite3
from contextlib import closing
from pathlib import Path


class MeasurementRecordRepo:
    """管理测量结果表结构、历史记录读取和人工复核写入。"""

    def __init__(self, database_path: Path) -> None:
        """保存测量结果所在的业务数据库路径。

        Args:
            database_path: 业务数据库文件路径。

        Returns:
            返回示例：
                None  # 测量结果表访问对象已初始化
        """
        self.database_path = database_path

    @staticmethod
    def create_table(connection: sqlite3.Connection) -> None:
        """在现有连接中创建测量结果表并补齐复核字段。

        Args:
            connection: 业务数据库初始化连接。

        Returns:
            返回示例：
                None  # 测量结果表及人工复核字段已就绪
        """
        # 创建测量结果表。
        connection.execute("""
            CREATE TABLE IF NOT EXISTS measurement_records (
                session_id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                finish_time TEXT NOT NULL,
                ordered_lines TEXT NOT NULL,
                final_frequency_hz REAL,
                measurement_frequencies TEXT NOT NULL DEFAULT '[]',
                evidence_directory TEXT NOT NULL,
                needs_review INTEGER NOT NULL DEFAULT 0,
                review_reason TEXT,
                reviewed_at TEXT,
                reviewed_lines TEXT
            );
        """)

        # 为已有开发库补齐人工复核字段。
        columns = connection.execute("PRAGMA table_info(measurement_records)")
        existing_columns = {column[1] for column in columns}
        if "reviewed_at" not in existing_columns:
            connection.execute(
                "ALTER TABLE measurement_records ADD COLUMN reviewed_at TEXT"
            )
        if "reviewed_lines" not in existing_columns:
            connection.execute(
                "ALTER TABLE measurement_records ADD COLUMN reviewed_lines TEXT"
            )

    def list_records(
        self,
        review_status: str | None = None,
        machine_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        start_finish_time: str | None = None,
        end_finish_time: str | None = None,
    ) -> list[dict]:
        """按复核状态、机器和结束时间分页读取测量历史。

        Args:
            review_status: None 表示全部，normal、pending、reviewed 表示查询状态。
            machine_id: None 表示全部机器，否则筛选指定机器。
            limit: 本页最多读取的记录数。
            offset: 跳过的记录数。
            start_finish_time: 可选的 UTC 结束时间下界。
            end_finish_time: 可选的 UTC 结束时间排他上界。

        Returns:
            返回示例：
                [{
                    "session_id": "session-1",  # 测量周期编号
                    "machine_id": "1",  # 机器编号
                    "machine_name": "皮带机 1",  # 机器名称或编号
                    "finish_time": "2026-09-27T08:00:00+00:00",  # 结束时间
                    "ordered_lines": '["ABC"]',  # OCR 文字 JSON
                    "final_frequency_hz": 50.0,  # 最终频率
                    "needs_review": 0,  # 是否需要人工复核
                    "reviewed_at": None,  # 人工复核时间
                    "reviewed_lines": None,  # 人工修改文字 JSON
                }]
        """
        # 按已选择的筛选条件生成参数化查询。
        conditions = []
        parameters = []
        if review_status == "normal":
            conditions.append("record.needs_review = 0")
        elif review_status == "pending":
            conditions.append("record.needs_review = 1 AND record.reviewed_at IS NULL")
        elif review_status == "reviewed":
            conditions.append(
                "record.needs_review = 1 AND record.reviewed_at IS NOT NULL"
            )
        if machine_id is not None:
            conditions.append("record.machine_id = ?")
            parameters.append(machine_id)
        if start_finish_time is not None:
            conditions.append("record.finish_time >= ?")
            parameters.append(start_finish_time)
        if end_finish_time is not None:
            conditions.append("record.finish_time < ?")
            parameters.append(end_finish_time)
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        # 读取测量结果和对应机器名称。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT record.session_id, record.machine_id, "
                "COALESCE(machine.machine_name, record.machine_id) AS machine_name, "
                "record.finish_time, record.ordered_lines, "
                "record.final_frequency_hz, record.needs_review, "
                "record.reviewed_at, record.reviewed_lines "
                "FROM measurement_records AS record "
                "LEFT JOIN machine ON CAST(machine.id AS TEXT) = record.machine_id"
                + where_clause
                + " ORDER BY record.finish_time DESC, record.session_id DESC"
                + " LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_records(
        self,
        review_status: str | None = None,
        machine_id: str | None = None,
        start_finish_time: str | None = None,
        end_finish_time: str | None = None,
    ) -> int:
        """统计当前复核状态、机器和结束时间条件下的记录。

        Args:
            review_status: None 表示全部，normal、pending、reviewed 表示查询状态。
            machine_id: None 表示全部机器，否则筛选指定机器。
            start_finish_time: 可选的 UTC 结束时间下界。
            end_finish_time: 可选的 UTC 结束时间排他上界。

        Returns:
            返回示例：
                25  # 当前筛选条件下的测量记录数
        """
        # 按当前复核状态和机器编号整理筛选条件。
        conditions = []
        parameters = []
        if review_status == "normal":
            conditions.append("record.needs_review = 0")
        elif review_status == "pending":
            conditions.append("record.needs_review = 1 AND record.reviewed_at IS NULL")
        elif review_status == "reviewed":
            conditions.append(
                "record.needs_review = 1 AND record.reviewed_at IS NOT NULL"
            )
        if machine_id is not None:
            conditions.append("record.machine_id = ?")
            parameters.append(machine_id)
        if start_finish_time is not None:
            conditions.append("record.finish_time >= ?")
            parameters.append(start_finish_time)
        if end_finish_time is not None:
            conditions.append("record.finish_time < ?")
            parameters.append(end_finish_time)
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        # 查询符合筛选条件的记录总数。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM measurement_records AS record" + where_clause,
                parameters,
            ).fetchone()
        return row[0]

    def count_daily_summary(
        self,
        start_finish_time: str,
        end_finish_time: str,
    ) -> dict[str, int]:
        """在一次查询中统计结束时间范围内的全部记录和待复核记录。

        Args:
            start_finish_time: UTC 结束时间下界，包含该时刻。
            end_finish_time: UTC 结束时间上界，不包含该时刻。

        Returns:
            返回示例：
                {
                    "recognition_count": 3,  # 时间范围内全部已入库记录数
                    "pending_review_count": 1,  # 时间范围内尚未复核的记录数
                }
        """
        # 使用同一连接和一条 SQL 读取两项检测统计。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS recognition_count,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN needs_review = 1 AND reviewed_at IS NULL
                                THEN 1
                                ELSE 0
                            END
                        ),
                        0
                    ) AS pending_review_count
                FROM measurement_records
                WHERE finish_time >= ? AND finish_time < ?
                """,
                (start_finish_time, end_finish_time),
            ).fetchone()

        # 整理同次查询返回的两项统计值。
        return {
            "recognition_count": int(row[0]),
            "pending_review_count": int(row[1]),
        }

    def list_record_machines(self) -> list[dict]:
        """读取所有出现过测量记录的机器。

        Args:
            无外部参数。

        Returns:
            返回示例：
                [{
                    "machine_id": "1",  # 机器编号
                    "machine_name": "皮带机 1",  # 机器名称或编号
                }]
        """
        # 从全部历史记录中读取机器，包括停用和软删除机器。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT DISTINCT record.machine_id, "
                "COALESCE(machine.machine_name, record.machine_id) AS machine_name "
                "FROM measurement_records AS record "
                "LEFT JOIN machine ON CAST(machine.id AS TEXT) = record.machine_id "
                "ORDER BY machine_name, record.machine_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_record(self, session_id: str) -> dict | None:
        """按周期编号读取一条测量记录的完整查看字段。

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
                    "ordered_lines": '["ABC"]',  # OCR 文字 JSON
                    "final_frequency_hz": 50.0,  # 最终频率
                    "evidence_directory": "runtime/evidence/1",  # 证据目录
                    "needs_review": 0,  # 是否需要人工复核
                    "review_reason": None,  # 复核原因
                    "reviewed_at": None,  # 人工复核时间
                    "reviewed_lines": None,  # 人工修改文字 JSON
                }
                None  # 周期编号没有对应记录
        """
        # 按周期编号读取测量详情及对应机器名称。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT record.session_id, record.machine_id, "
                "COALESCE(machine.machine_name, record.machine_id) AS machine_name, "
                "record.start_time, record.finish_time, record.ordered_lines, "
                "record.final_frequency_hz, record.evidence_directory, "
                "record.needs_review, record.review_reason, "
                "record.reviewed_at, record.reviewed_lines "
                "FROM measurement_records AS record "
                "LEFT JOIN machine ON CAST(machine.id AS TEXT) = record.machine_id "
                "WHERE record.session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_review(
        self, session_id: str, reviewed_at: str, reviewed_lines: str | None
    ) -> bool:
        """只对尚未复核的测量记录写入人工结论。

        Args:
            session_id: 待复核的测量周期编号。
            reviewed_at: UTC ISO 格式的复核时间。
            reviewed_lines: 人工文字 JSON；确认原结果时为 None。

        Returns:
            返回示例：
                True  # 本次完成了人工复核
                False  # 记录已复核或不属于待复核记录
        """
        # 在一次条件更新中写入复核时间和人工文字。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE measurement_records "
                    "SET reviewed_at = ?, reviewed_lines = ? "
                    "WHERE session_id = ? AND needs_review = 1 "
                    "AND reviewed_at IS NULL",
                    (reviewed_at, reviewed_lines, session_id),
                )
                return cursor.rowcount == 1
