"""验证皮带机动画控制接口和机器卡片频率显示。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.belt_animation import BeltAnimationWidget
from ui.pages.realtime_page import MachineCard


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """创建组件测试所需的 Qt 应用实例。

    Args:
        无。

    Returns:
        返回示例：
            QApplication([])  # 供测试组件使用的 Qt 应用
    """
    application = QApplication.instance()
    return application or QApplication([])


def test_animation_control_interfaces(qt_application: QApplication) -> None:
    """验证机器、采集和频率监听控制接口更新动画状态。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 控制接口状态与调用一致
    """
    animation_widget = BeltAnimationWidget()
    animation_widget.start_machine()
    assert animation_widget._machine_state.name == "STARTING"

    animation_widget.start_capture()
    animation_widget.set_frequency_listening(True)
    assert animation_widget.capturing is True
    assert animation_widget.frequency_listening is True

    animation_widget.stop_machine()
    assert animation_widget._machine_state.name == "STOPPING"
    assert animation_widget.capturing is False
    assert animation_widget.frequency_listening is False
    animation_widget.animation_timer.stop()
    animation_widget.extension_animation.stop()
    animation_widget.close()


def test_capture_and_frequency_controls_can_stop_independently(
    qt_application: QApplication,
) -> None:
    """验证采集动画和频率监听可分别关闭。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 采集和频率监听分别按接口调用关闭
    """
    animation_widget = BeltAnimationWidget()
    animation_widget.start_capture()
    animation_widget.set_frequency_listening(True)
    animation_widget.stop_machine()
    assert animation_widget.capturing is False
    assert animation_widget.frequency_listening is False

    animation_widget.start_capture()
    animation_widget.set_frequency_listening(True)
    animation_widget.stop_capture()
    animation_widget.set_frequency_listening(False)
    assert animation_widget.capturing is False
    assert animation_widget.frequency_listening is False
    animation_widget.animation_timer.stop()
    animation_widget.close()


@pytest.mark.parametrize(
    ("frequency", "expected_text"),
    ((None, "--"), (0.0, "0.0 Hz"), (12.5, "12.5 Hz")),
)
def test_machine_card_formats_frequency_values(
    frequency: float | None,
    expected_text: str,
) -> None:
    """验证卡片区分无频率数据、真实零频率和实际频率。

    Args:
        frequency: 待显示的频率值。
        expected_text: 期望显示的频率文字。

    Returns:
        返回示例：
            None  # 频率标签收到期望文字
    """
    frequency_label = Mock()
    card = SimpleNamespace(frequency_label=frequency_label)
    MachineCard.set_frequency(card, frequency)
    frequency_label.setText.assert_called_once_with(expected_text)


def test_machine_card_sets_and_clears_ocr_result() -> None:
    """验证卡片按行显示并清空 OCR 结果。

    Args:
        无。

    Returns:
        返回示例：
            None  # OCR 标签显示换行结果并可恢复默认文字
    """
    ocr_result_label = Mock()
    card = SimpleNamespace(ocr_result_label=ocr_result_label)
    result_lines = ("2378244 VEGA × 5EPJ1152", "2926 215C", "003")

    MachineCard.set_ocr_result(card, result_lines, ("A" * 20, "2926215C", "003"))
    ocr_result_label.setText.assert_called_once_with(
        "20  2378244 VEGA × 5EPJ1152\n8  2926 215C\n3  003\n2  --"
    )

    MachineCard.set_ocr_result(card, (), ())
    ocr_result_label.setText.assert_called_with("20  --\n8  --\n3  --\n2  --")
    MachineCard.clear_ocr_result(card)
    assert ocr_result_label.setText.call_args_list[-1].args == ("20  --\n8  --\n3  --\n2  --",)


def test_ocr_page_preserves_session_and_text_on_refresh(qt_application) -> None:
    """验证文字分类、周期隔离和刷新恢复。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        返回示例：
            None  # 新周期清空缓存，刷新保留身份且旧文字被隔离
    """
    from PySide6.QtCore import Qt
    from ui.pages.realtime_page import RealtimePage

    # 创建两台机器的真实页面控件。
    service = Mock()
    service.list_enabled_machines.return_value = [
        {"id": 1, "machine_name": "机器 1"},
        {"id": 2, "machine_name": "机器 2"},
    ]
    page = RealtimePage(service)
    ordered_lines = ("14", "2926 215C", "<b>003</b>", "2926 216C", "2926 217C", "长文字")
    normalized_lines = ("14", "2926215C", "003", "2926216C", "2926217C", "A" * 20)
    try:
        # 正式启动后交付乱序文字，核对分类和纯文本设置。
        page.update_measurement_progress("1", "first", "session_start", "success")
        page.update_ocr_result("1", "first", ordered_lines, normalized_lines)
        expected_text = "20  长文字\n8  2926 215C\n    2926 216C\n    2926 217C\n3  <b>003</b>\n2  14"
        assert page.cards_by_machine_id["1"].ocr_result_label.text() == expected_text
        assert page.cards_by_machine_id["1"].ocr_result_label.textFormat() == Qt.TextFormat.PlainText
        assert page.cards_by_machine_id["2"].ocr_result_label.text() == "20  --\n8  --\n3  --\n2  --"

        # 入库进度、连接状态和刷新均保留已完成文字。
        page.update_measurement_progress("1", "first", "evidence_storage", "success")
        page.update_connection_state("1", "已停止", "")
        page.reload_machines()
        assert page.cards_by_machine_id["1"].ocr_result_label.text() == expected_text
        assert page.cards_by_machine_id["1"].progress_session_id == "first"

        # 新周期同时清空缓存和控件，刷新不恢复上一轮文字。
        page.update_measurement_progress("1", "second", "session_start", "success")
        assert page.ocr_results_by_machine_id["1"] == ("second", (), ())
        page.reload_machines()
        assert page.cards_by_machine_id["1"].progress_session_id == "second"
        page.update_ocr_result("1", "first", ordered_lines, normalized_lines)
        page.update_measurement_progress("1", "first", "evidence_storage", "success")
        assert page.ocr_results_by_machine_id["1"] == ("second", (), ())
        assert page.cards_by_machine_id["1"].ocr_result_label.text() == "20  --\n8  --\n3  --\n2  --"

        # 第二台机器独立接收本轮结果。
        page.update_measurement_progress("2", "other", "session_start", "success")
        page.update_ocr_result("2", "other", ("12",), ("12",))
        assert page.cards_by_machine_id["2"].ocr_result_label.text().endswith("2  12")
        assert page.ocr_results_by_machine_id["1"] == ("second", (), ())
    finally:
        page.close()
        page.deleteLater()


def test_monitoring_service_delivers_text_from_background_thread(qt_application, monkeypatch) -> None:
    """验证后台监测通过 Qt 信号向页面交付周期和文字。

    Args:
        qt_application: Qt 应用实例。
        monkeypatch: 属性替换工具。

    Returns:
        返回示例：
            None  # 后台信号按顺序进入页面并显示最终文字
    """
    from pathlib import Path
    from unittest.mock import AsyncMock
    from ui.pages.realtime_page import RealtimePage
    from src.service.monitoring_service import MonitoringService

    # 创建页面和不访问设备的运行时替身。
    machine_service = Mock()
    machine_service.list_enabled_machines.return_value = [{"id": 1, "machine_name": "机器 1"}]
    page = RealtimePage(machine_service)
    runtime = SimpleNamespace(failure=None, stop=AsyncMock())

    async def start_runtime(camera_notification, progress_notification, ocr_notification) -> None:
        """从监测线程发送启动和最终文字通知。

        Args:
            camera_notification: 相机状态回调。
            progress_notification: 测量进度回调。
            ocr_notification: 最终文字回调。

        Returns:
            返回示例：
                None  # 周期身份和文字已发出
        """
        progress_notification("1", "session", "session_start", "success")
        ocr_notification("1", "session", ("003",), ("003",))

    # 替换设备启动入口并绑定真实 Qt 信号。
    runtime.start = start_runtime
    monkeypatch.setattr("src.service.monitoring_service.load_config", Mock())
    monkeypatch.setattr("src.service.monitoring_service.SystemRuntime", Mock(return_value=runtime))
    service = MonitoringService(Path("config"))
    service.stop_requested.set()
    service.measurement_progress_changed_signal.connect(page.update_measurement_progress)
    service.ocr_result_changed_signal.connect(page.update_ocr_result)
    try:
        # 等待线程结束，再由主线程处理排队信号。
        service.start()
        assert service.wait(5000)
        qt_application.processEvents()
        assert service.failure_message == ""
        assert page.ocr_results_by_machine_id["1"] == ("session", ("003",), ("003",))
        assert page.cards_by_machine_id["1"].ocr_result_label.text() == "20  --\n8  --\n3  003\n2  --"
        runtime.stop.assert_awaited_once()
    finally:
        service.wait()
        page.close()
        page.deleteLater()
