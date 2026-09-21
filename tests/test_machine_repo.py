"""机器信息插入与数据库约束测试。"""

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
    # 创建临时机器表并记录插入前的 UTC 时间。
    database_path = tmp_path / "machines.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    before_insert = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 分别插入默认启用机器和带备注的停用机器。
    first_id = repository.insert("1# 皮带机", "CAM001", "FREQ001")
    second_id = repository.insert("2# 皮带机", "CAM002", "FREQ002", False, "入口 'A'")
    after_insert = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 使用新连接验证提交结果、默认值和时间字段。
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "SELECT id, machine_name, camera_serial, frequency_meter_serial, "
            "enabled, remark, created_at, updated_at FROM machine ORDER BY id"
        ).fetchall()
    assert rows[0][:6] == (first_id, "1# 皮带机", "CAM001", "FREQ001", 1, None)
    assert rows[1][:6] == (second_id, "2# 皮带机", "CAM002", "FREQ002", 0, "入口 'A'")
    assert second_id > first_id
    for row in rows:
        assert before_insert <= row[6] <= after_insert
        assert row[6] == row[7]


def test_insert_failure_keeps_original_record(tmp_path):
    """验证非法启用值插入失败且原记录保持不变。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 启用值约束拒绝非法记录且后续插入正常
    """
    # 创建临时机器表并插入初始机器。
    database_path = tmp_path / "machines.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    repository.insert("原机器", "CAM001", "FREQ001")

    # 非法启用值触发约束异常，随后插入另一台机器。
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        repository.insert("非法机器", "CAM002", "FREQ002", enabled=2)
    repository.insert("另一台机器", "CAM002", "FREQ002")

    # 确认失败插入没有覆盖原记录或阻止后续提交。
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT id, machine_name FROM machine ORDER BY id").fetchall()
    assert rows == [(1, "原机器"), (2, "另一台机器")]
