"""封装 machine 表的数据库操作。"""

import sqlite3
from contextlib import closing
from pathlib import Path


class MachineRepo:
    """管理机器表结构和机器信息写入。"""

    def __init__(self, database_path: Path):
        """保存机器信息所在的业务数据库路径。

        Args:
            database_path: 业务数据库文件路径。

        Returns:
            返回示例：
                None  # 初始化机器表访问对象
        """
        # 保存机器信息所在的业务数据库路径。
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
            remark: 机器备注，默认无备注。

        Returns:
            返回示例：
                1  # 新增机器记录的数据库自增主键
        """
        # 打开业务库连接，在事务中插入机器信息。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO machine "
                "(machine_name, camera_serial, frequency_meter_serial, enabled, remark) "
                "VALUES (?, ?, ?, ?, ?)",
                (machine_name, camera_serial, frequency_meter_serial, enabled, remark),
            )

            # 返回新增记录的自增主键。
            return cursor.lastrowid

    def find_duplicate_field(
        self,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        exclude_id: int | None = None,
    ) -> str | None:
        """查询机器名称、相机和频率仪序列号，返回首个重复字段。

        Args:
            machine_name: 待保存的机器名称。
            camera_serial: 待绑定的相机序列号。
            frequency_meter_serial: 待绑定的频率仪序列号。
            exclude_id: 编辑时排除的机器编号，新增时为 None。

        Returns:
            返回示例：
                "machine_name"  # 机器名称重复
                "camera_serial"  # 相机序列号重复
                "frequency_meter_serial"  # 频率仪序列号重复
                None  # 三个字段均未重复
        """
        # 一次查询三个字段在未删除记录中是否已有对应记录，并排除正在编辑的机器。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            duplicates = connection.execute(
                "SELECT "
                "EXISTS(SELECT 1 FROM machine WHERE machine_name = ? AND is_deleted = 0 AND id IS NOT ?), "
                "EXISTS(SELECT 1 FROM machine WHERE camera_serial = ? AND is_deleted = 0 AND id IS NOT ?), "
                "EXISTS(SELECT 1 FROM machine WHERE frequency_meter_serial = ? AND is_deleted = 0 AND id IS NOT ?)",
                (
                    machine_name, exclude_id,
                    camera_serial, exclude_id,
                    frequency_meter_serial, exclude_id,
                ),
            ).fetchone()

        # 按表单字段顺序返回首个重复项。
        fields = ("machine_name", "camera_serial", "frequency_meter_serial")
        for field, duplicated in zip(fields, duplicates):
            if duplicated:
                return field

        # 三个字段都不重复时返回空。
        return None

    def list_all(self) -> list[dict]:
        """按编号读取全部未删除机器信息。

        Args:
            无外部参数。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 机器编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 查询未删除机器字段并关闭读取连接。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, machine_name, camera_serial, frequency_meter_serial, "
                "enabled, created_at, updated_at, remark FROM machine "
                "WHERE is_deleted = 0 ORDER BY id"
            ).fetchall()

        # 将数据库字段转换为页面使用的数据格式。
        machines = []
        for row in rows:
            machine = dict(row)
            machine["enabled"] = bool(machine["enabled"])
            machine["remark"] = machine["remark"] or ""
            machines.append(machine)
        return machines

    def list_enabled(self) -> list[dict]:
        """按编号读取全部已启用机器信息。

        Args:
            无外部参数。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 机器编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 复用全部机器查询并过滤出已启用机器。
        return [machine for machine in self.list_all() if machine["enabled"]]

    def update(
        self,
        machine_id: int,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool,
        remark: str | None,
    ) -> int:
        """按编号更新机器信息并返回受影响行数。

        Args:
            machine_id: 要修改的机器编号。
            machine_name: 修改后的机器名称。
            camera_serial: 修改后的相机序列号。
            frequency_meter_serial: 修改后的频率仪序列号。
            enabled: 修改后的启用状态。
            remark: 修改后的备注，无备注时为 None。

        Returns:
            返回示例：
                1  # 受影响行数，0 表示没有对应记录
        """
        # 更新机器字段并显式刷新修改时间。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection, connection:
            cursor = connection.execute(
                "UPDATE machine SET machine_name = ?, camera_serial = ?, frequency_meter_serial = ?, "
                "enabled = ?, remark = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (machine_name, camera_serial, frequency_meter_serial, enabled, remark, machine_id),
            )

            # 返回受影响行数。
            return cursor.rowcount

    def soft_delete(self, machine_id: int) -> int:
        """按编号标记删除机器并返回受影响行数。

        Args:
            machine_id: 要删除的机器编号。

        Returns:
            返回示例：
                1  # 受影响行数，0 表示没有对应记录
        """
        # 置删除标记并显式刷新修改时间，保留原记录。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection, connection:
            cursor = connection.execute(
                "UPDATE machine SET is_deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (machine_id,),
            )

            # 返回受影响行数。
            return cursor.rowcount

    @staticmethod
    def create_table(connection: sqlite3.Connection) -> None:
        """创建机器表和只约束未删除记录的唯一索引，保留已有记录。

        Args:
            connection: 初始化流程提供的数据库连接。

        Returns:
            返回示例：
                None  # 机器表已就绪
        """
        # 在初始化连接中创建机器表。
        connection.execute("""
            CREATE TABLE IF NOT EXISTS machine (
                id INTEGER PRIMARY KEY AUTOINCREMENT, -- 数据库内部自增主键
                machine_name TEXT NOT NULL, -- 机器显示名称
                camera_serial TEXT NOT NULL, -- 绑定的相机序列号
                frequency_meter_serial TEXT NOT NULL, -- 绑定的频率仪序列号
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)), -- 是否启用，1启用、0停用
                is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)), -- 是否已删除，1已删除、0正常
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 创建时间，UTC，格式为YYYY-MM-DD HH:MM:SS
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 修改时间，UTC，保存修改时由写入方更新
                remark TEXT -- 机器备注，可为空
            );
        """)

        # 机器名称、相机和频率仪序列号只在未删除记录中保持唯一。
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS machine_name_active ON machine(machine_name) WHERE is_deleted = 0"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS machine_camera_serial_active "
            "ON machine(camera_serial) WHERE is_deleted = 0"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS machine_frequency_meter_serial_active "
            "ON machine(frequency_meter_serial) WHERE is_deleted = 0"
        )
