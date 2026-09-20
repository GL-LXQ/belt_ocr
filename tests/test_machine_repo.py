"""设备信息插入与数据库约束测试。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from repo.machine_repo import MachineRepo


def test_insert_persists_machine_fields_and_default_times(tmp_path):
    """验证机器字段、默认值和 UTC 时间持久化。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 新连接可读取完整机器信息与默认时间
    """
    # 创建临时设备表并记录插入前的 UTC 时间。
    database_path = tmp_path / "machines.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    before_insert = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 分别插入默认启用设备和带备注的停用设备。
    first_id = repository.insert("M01", "1# 皮带机", "CAM001", "FREQ001")
    second_id = repository.insert("M02", "2# 皮带机", "CAM002", "FREQ002", False, "入口 'A'")
    after_insert = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 使用新连接验证提交结果、默认值和时间字段。
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "SELECT id, machine_id, machine_name, camera_serial, frequency_meter_serial, "
            "enabled, remark, created_at, updated_at FROM machine ORDER BY id"
        ).fetchall()
    assert rows[0][:7] == (first_id, "M01", "1# 皮带机", "CAM001", "FREQ001", 1, None)
    assert rows[1][:7] == (second_id, "M02", "2# 皮带机", "CAM002", "FREQ002", 0, "入口 'A'")
    assert second_id > first_id
    for row in rows:
        assert before_insert <= row[7] <= after_insert
        assert row[7] == row[8]


def test_insert_duplicate_machine_keeps_original_record(tmp_path):
    """验证重复编号插入失败且原记录保持不变。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 唯一约束拒绝重复记录且后续插入正常
    """
    # 创建临时设备表并插入初始机器。
    database_path = tmp_path / "machines.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    repository.insert("M01", "原机器", "CAM001", "FREQ001")

    # 重复机器编号触发约束异常，随后插入另一台机器。
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        repository.insert("M01", "重复机器", "CAM002", "FREQ002")
    repository.insert("M02", "另一台机器", "CAM002", "FREQ002")

    # 确认失败插入没有覆盖原记录或阻止后续提交。
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT machine_id, machine_name FROM machine ORDER BY id").fetchall()
    assert rows == [("M01", "原机器"), ("M02", "另一台机器")]
