"""封装 abnormal_events 表的读写操作。"""

import sqlite3
from contextlib import closing
from pathlib import Path


class AbnormalEventRepo:
    """管理运行库中的异常事件记录。"""

    @staticmethod
    def create_table(connection: sqlite3.Connection) -> None:
        """创建异常事件表并保留已有数据。

        Args:
            connection: 初始化流程提供的数据库连接。

        Returns:
            返回示例：
                None  # 异常事件表已就绪
        """
        # 在初始化连接中创建异常事件表。
        connection.execute("""
            CREATE TABLE IF NOT EXISTS abnormal_events (
                abnormal_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                machine_id TEXT,
                session_id TEXT,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
        """)

    def __init__(self, database_path: Path):
        """保存运行数据库路径。

        Args:
            database_path: 运行数据库文件路径。

        Returns:
            返回示例：
                None  # 初始化异常事件表访问对象
        """
        # 保存运行数据库路径。
        self.database_path = database_path

    def insert(
        self,
        created_at: float,
        machine_id: str,
        session_id: str | None,
        reason: str,
        payload_json: str,
    ) -> int:
        """插入一条异常事件记录。

        Args:
            created_at: 事件创建时间，Unix 时间戳。
            machine_id: 所属机器编号。
            session_id: 所属周期编号，无周期时为空。
            reason: 异常原因描述。
            payload_json: 已序列化的事件内容。

        Returns:
            返回示例：
                1  # 新增异常事件的自增主键
        """
        # 在运行库事务中写入异常事件并返回主键。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            with connection:
                cursor = connection.execute(
                    "INSERT INTO abnormal_events "
                    "(created_at, machine_id, session_id, reason, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (created_at, machine_id, session_id, reason, payload_json),
                )
                return cursor.lastrowid

    def list_machine_ids(self) -> list[str]:
        """读取已有异常事件关联的机器编号。

        Args:
            无外部参数。

        Returns:
            返回示例：
                ["1", "2"]  # 有异常事件的机器编号
        """
        # 从异常事件表读取可用于筛选的机器编号。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            rows = connection.execute(
                "SELECT DISTINCT machine_id FROM abnormal_events "
                "WHERE machine_id IS NOT NULL AND machine_id != '' "
                "ORDER BY machine_id"
            ).fetchall()
            return [machine_id for (machine_id,) in rows]

    def list_events(
        self,
        machine_id: str | None = None,
        session_id: str | None = None,
        start_created_at: float | None = None,
        end_created_at: float | None = None,
    ) -> list[dict]:
        """按机器、完整 Session ID 和记录时间查询异常事件。

        Args:
            machine_id: 指定机器编号，None 表示全部机器。
            session_id: 完整 Session ID，None 表示全部周期。
            start_created_at: 记录时间的 Unix 时间戳下界，包含该时刻。
            end_created_at: 记录时间的 Unix 时间戳上界，不包含该时刻。

        Returns:
            返回示例：
                [{
                    "abnormal_event_id": 5,  # 异常事件主键
                    "created_at": 1790496570.9,  # 记录写入时间戳
                    "machine_id": "1",  # 机器编号
                    "session_id": "session-1",  # 周期编号
                    "reason": "OCR 识别超时",  # 异常原因描述
                    "payload_json": "{}",  # 原始事件内容
                }]
        """
        # 组合机器和完整周期编号的精确筛选条件。
        conditions = []
        parameters = []
        if machine_id is not None:
            conditions.append("machine_id = ?")
            parameters.append(machine_id)
        if session_id is not None:
            conditions.append("session_id = ?")
            parameters.append(session_id)

        # 在排序前筛选包含下界、不包含上界的记录时间范围。
        if start_created_at is not None:
            conditions.append("created_at >= ?")
            parameters.append(start_created_at)
        if end_created_at is not None:
            conditions.append("created_at < ?")
            parameters.append(end_created_at)

        # 按记录写入时间和主键倒序读取匹配结果。
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = (
            "SELECT abnormal_event_id, created_at, machine_id, session_id, "
            "reason, payload_json FROM abnormal_events"
            + where_clause
            + " ORDER BY created_at DESC, abnormal_event_id DESC"
        )

        # 将查询行转换为页面可用的字段字典。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
            return [dict(row) for row in rows]

    def get_event(self, abnormal_event_id: int) -> dict | None:
        """按异常事件主键读取完整记录。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            返回示例：
                {
                    "abnormal_event_id": 5,  # 异常事件主键
                    "created_at": 1790496570.9,  # 记录写入时间戳
                    "machine_id": "1",  # 机器编号
                    "session_id": "session-1",  # 周期编号
                    "reason": "OCR 识别超时",  # 异常原因描述
                    "payload_json": "{}",  # 原始事件内容
                }
                None  # 记录不存在
        """
        # 按主键读取一条异常记录。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT abnormal_event_id, created_at, machine_id, session_id, "
                "reason, payload_json FROM abnormal_events WHERE abnormal_event_id = ?",
                (abnormal_event_id,),
            ).fetchone()
            return dict(row) if row is not None else None
