"""验证机器业务服务的编号返回和重复字段异常。"""

from unittest.mock import Mock

import pytest

from src.service.machine_service import MachineDuplicateFieldError, MachineService


def test_machine_service_returns_machine_ids() -> None:
    """验证新增和修改成功时返回机器编号。

    Args:
        无外部参数。

    Returns:
        None  # 两种保存操作均返回机器编号
    """
    # 设置无重复字段的机器 Repo。
    machine_repo = Mock()
    machine_repo.find_duplicate_field.return_value = None
    machine_repo.insert.return_value = 7
    machine_service = MachineService(machine_repo)

    # 核对新增操作返回已插入的编号。
    assert machine_service.create_machine("皮带机", "CAM001", "FREQ001") == 7
    machine_repo.insert.assert_called_once_with(
        "皮带机", "CAM001", "FREQ001", True, None
    )

    # 核对修改操作返回正在编辑的编号。
    assert machine_service.update_machine(7, "皮带机", "CAM001", "FREQ001") == 7
    machine_repo.update.assert_called_once_with(
        7, "皮带机", "CAM001", "FREQ001", True, None
    )


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("machine_name", "机器名称已存在，请修改。"),
        ("camera_serial", "相机序列号已被其他机器使用。"),
        ("frequency_meter_serial", "频率仪序列号已被其他机器使用。"),
    ),
)
def test_machine_service_reports_duplicate_field(field: str, message: str) -> None:
    """验证新增和修改时保留重复字段与原有提示。

    Args:
        field: Repo 报告的重复字段名。
        message: 对应字段的页面提示。

    Returns:
        None  # 两种保存操作均拒绝重复字段
    """
    # 设置机器 Repo 的重复字段结果。
    machine_repo = Mock()
    machine_repo.find_duplicate_field.return_value = field
    machine_service = MachineService(machine_repo)

    # 核对新增操作的重复字段异常。
    with pytest.raises(MachineDuplicateFieldError) as create_error:
        machine_service.create_machine("皮带机", "CAM001", "FREQ001")
    assert create_error.value.field == field
    assert str(create_error.value) == message
    machine_repo.insert.assert_not_called()

    # 核对修改操作的重复字段异常。
    with pytest.raises(MachineDuplicateFieldError) as update_error:
        machine_service.update_machine(7, "皮带机", "CAM001", "FREQ001")
    assert update_error.value.field == field
    assert str(update_error.value) == message
    machine_repo.update.assert_not_called()
