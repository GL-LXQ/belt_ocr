"""不创建窗口地验证配置控制器、监测生命周期和下次启动读取。"""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.controller.controller import AppController, Result
from src.runtime.system_runtime_thread import SystemRuntimeThread
from src.service.configuration_service import ConfigurationService, ConfigurationServiceError
from src.service.machine_service import MachineServiceError
from ui.pages.system_configuration_page import normalize_preview_value


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
    assert set(read_result.data) == {"settings"}

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


@pytest.mark.parametrize(
    ("thread_running", "stop_requested"),
    [(False, False), (True, False), (True, True), (False, True)],
    ids=["starting", "running", "stopping", "cleanup_pending"],
)
def test_backend_save_guard_covers_every_owned_thread_state(
    configuration_controller: AppController,
    thread_running: bool,
    stop_requested: bool,
) -> None:
    """确认线程启动、运行、停止和清理阶段都禁止后端保存。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。
        thread_running: 线程是否报告正在运行。
        stop_requested: 是否已经提交停止请求。

    Returns:
        返回示例：
            None  # 只要控制器持有线程就拒绝保存，不查询机器或写文件
    """
    # 不创建或启动真实线程，仅模拟完整生命周期的四种状态。
    controller = configuration_controller
    controller.runtime_thread = Mock()
    controller.runtime_thread.isRunning.return_value = thread_running
    controller.runtime_thread.stop_requested.is_set.return_value = stop_requested
    controller.configuration_service = Mock()

    result = controller.save_configuration({"camera_gain": 2.0})
    assert not result.success
    controller.machine_service.list_machines.assert_not_called()
    controller.configuration_service.save_configuration.assert_not_called()


def test_configuration_save_waits_for_finished_cleanup_and_state_signals(
    configuration_controller: AppController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认停止请求不会解锁保存，只有线程结束回调完成后才允许保存。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 生命周期信号与后端保存保护覆盖启动和完全清理
    """
    # 替换线程启动操作，不创建工作线程或访问设备。
    controller = configuration_controller
    draft = controller.read_configuration().data["settings"]
    draft["camera_gain"] = 3.5
    state_changes = []

    def record_monitoring_state() -> None:
        """在状态通知到达时读取控制器是否仍持有线程。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 当前线程占用状态已加入通知记录
        """
        state_changes.append(controller.runtime_thread is not None)

    controller.monitoring_state_changed_signal.connect(record_monitoring_state)
    monkeypatch.setattr(SystemRuntimeThread, "start", Mock())

    # 启动后尚未运行的线程仍持有保存锁，停止请求也不清除引用。
    assert controller.start_monitoring().success
    runtime_thread = controller.runtime_thread
    assert not runtime_thread.isRunning()
    assert not controller.save_configuration(draft).success
    assert controller.stop_monitoring().success
    assert runtime_thread.stop_requested.is_set()
    assert not controller.save_configuration(draft).success

    # 手动发送结束通知，完成控制器原有清理流程后保存才成功。
    runtime_thread.finished.emit()
    assert controller.runtime_thread is None
    assert state_changes == [True, False]
    assert controller.save_configuration(draft).success


def test_next_runtime_start_reads_saved_yaml_without_hot_updating_current_config(
    configuration_controller: AppController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认配置保存只影响下次 Runtime 构造，不启动任何真实设备。

    Args:
        configuration_controller: 使用临时 YAML 的控制器。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 两次 Runtime 使用各自启动时的 YAML，新值只出现在第二次
    """
    # 用异步替身替换 Runtime，保留 SystemRuntimeThread 的真实配置读取。
    controller = configuration_controller
    runtime = Mock(failure=None)
    runtime.start = AsyncMock()
    runtime.stop = AsyncMock()
    runtime_factory = Mock(return_value=runtime)
    monkeypatch.setattr("src.runtime.system_runtime_thread.SystemRuntime", runtime_factory)
    first_thread = SystemRuntimeThread(controller.configuration_directory)
    first_thread.stop_requested.set()
    first_thread.run()
    first_configuration = runtime_factory.call_args.args[0]
    assert first_configuration.camera_gain == 1.0
    assert first_thread.failure_message == ""

    # 正常保存只更新 YAML，已经构建的旧运行配置保持原值。
    draft = controller.read_configuration().data["settings"]
    draft["camera_gain"] = 4.5
    assert controller.save_configuration(draft).success
    assert first_configuration.camera_gain == 1.0
    assert runtime_factory.call_count == 1

    # 下次线程入口重新读盘并传入新值，整个测试不启动 QThread。
    next_thread = SystemRuntimeThread(controller.configuration_directory)
    next_thread.stop_requested.set()
    next_thread.run()
    assert runtime_factory.call_args.args[0].camera_gain == 4.5
    assert runtime_factory.call_count == 2
    assert runtime.start.await_count == 2
    assert runtime.stop.await_count == 2
    assert next_thread.failure_message == ""


def test_integer_preview_normalization_keeps_type_corrections_dirty() -> None:
    """确认整数草稿将错误小数修正为整数时不会被当作未修改。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 纯辅助函数保留整数类型差异，不创建窗口或应用
    """
    assert normalize_preview_value(1000.0, "integer") != normalize_preview_value(1000, "integer")
    assert normalize_preview_value("1000", "integer") != normalize_preview_value(1000, "integer")
    assert normalize_preview_value(True, "integer") != normalize_preview_value(1, "integer")
    assert normalize_preview_value(80, "number") == normalize_preview_value(80.0, "number")
    assert normalize_preview_value(None, "number") != normalize_preview_value(0, "number")
