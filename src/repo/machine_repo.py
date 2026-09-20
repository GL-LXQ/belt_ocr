"""封装 machine 表的数据库操作。"""

import sqlite3
from contextlib import closing
from pathlib import Path


class MachineRepo:
    """管理设备表结构和机器信息写入。"""

    def __init__(self, database_path: Path):
        """保存设备信息所在的业务数据库路径。

        Args:
            database_path: 业务数据库文件路径。

        Returns:
            返回示例：
                None  # 初始化设备表访问对象
        """
        self.database_path = database_path

    def insert(
        self,
        machine_id: str,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> int:
        """插入一台机器的信息并返回自增主键。

        Args:
            machine_id: 唯一的机器业务编号。
            machine_name: 机器显示名称。
            camera_serial: 绑定的相机序列号。
            frequency_meter_serial: 绑定的频率仪序列号。
            enabled: 是否启用，默认启用。
            remark: 设备备注，默认无备注。

        Returns:
            返回示例：
                1  # 新增机器记录的数据库自增主键
        """
        # 打开业务库连接，在事务中插入机器信息并使用表默认时间。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO machine "
                "(machine_id, machine_name, camera_serial, frequency_meter_serial, enabled, remark) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (machine_id, machine_name, camera_serial, frequency_meter_serial, enabled, remark),
            )
            return cursor.lastrowid

    @staticmethod
    def create_table(connection: sqlite3.Connection) -> None:
        """创建设备表并保留已有数据。

        Args:
            connection: 初始化流程提供的数据库连接。

        Returns:
            返回示例：
                None  # 设备表已就绪
        """
        # 在初始化连接中创建设备表。
        connection.execute("""
            CREATE TABLE IF NOT EXISTS machine (
                id INTEGER PRIMARY KEY AUTOINCREMENT, -- 数据库内部自增主键
                machine_id TEXT NOT NULL UNIQUE, -- 机器业务编号，唯一标识机器
                machine_name TEXT NOT NULL, -- 机器显示名称
                camera_serial TEXT NOT NULL, -- 绑定的相机序列号
                frequency_meter_serial TEXT NOT NULL, -- 绑定的频率仪序列号
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)), -- 是否启用，1启用、0停用
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 创建时间，UTC，格式为YYYY-MM-DD HH:MM:SS
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 修改时间，UTC，保存修改时由写入方更新
                remark TEXT -- 设备备注，可为空
            );
        """)
