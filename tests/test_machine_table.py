"""验证设备表初始化、字段默认值和数据约束。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from configuration import MeasurementConfiguration
from database import Database


@pytest.fixture
def machine_database(tmp_path):
    """初始化临时双库并在测试后释放实例锁。

    Args:
        tmp_path: pytest 提供的临时目录。

    Yields:
        Database()  # 已初始化的临时数据库管理对象

    Returns:
        返回示例：
            None  # 测试结束后关闭数据库并释放实例锁
    """
    # 准备不连接硬件的临时数据库配置。
    configuration = MeasurementConfiguration(
        machines=(),
        database_path=tmp_path / "business" / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path / "sdk",
    )
    database = Database(configuration, AsyncMock())

    # 初始化双库，并在测试结束或异常时释放资源。
    try:
        database.initialize()
        yield database
    finally:
        database.close()


def test_machine_table_defaults_and_reinitialization(machine_database):
    """验证设备默认值及重复初始化保留已有记录。

    Args:
        machine_database: 已初始化的临时数据库管理对象。

    Returns:
        返回示例：
            None  # 默认值、字段结构和数据保留断言通过
    """
    # 确认建表未插入设备，并写入一条最小设备记录。
    with closing(sqlite3.connect(machine_database.configuration.database_path)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT COUNT(*) FROM machine").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'machines'").fetchone() is None
        with connection:
            connection.execute(
                "INSERT INTO machine (machine_name, camera_serial, frequency_meter_serial) "
                "VALUES (?, ?, ?)",
                ("1# 皮带机", "CAM001", "METER001"),
            )

        # 重复初始化后验证字段、默认启用状态和 UTC 时间。
        machine_database.initialize()
        record = connection.execute("SELECT * FROM machine").fetchone()
        assert record.keys() == [
            "id", "machine_name", "camera_serial", "frequency_meter_serial",
            "enabled", "created_at", "updated_at", "remark",
        ]
        assert record["id"] == 1
        assert record["machine_name"] == "1# 皮带机"
        assert record["enabled"] == 1
        assert record["remark"] is None
        assert record["created_at"] == record["updated_at"]
        created_at = datetime.strptime(record["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        assert 0 <= (datetime.now(timezone.utc) - created_at).total_seconds() < 10

        # 确认原有测量表仍存在且设备配置不进入运行库。
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    with closing(sqlite3.connect(machine_database.configuration.recovery_path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'machine'").fetchone() is None


def test_machine_table_constraints(machine_database):
    """验证设备必填字段和启用值约束。

    Args:
        machine_database: 已初始化的临时数据库管理对象。

    Returns:
        返回示例：
            None  # 数据库拒绝必填字段空值和非法启用值
    """
    # 写入一条停用设备并保留备注。
    statement = (
        "INSERT INTO machine (machine_name, camera_serial, frequency_meter_serial, enabled, remark) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    with closing(sqlite3.connect(machine_database.configuration.database_path)) as connection, connection:
        connection.execute(statement, ("1# 皮带机", "CAM001", "METER001", 0, "一号产线"))

        # 分别验证必填字段空值和非法启用值。
        invalid_records = [
            (None, "CAM002", "METER002", 1, None),
            ("2# 皮带机", None, "METER002", 1, None),
            ("2# 皮带机", "CAM002", None, 1, None),
            ("2# 皮带机", "CAM002", "METER002", 2, None),
        ]
        for record in invalid_records:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, record)
        assert connection.execute("SELECT enabled, remark FROM machine").fetchone() == (0, "一号产线")
