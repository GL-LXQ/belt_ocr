"""保留配置控制器的纯后端回归，所有读写限定在临时目录。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.controller.controller import AppController, Result
from src.service.configuration_service import ConfigurationService, ConfigurationServiceError
from src.service.machine_service import MachineServiceError


@pytest.fixture
def configuration_controller(tmp_path: Path) -> AppController:
    """使用临时 YAML 和机器服务替身创建配置控制器。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            AppController(...)  # 未创建窗口且尚未启动监测的控制器
    """
    # 准备合法的最小配置，所有路径均位于测试临时目录。
    configuration_text = (
        "application:\n  database_path: data.sqlite3\n  evidence_directory: evidence\n"
        "camera:\n  mvs_development_directory: sdk\n  camera_gain: 1.0\n"
        "ocr: {}\nfrequency: {}\nmachine: {}\n"
        "io:\n  modbus_serial_port: COM8\n  io_machine_channels: {'1': 0}\n"
    )
    (tmp_path / "config.yaml").write_text(configuration_text, encoding="utf-8")

    # 控制器自行创建配置服务，其他业务使用不触及数据库的替身。
    machine_service = Mock()
    machine_service.list_machines.return_value = {
        "machines": [
            {
                "id": 1,
                "enabled": True,
            },
        ],
    }
    return AppController(machine_service, Mock(), Mock(), tmp_path)


def test_controller_owns_configuration_service_and_returns_saved_settings(
    configuration_controller: AppController,
) -> None:
    """确认控制器创建服务并统一返回读取、校验和保存结果。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。

    Returns:
        返回示例：
            None  # 读写结果包含 settings，校验成功结果没有额外数据
    """
    controller = configuration_controller
    assert isinstance(controller.configuration_service, ConfigurationService)
    read_result = controller.read_configuration()
    assert read_result.success
    assert set(read_result.data) == {"settings", "revision"}

    # 通过控制器完成校验和保存，再读取确认落盘结果。
    draft = read_result.data["settings"]
    draft["camera_gain"] = 2.5
    assert controller.validate_configuration(draft) == Result.ok()
    save_result = controller.save_configuration(draft)
    assert save_result.success
    assert save_result.data["settings"]["camera_gain"] == 2.5
    assert "下次" in save_result.message
    assert controller.read_configuration().data["settings"]["camera_gain"] == 2.5


def test_controller_refreshes_machine_records_for_validation_and_save(
    configuration_controller: AppController,
) -> None:
    """确认校验后的机器启用变化会在保存前重新检查。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。

    Returns:
        返回示例：
            None  # 保存读取最新机器状态并拒绝新启用机器的缺失通道
    """
    controller = configuration_controller
    draft = controller.read_configuration().data["settings"]
    draft["camera_gain"] = 3.0
    assert controller.validate_configuration(draft).success
    original_bytes = (controller.configuration_directory / "config.yaml").read_bytes()

    # 模拟校验后出现新的启用机器，原草稿没有该机器的 DI 行。
    controller.machine_service.list_machines.return_value["machines"].append({
        "id": 2,
        "enabled": True,
    })
    save_result = controller.save_configuration(draft)
    assert not save_result.success
    assert save_result.data == {"field": "io_machine_channels.2"}
    assert controller.machine_service.list_machines.call_count == 2
    assert (controller.configuration_directory / "config.yaml").read_bytes() == original_bytes


@pytest.mark.parametrize("operation_name", ["validate_configuration", "save_configuration"])
def test_controller_reports_service_field_errors_without_overwrite(
    configuration_controller: AppController,
    operation_name: str,
) -> None:
    """确认配置业务错误完整保留字段定位和原始文件。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。
        operation_name: 被测试的控制器入口名称。

    Returns:
        返回示例：
            None  # 失败结果包含相机增益字段，文件字节未变
    """
    controller = configuration_controller
    draft = controller.read_configuration().data["settings"]
    original_bytes = (controller.configuration_directory / "config.yaml").read_bytes()
    draft["camera_gain"] = -1

    result = getattr(controller, operation_name)(draft)
    assert not result.success
    assert result.data == {"field": "camera_gain"}
    assert result.message
    assert (controller.configuration_directory / "config.yaml").read_bytes() == original_bytes


def test_controller_read_failure_retains_service_error_fields(configuration_controller: AppController) -> None:
    """确认读取业务异常也按统一结果返回定位信息。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。

    Returns:
        返回示例：
            None  # 读取失败返回 service 提供的错误文案和字段
    """
    controller = configuration_controller
    controller.configuration_service = Mock()
    controller.configuration_service.read_configuration.side_effect = ConfigurationServiceError("配置读取失败", "camera")
    assert controller.read_configuration() == Result.error("配置读取失败", data={"field": "camera"})


@pytest.mark.parametrize("operation_name", ["validate_configuration", "save_configuration"])
def test_controller_machine_lookup_failure_does_not_call_configuration_service(
    configuration_controller: AppController,
    operation_name: str,
) -> None:
    """确认机器查询失败时不能跳过绑定检查继续校验或写入。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。
        operation_name: 被测试的控制器入口名称。

    Returns:
        返回示例：
            None  # 原始机器业务错误已展示，配置服务未执行
    """
    controller = configuration_controller
    controller.configuration_service = Mock()
    controller.machine_service.list_machines.side_effect = MachineServiceError("测试数据库不可用")

    result = getattr(controller, operation_name)({"camera_gain": 2.0})
    assert not result.success
    assert "测试数据库不可用" in result.message
    getattr(controller.configuration_service, operation_name).assert_not_called()
