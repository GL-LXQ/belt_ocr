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
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> int:
        """插入一台机器的信息并返回自增主键。

        Args:
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
                "(machine_name, camera_serial, frequency_meter_serial, enabled, remark) "
                "VALUES (?, ?, ?, ?, ?)",
                (machine_name, camera_serial, frequency_meter_serial, enabled, remark),
            )
            return cursor.lastrowid

    def find_duplicate_field(self, machine_name: str, camera_serial: str, frequency_meter_serial: str) -> str | None:
        """查询机器名称、相机和频率仪序列号，返回首个重复字段。

        Args:
            machine_name: 待新增的机器名称。
            camera_serial: 待绑定的相机序列号。
            frequency_meter_serial: 待绑定的频率仪序列号。

        Returns:
            返回示例：
                "machine_name"  # 机器名称重复
                "camera_serial"  # 相机序列号重复
                "frequency_meter_serial"  # 频率仪序列号重复
                None  # 三个字段均未重复
        """
        # 一次查询三个字段是否已有对应记录。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            duplicates = connection.execute(
                "SELECT "
                "EXISTS(SELECT 1 FROM machine WHERE machine_name = ?), "
                "EXISTS(SELECT 1 FROM machine WHERE camera_serial = ?), "
                "EXISTS(SELECT 1 FROM machine WHERE frequency_meter_serial = ?)",
                (machine_name, camera_serial, frequency_meter_serial),
            ).fetchone()

        # 按表单字段顺序返回首个重复项。
        fields = ("machine_name", "camera_serial", "frequency_meter_serial")
        for field, duplicated in zip(fields, duplicates):
            if duplicated:
                return field
        return None

    def list_all(self) -> list[dict]:
        """按编号读取全部设备信息。

        Args:
            无。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 设备编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 查询设备字段并关闭读取连接。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, machine_name, camera_serial, frequency_meter_serial, "
                "enabled, created_at, updated_at, remark FROM machine ORDER BY id"
            ).fetchall()

        # 将数据库字段转换为页面使用的数据格式。
        devices = []
        for row in rows:
            device = dict(row)
            device["enabled"] = bool(device["enabled"])
            device["remark"] = device["remark"] or ""
            devices.append(device)
        return devices

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
                machine_name TEXT NOT NULL UNIQUE, -- 机器显示名称
                camera_serial TEXT NOT NULL UNIQUE, -- 绑定的相机序列号
                frequency_meter_serial TEXT NOT NULL UNIQUE, -- 绑定的频率仪序列号
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)), -- 是否启用，1启用、0停用
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 创建时间，UTC，格式为YYYY-MM-DD HH:MM:SS
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 修改时间，UTC，保存修改时由写入方更新
                remark TEXT -- 设备备注，可为空
            );
        """)
