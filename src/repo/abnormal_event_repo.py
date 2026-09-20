"""封装 abnormal_events 表的写入操作。"""

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
        self.database_path = database_path

    def insert(self, created_at: float, machine_id: str, session_id: str | None, reason: str, payload_json: str) -> int:
        """插入一条异常事件记录。

        Args:
            created_at: 事件创建时间，Unix 时间戳。
            machine_id: 所属机器编号。
            session_id: 所属周期编号，无周期时为空。
            reason: 异常原因标识。
            payload_json: 已序列化的事件内容。

        Returns:
            返回示例：
                1  # 新增异常事件的自增主键
        """
        # 在运行库事务中写入异常事件并返回主键。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO abnormal_events (created_at, machine_id, session_id, reason, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (created_at, machine_id, session_id, reason, payload_json),
            )
            return cursor.lastrowid
