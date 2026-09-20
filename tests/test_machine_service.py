"""验证设备业务服务转换数据库错误。"""

import sqlite3
from contextlib import closing

import pytest

from src.machine_service import MachineService, MachineServiceError
from src.repo.machine_repo import MachineRepo


@pytest.mark.parametrize("operation, database_error", [
    ("create_machine", sqlite3.OperationalError("database is locked")),
    ("create_machine", sqlite3.IntegrityError("CHECK constraint failed")),
    ("list_machines", sqlite3.OperationalError("no such table: machine")),
])
def test_database_errors_become_service_errors(tmp_path, monkeypatch, operation, database_error):
    """验证数据库故障转换为业务异常。

    Args:
        tmp_path: 临时数据库目录。
        monkeypatch: 数据访问方法替换工具。
        operation: 被测试的业务操作。
        database_error: 数据访问层抛出的异常。

    Returns:
        返回示例：
            None  # 数据库故障保留异常原因
    """
    # 创建业务服务并替换对应的数据访问操作。
    repository = MachineRepo(tmp_path / "machines.sqlite3")
    with closing(sqlite3.connect(repository.database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    service = MachineService(repository)

    def fail_database_operation(*arguments):
        """模拟数据访问异常。

        Args:
            arguments: 传入数据访问方法的位置参数。

        Returns:
            返回示例：
                None  # 不返回结果，抛出指定数据库异常
        """
        raise database_error

    repository_operation = "insert" if operation == "create_machine" else "list_all"
    monkeypatch.setattr(repository, repository_operation, fail_database_operation)

    # 执行业务操作并检查异常类型和原始原因。
    with pytest.raises(MachineServiceError) as captured:
        if operation == "create_machine":
            service.create_machine("皮带机", "CAM001", "FREQ001")
        else:
            service.list_machines()
    assert captured.value.__cause__ is database_error
    assert "失败" in str(captured.value)

@pytest.mark.parametrize("duplicate_field", [
    "machine_name",
    "camera_serial",
    "frequency_meter_serial",
])
def test_create_returns_success_and_duplicate_results(tmp_path, duplicate_field):
    """验证成功和各字段重复均通过返回结果表达。

    Args:
        tmp_path: 临时数据库目录。
        duplicate_field: 本次重复的设备字段。

    Returns:
        返回示例：
            None  # 成功返回编号，重复返回字段且没有新增记录
    """
    # 初始化真实数据库并创建一条设备记录。
    database_path = tmp_path / "machines.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    service = MachineService(MachineRepo(database_path))
    values = {
        "machine_name": "皮带机",
        "camera_serial": "CAM001",
        "frequency_meter_serial": "FREQ001",
    }
    result = service.create_machine(**values)
    assert result == {
        "success": True,
        "device_id": 1,
        "field": None,
        "message": "设备创建成功",
    }

    # 仅重复指定字段并验证返回值和数据库记录数量。
    for field in values:
        if field != duplicate_field:
            values[field] += "新"
    result = service.create_machine(**values)
    assert result["success"] is False
    assert result["device_id"] is None
    assert result["field"] == duplicate_field
    assert result["message"]
    assert len(service.list_machines()) == 1


def test_concurrent_duplicate_is_rejected_by_database(tmp_path, monkeypatch):
    """验证预查询后发生的重复写入由唯一约束拒绝。

    Args:
        tmp_path: 临时数据库目录。
        monkeypatch: 重复查询替换工具。

    Returns:
        返回示例：
            None  # 并发重复提示保存失败且不会新增重复记录
    """
    # 建表并写入已有设备。
    repository = MachineRepo(tmp_path / "machines.sqlite3")
    with closing(sqlite3.connect(repository.database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository.insert("皮带机", "CAM001", "FREQ001")
    service = MachineService(repository)

    # 模拟预查询未发现其他写入方的数据。
    monkeypatch.setattr(repository, "find_duplicate_field", lambda *arguments: None)
    with pytest.raises(MachineServiceError) as captured:
        service.create_machine("皮带机", "CAM002", "FREQ002")
    assert isinstance(captured.value.__cause__, sqlite3.IntegrityError)
    assert len(service.list_machines()) == 1
