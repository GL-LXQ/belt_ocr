"""验证数据库机器连接、卡片反馈和窗口退出收尾。"""

import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtTest import QTest

from app import App
from config_util import load_config
from configuration_support import create_machine_database, write_configuration_files
from fake_mvs import FakeMvsSdk
from test_ui_shell import application, window


@pytest.fixture
def monitoring_environment(window, tmp_path, monkeypatch):
    """准备真实窗口、独立配置和相机替身。

    Args:
        window: 测试窗口。
        tmp_path: 临时目录。
        monkeypatch: 测试替换工具。

    Yields:
        (page, sdk, warnings)  # 监测页、相机替身和提示记录

    Returns:
        None  # 测试结束后等待后台释放
    """
    # 写入与窗口共用业务库的公共配置。
    page = window.page_stack.widget(0)
    settings = {
        "database_path": str(window.machine_service.machine_repo.database_path),
        "evidence_directory": str(tmp_path / "evidence"),
        "mvs_development_directory": "fake-sdk",
    }
    page.configuration_directory = tmp_path / "config"
    write_configuration_files(page.configuration_directory, settings)

    # 替换硬件入口和模态提示，保留正式 App 与线程流程。
    sdk = FakeMvsSdk()
    warnings = Mock()
    monkeypatch.setattr("app.load_mvs_sdk", Mock(return_value=sdk))
    monkeypatch.setattr("ui.pages.realtime_page.QMessageBox.warning", warnings)
    yield page, sdk, warnings

    # 失败断言时也通知后台收尾，避免销毁运行中的线程。
    service = page.monitoring_service
    if service is not None:
        service.stop_requested.set()
        assert service.wait(5000)
        QTest.qWait(10)


def wait_for_condition(condition):
    """处理 Qt 事件直到状态满足或测试超时。

    Args:
        condition: 返回布尔值的状态检查。

    Returns:
        None  # 条件已满足，否则断言失败
    """
    # 允许后台执行，同时持续投递主线程的 Qt 信号。
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


def test_connects_enabled_database_machines_and_stops(monitoring_environment):
    """验证启用机器绑定、刷新状态保留及主动停止。

    Args:
        monitoring_environment: 页面、相机替身和提示记录。

    Returns:
        None  # 机器正确绑定，停止后所有相机释放
    """
    page, sdk, warnings = monitoring_environment
    repository = page.machine_service.machine_repo
    # 建立启用、停用和软删除机器，序列号不依赖卡片序号。
    machine_ids = create_machine_database(repository.database_path, [
        {
            "machine_name": "一号机器",
            "camera_serial": "SERIAL-B",
            "frequency_meter_serial": "FREQ-B",
        },
        {
            "machine_name": "停用机器",
            "camera_serial": "DISABLED",
            "frequency_meter_serial": "FREQ-D",
            "enabled": False,
        },
        {
            "machine_name": "删除机器",
            "camera_serial": "DELETED",
            "frequency_meter_serial": "FREQ-X",
        },
        {
            "machine_name": "二号机器",
            "camera_serial": "SERIAL-A",
            "frequency_meter_serial": "FREQ-A",
        },
    ])
    first_id, _, deleted_id, second_id = machine_ids
    repository.soft_delete(deleted_id)

    # 读取公共配置并构建应用，只绑定启用机器且编号取业务库自增编号。
    configured_app = App(load_config(page.configuration_directory))
    machine_managers = list(configured_app.machine_managers.values())
    assert [manager.machine.machine_id for manager in machine_managers] == [str(first_id), str(second_id)]
    assert [manager.machine.camera_serial for manager in machine_managers] == ["SERIAL-B", "SERIAL-A"]
    assert [manager.machine.frequency_meter_serial for manager in machine_managers] == ["FREQ-B", "FREQ-A"]
    assert all(not manager.machine.simulated_frequencies_hz for manager in machine_managers)

    # 点击后连接，刷新仍显示实际连接状态且尚未取流。
    page.start_button.click()
    wait_for_condition(lambda: len(sdk.cameras) == 2 and all(
        card.badge.text() == "相机已连接" for card in page.machine_cards
    ))
    page.reload_machines()
    assert all(card.badge.text() == "相机已连接" for card in page.machine_cards)
    assert all(not camera.grabbing for camera in sdk.cameras.values())
    assert set(sdk.cameras) == {"SERIAL-B", "SERIAL-A"}

    # 主动停止等待释放，再允许下一次启动。
    page.stop_button.click()
    wait_for_condition(lambda: page.monitoring_service is None)
    assert sdk.closed and all(camera.closed for camera in sdk.cameras.values())
    assert all(card.badge.text() == "已停止" for card in page.machine_cards)
    assert page.start_button.isEnabled()
    warnings.assert_not_called()


@pytest.mark.parametrize("reason", ["未找到相机：BAD", "打开相机 BAD 失败，错误码：0x80000003"])
def test_connection_failure_releases_other_cameras(monitoring_environment, monkeypatch, reason):
    """验证第二台相机失败时展示原因并释放已连接相机。

    Args:
        monitoring_environment: 页面、相机替身和提示记录。
        monkeypatch: 测试替换工具。
        reason: SDK 故障信息。

    Returns:
        None  # 原因展示在对应卡片，其他机器已停止
    """
    page, sdk, warnings = monitoring_environment
    repository = page.machine_service.machine_repo
    repository.insert("正常机器", "GOOD", "FREQ-G")
    repository.insert("故障机器", "BAD", "FREQ-B")
    # 第一台真实替身打开，第二台返回 SDK 边界故障。
    camera = sdk.open_camera("GOOD")
    monkeypatch.setattr(sdk, "open_camera", Mock(side_effect=[camera, RuntimeError(reason)]))
    page.start_button.click()
    wait_for_condition(lambda: page.monitoring_service is None)

    # 失败卡片保留具体原因，已连接卡片显示整体退出。
    assert page.machine_cards[1].badge.text() == "连接失败"
    assert reason in page.machine_cards[1].toolTip()
    assert page.machine_cards[0].badge.text() == "监测失败"
    assert sdk.closed and camera.closed
    warnings.assert_called_once()


def test_empty_machines_do_not_open_sdk(monitoring_environment, monkeypatch):
    """验证空机器列表提示用户且不加载硬件。

    Args:
        monitoring_environment: 页面、相机替身和提示记录。
        monkeypatch: 测试替换工具。

    Returns:
        None  # 无启用机器时启动结束且按钮恢复
    """
    page, sdk, warnings = monitoring_environment
    loader = Mock(return_value=sdk)
    monkeypatch.setattr("app.load_mvs_sdk", loader)
    page.start_button.click()
    wait_for_condition(lambda: page.monitoring_service is None)
    loader.assert_not_called()
    assert "没有启用的机器" in warnings.call_args.args[2]


def test_close_window_waits_for_connecting_camera(monitoring_environment, window, monkeypatch):
    """验证连接尚未返回时关闭窗口仍等待相机释放。

    Args:
        monitoring_environment: 页面、相机替身和提示记录。
        window: 主窗口。
        monkeypatch: 测试替换工具。

    Returns:
        None  # 后台连接返回并释放资源后窗口关闭
    """
    page, sdk, warnings = monitoring_environment
    page.machine_service.machine_repo.insert("机器", "SERIAL", "FREQ")
    entered = threading.Event()
    release = threading.Event()
    open_camera = sdk.open_camera

    def open_blocked_camera(serial, **parameters):
        """等待测试放行后打开相机替身。

        Args:
            serial: 相机序列号。
            parameters: 相机配置参数。

        Returns:
            MvsCamera  # 正式相机封装的测试实例
        """
        entered.set()
        assert release.wait(5)
        return open_camera(serial, **parameters)

    # 阻塞连接并请求关闭，窗口在后台结束前继续存在。
    monkeypatch.setattr(sdk, "open_camera", open_blocked_camera)
    page.start_button.click()
    try:
        wait_for_condition(entered.is_set)
        window.close()
        assert window.isVisible()
    finally:
        release.set()

    # 放行连接后自动停止，不留下相机资源。
    wait_for_condition(lambda: page.monitoring_service is None and not window.isVisible())
    assert sdk.closed and sdk.cameras["SERIAL"].closed
    warnings.assert_not_called()
