"""封装 machine 表的数据库操作。"""

import sqlite3


class MachineRepo:
    """管理设备表结构，设备读写接口按后续业务需求添加。"""

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
