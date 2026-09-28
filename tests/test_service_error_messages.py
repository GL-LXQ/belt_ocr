"""验证 Service 对外错误不包含底层数据库信息。"""

import sqlite3
from unittest.mock import Mock

import pytest

from src.service.abnormal_event_service import (
    AbnormalEventService,
    AbnormalEventServiceError,
)
from src.service.machine_service import MachineService, MachineServiceError
from src.service.measurement_record_service import (
    MeasurementRecordService,
    MeasurementRecordServiceError,
)


def test_machine_list_hides_database_message() -> None:
    """验证机器列表数据库故障只返回业务提示。

    Args:
        无外部参数。

    Returns:
        None  # 机器列表错误不包含数据库原文
    """
    # 设置机器 Repo 的数据库故障。
    machine_repo = Mock()
    machine_repo.list_all.side_effect = sqlite3.OperationalError("database is locked")
    machine_service = MachineService(machine_repo)

    # 核对机器服务的对外提示。
    with pytest.raises(MachineServiceError) as captured_error:
        machine_service.list_machines()
    assert str(captured_error.value) == "机器列表读取失败。"


def test_measurement_record_list_hides_database_message() -> None:
    """验证测量记录数据库故障只返回业务提示。

    Args:
        无外部参数。

    Returns:
        None  # 历史记录错误不包含数据库原文
    """
    # 设置测量记录 Repo 的数据库故障。
    measurement_record_repo = Mock()
    measurement_record_repo.count_records.return_value = 1
    measurement_record_repo.list_records.side_effect = sqlite3.OperationalError(
        "database is locked"
    )
    measurement_record_service = MeasurementRecordService(measurement_record_repo)

    # 核对测量记录服务的对外提示。
    with pytest.raises(MeasurementRecordServiceError) as captured_error:
        measurement_record_service.list_records()
    assert str(captured_error.value) == "历史记录读取失败。"


def test_abnormal_event_list_hides_database_message() -> None:
    """验证异常事件数据库故障只返回业务提示。

    Args:
        无外部参数。

    Returns:
        None  # 异常事件错误不包含数据库原文
    """
    # 设置异常事件 Repo 的数据库故障。
    abnormal_event_repo = Mock()
    abnormal_event_repo.list_events.side_effect = sqlite3.OperationalError(
        "database is locked"
    )
    abnormal_event_service = AbnormalEventService(abnormal_event_repo)

    # 核对异常事件服务的对外提示。
    with pytest.raises(AbnormalEventServiceError) as captured_error:
        abnormal_event_service.list_events()
    assert str(captured_error.value) == "异常事件读取失败。"
