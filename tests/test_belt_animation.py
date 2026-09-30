"""验证皮带机动画控制接口和机器卡片频率显示。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.controller.controller import AppController
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


@pytest.mark.parametrize("widget_size", ((248, 125), (280, 125), (480, 175)))
def test_mechanical_scene_renders_at_card_sizes(
    qt_application: QApplication,
    widget_size: tuple[int, int],
) -> None:
    """验证小卡片和宽卡片的机械场景及独立子动画可以绘制。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。
        widget_size: 动画区域的宽度和高度。

    Returns:
        返回示例：
            None  # 展开、运行、采集、监听和收缩画面均可绘制
    """
    animation_widget = BeltAnimationWidget()
    try:
        # 按机器卡片可用尺寸绘制停止状态。
        animation_widget.resize(*widget_size)
        animation_widget.animation_timer.stop()
        stopped_image = animation_widget.grab().toImage()
        assert not stopped_image.isNull()
        assert animation_widget.scene_renderer.isValid()
        assert stopped_image.size() == animation_widget.size()

        # 单独开启子动画也更新画面，不启动皮带。
        animation_widget.start_capture()
        capture_image = animation_widget.grab().toImage()
        assert capture_image != stopped_image
        animation_widget.stop_capture()
        animation_widget.set_frequency_listening(True)
        listening_image = animation_widget.grab().toImage()
        assert listening_image != stopped_image
        assert animation_widget._machine_state.name == "STOPPED"

        # 展开后推进滚筒位移并检查运行画面。
        animation_widget.start_machine()
        animation_widget.extension_animation.setCurrentTime(325)
        assert not animation_widget.grab().isNull()
        animation_widget.extension_animation.setCurrentTime(650)
        animation_widget.start_capture()
        running_image = animation_widget.grab().toImage()
        for frame_index in range(35):
            animation_widget._advance_animation()
        assert animation_widget.belt_travel > 80
        assert animation_widget.grab().toImage() != running_image
        assert animation_widget._machine_state.name == "RUNNING"

        # 关闭后收缩到停止状态，仍保持有效画面。
        animation_widget.stop_machine()
        animation_widget.extension_animation.setCurrentTime(325)
        assert not animation_widget.grab().isNull()
        animation_widget.extension_animation.setCurrentTime(650)
        assert animation_widget._machine_state.name == "STOPPED"
        assert animation_widget.extension == 0.0
        assert not animation_widget.grab().isNull()
        assert animation_widget.scene_renderer.isValid()
    finally:
        animation_widget.animation_timer.stop()
        animation_widget.extension_animation.stop()
        animation_widget.close()
        animation_widget.deleteLater()


def test_machine_restarts_during_retraction(qt_application: QApplication) -> None:
    """验证上一轮收缩尚未完成时新周期可以重新展开。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 新启动从当前展开比例进入 STARTING 并最终运行
    """
    animation_widget = BeltAnimationWidget()
    try:
        # 完成启动后推进到收缩中途。
        animation_widget.start_machine()
        animation_widget.extension_animation.setCurrentTime(650)
        animation_widget.stop_machine()
        animation_widget.extension_animation.setCurrentTime(325)
        retracted_extension = animation_widget.extension
        assert animation_widget._machine_state.name == "STOPPING"
        assert 0.0 < retracted_extension < 1.0

        # 新启动中断收缩，从当前比例展开到运行状态。
        animation_widget.start_machine()
        assert animation_widget._machine_state.name == "STARTING"
        assert animation_widget.extension_animation.startValue() == retracted_extension
        animation_widget.extension_animation.setCurrentTime(650)
        assert animation_widget._machine_state.name == "RUNNING"
        assert animation_widget.extension == 1.0
    finally:
        animation_widget.animation_timer.stop()
        animation_widget.extension_animation.stop()
        animation_widget.close()
        animation_widget.deleteLater()


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
        "2378244 VEGA × 5EPJ1152"
    )

    MachineCard.set_ocr_result(card, (), ())
    ocr_result_label.setText.assert_called_with("--")
    MachineCard.clear_ocr_result(card)
    assert ocr_result_label.setText.call_args_list[-1].args == ("--",)


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
    service.list_enabled_machines.return_value = {
        "machines": [
            {
                "id": 1,
                "machine_name": "机器 1",
                "camera_serial": "CAM-1",
                "frequency_meter_serial": "FREQ-1",
            },
            {
                "id": 2,
                "machine_name": "机器 2",
                "camera_serial": "CAM-2",
                "frequency_meter_serial": "FREQ-2",
            },
        ],
    }
    page = RealtimePage(AppController(service, Mock(), Mock(), Path("config")))
    ordered_lines = ("14", "2926 215C", "<b>003</b>", "2926 216C", "2926 217C", "长文字")
    normalized_lines = ("14", "2926215C", "003", "2926216C", "2926217C", "A" * 20)
    try:
        # 正式启动后交付乱序文字，核对分类和纯文本设置。
        page.update_measurement_progress("1", "first", "session_start", "success")
        page.update_ocr_result("1", "first", ordered_lines, normalized_lines)
        expected_text = (
            "20  长文字\n8  2926 215C\n    2926 216C\n    2926 217C"
            "\n3  <b>003</b>\n2  14"
        )
        assert page.detail_panel.ocr_text.toPlainText() == expected_text
        result_label = page.cards_by_machine_id["1"].ocr_result_label
        assert result_label.textFormat() == Qt.TextFormat.PlainText
        assert page.cards_by_machine_id["2"].ocr_result_label.text() == "--"

        # 入库进度、连接状态和刷新均保留已完成文字。
        page.update_measurement_progress("1", "first", "evidence_storage", "success")
        page.update_connection_state("1", "已停止", "")
        page.reload_machines()
        assert page.detail_panel.ocr_text.toPlainText() == expected_text
        assert page.cards_by_machine_id["1"].progress_session_id == "first"

        # 新周期同时清空缓存和控件，刷新不恢复上一轮文字。
        page.update_measurement_progress("1", "second", "session_start", "success")
        assert page.ocr_results_by_machine_id["1"] == ("second", (), ())
        page.reload_machines()
        assert page.cards_by_machine_id["1"].progress_session_id == "second"
        page.update_ocr_result("1", "first", ordered_lines, normalized_lines)
        page.update_measurement_progress("1", "first", "evidence_storage", "success")
        assert page.ocr_results_by_machine_id["1"] == ("second", (), ())
        assert page.cards_by_machine_id["1"].ocr_result_label.text() == "--"

        # 第二台机器独立接收本轮结果。
        page.update_measurement_progress("2", "other", "session_start", "success")
        page.update_ocr_result("2", "other", ("12",), ("12",))
        assert page.cards_by_machine_id["2"].ocr_result_label.text() == "12"
        assert page.ocr_results_by_machine_id["1"] == ("second", (), ())
    finally:
        page.close()
        page.deleteLater()


def test_page_animates_only_current_machine_and_session(
    qt_application: QApplication,
) -> None:
    """验证进度和关闭通知只控制对应机器的当前周期。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 失败保留皮带，关闭收起皮带，旧周期通知被忽略
    """
    from ui.pages.realtime_page import RealtimePage

    # 创建两台机器的页面并受理第一台机器的周期。
    service = Mock()
    service.list_enabled_machines.return_value = {
        "machines": [
            {
                "id": 1,
                "machine_name": "机器 1",
                "camera_serial": "CAM-1",
                "frequency_meter_serial": "FREQ-1",
            },
            {
                "id": 2,
                "machine_name": "机器 2",
                "camera_serial": "CAM-2",
                "frequency_meter_serial": "FREQ-2",
            },
        ],
    }
    page = RealtimePage(AppController(service, Mock(), Mock(), Path("config")))
    try:
        page.update_measurement_progress("1", "first", "session_start", "success")
        first_animation = page.cards_by_machine_id["1"].belt_animation
        second_animation = page.cards_by_machine_id["2"].belt_animation
        assert first_animation._machine_state.name == "STARTING"
        assert second_animation._machine_state.name == "STOPPED"

        # 启动子动画后，业务失败只关闭对应子动画。
        page.update_measurement_progress("1", "first", "image_capture", "running")
        page.update_measurement_progress(
            "1", "first", "frequency_collection", "running"
        )
        assert first_animation.capturing
        assert first_animation.frequency_listening
        page.update_measurement_progress(
            "1", "first", "character_recognition", "failed"
        )
        assert first_animation._machine_state.name == "STARTING"
        assert not first_animation.capturing
        assert not first_animation.frequency_listening
        page.update_measurement_progress("1", "first", "image_capture", "failed")
        page.update_measurement_progress("1", "first", "frequency_collection", "failed")
        assert not first_animation.capturing
        assert not first_animation.frequency_listening
        assert first_animation._machine_state.name == "STARTING"

        # 当前周期关闭后保留文字，旧周期消息不影响下一轮。
        page.update_ocr_result("1", "first", ("003",), ("003",))
        page.update_cycle_closed("1", "first")
        assert first_animation._machine_state.name == "STOPPING"
        first_card = page.cards_by_machine_id["1"]
        assert first_card.ocr_result_label.text() == "003"
        page.update_measurement_progress("1", "second", "session_start", "success")

        # 成功状态分别结束本轮的扫描和频率波形。
        page.update_measurement_progress("1", "second", "image_capture", "running")
        page.update_measurement_progress(
            "1", "second", "frequency_collection", "running"
        )
        page.update_measurement_progress("1", "second", "image_capture", "success")
        page.update_measurement_progress(
            "1", "second", "frequency_collection", "success"
        )
        assert not first_animation.capturing
        assert not first_animation.frequency_listening

        # 旧周期和另一台机器的消息都不影响当前皮带。
        page.update_cycle_closed("1", "first")
        page.update_measurement_progress("1", "first", "image_capture", "running")
        assert first_animation._machine_state.name == "STARTING"
        assert not first_animation.capturing
        page.update_measurement_progress("2", "other", "session_start", "success")
        page.update_cycle_closed("2", "other")
        assert second_animation._machine_state.name == "STOPPING"
        assert first_animation._machine_state.name == "STARTING"
        assert first_card.ocr_result_label.text() == "--"
    finally:
        page.close()
        page.deleteLater()


@pytest.mark.parametrize(
    ("stage", "animation_attribute"),
    (
        ("image_capture", "capturing"),
        ("frequency_collection", "frequency_listening"),
    ),
)
def test_failed_subprocess_stops_its_animation_without_closing_belt(
    qt_application: QApplication,
    stage: str,
    animation_attribute: str,
) -> None:
    """验证采集或频率失败会停止子动画但保留本轮皮带。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。
        stage: 失败的处理阶段。
        animation_attribute: 对应子动画的状态属性。

    Returns:
        返回示例：
            None  # 对应子动画停止，周期关闭前皮带仍处于启动状态
    """
    from ui.pages.realtime_page import RealtimePage

    # 创建单台机器并启动对应子动画。
    service = Mock()
    service.list_enabled_machines.return_value = {
        "machines": [
            {
                "id": 1,
                "machine_name": "机器 1",
                "camera_serial": "CAM-1",
                "frequency_meter_serial": "FREQ-1",
            },
        ],
    }
    page = RealtimePage(AppController(service, Mock(), Mock(), Path("config")))
    try:
        page.update_measurement_progress("1", "session", "session_start", "success")
        page.update_measurement_progress("1", "session", stage, "running")
        animation = page.cards_by_machine_id["1"].belt_animation
        assert getattr(animation, animation_attribute)

        # 失败时结束子动画，但等待正式关闭通知收起皮带。
        page.update_measurement_progress("1", "session", stage, "failed")
        assert not getattr(animation, animation_attribute)
        assert animation._machine_state.name == "STARTING"
    finally:
        page.close()
        page.deleteLater()


def test_page_refresh_and_monitoring_stop_restore_safe_animation(
    qt_application: QApplication,
) -> None:
    """验证页面刷新只恢复运行周期，服务结束后全部动画停止。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 运行状态可恢复，已关闭周期及服务停止后不恢复子动画
    """
    from ui.pages.realtime_page import RealtimePage

    # 创建页面并让两台机器进入不同采集状态。
    service = Mock()
    service.list_enabled_machines.return_value = {
        "machines": [
            {
                "id": 1,
                "machine_name": "机器 1",
                "camera_serial": "CAM-1",
                "frequency_meter_serial": "FREQ-1",
            },
            {
                "id": 2,
                "machine_name": "机器 2",
                "camera_serial": "CAM-2",
                "frequency_meter_serial": "FREQ-2",
            },
        ],
    }
    page = RealtimePage(AppController(service, Mock(), Mock(), Path("config")))
    try:
        for machine_id in ("1", "2"):
            page.update_measurement_progress(
                machine_id, "session", "session_start", "success"
            )
        page.update_measurement_progress("1", "session", "image_capture", "running")
        page.update_measurement_progress(
            "1", "session", "frequency_collection", "running"
        )
        page.update_measurement_progress("2", "session", "image_capture", "running")
        page.update_cycle_closed("2", "session")

        # 重建卡片后只恢复第一台机器的运行和子动画。
        page.reload_machines()
        first_animation = page.cards_by_machine_id["1"].belt_animation
        second_animation = page.cards_by_machine_id["2"].belt_animation
        assert first_animation._machine_state.name == "STARTING"
        assert first_animation.capturing
        assert first_animation.frequency_listening
        assert second_animation._machine_state.name == "STOPPED"
        assert not second_animation.capturing
        assert not second_animation.frequency_listening
        second_card = page.cards_by_machine_id["2"]
        assert second_card.progress_statuses["image_capture"] == "running"

        # 失败时只结束子动画，刷新后也不重新启动。
        page.update_measurement_progress(
            "1", "session", "character_recognition", "failed"
        )
        assert first_animation._machine_state.name == "STARTING"
        assert not first_animation.capturing
        assert not first_animation.frequency_listening
        page.reload_machines()
        first_animation = page.cards_by_machine_id["1"].belt_animation
        assert first_animation._machine_state.name == "STARTING"
        assert not first_animation.capturing
        assert not first_animation.frequency_listening

        # 服务结束后停止动画，刷新也不再恢复运行状态。
        page.finish_monitoring("")
        assert first_animation._machine_state.name == "STOPPING"
        assert not first_animation.capturing
        assert not first_animation.frequency_listening
        page.reload_machines()
        restored_animation = page.cards_by_machine_id["1"].belt_animation
        assert restored_animation._machine_state.name == "STOPPED"
    finally:
        page.close()
        page.deleteLater()


def test_runtime_thread_delivers_text_from_background_thread(
    qt_application, monkeypatch
) -> None:
    """验证后台监测通过 Qt 信号向页面交付周期和文字。

    Args:
        qt_application: Qt 应用实例。
        monkeypatch: 属性替换工具。

    Returns:
        返回示例：
            None  # 后台信号按顺序进入页面并显示最终文字
    """
    from unittest.mock import AsyncMock
    from ui.pages.realtime_page import RealtimePage
    from src.runtime.system_runtime_thread import SystemRuntimeThread

    # 创建页面和不访问设备的运行时替身。
    machine_service = Mock()
    machine_service.list_enabled_machines.return_value = {
        "machines": [
            {
                "id": 1,
                "machine_name": "机器 1",
                "camera_serial": "CAM-1",
                "frequency_meter_serial": "FREQ-1",
            },
        ],
    }
    controller = AppController(machine_service, Mock(), Mock(), Path("config"))
    page = RealtimePage(controller)
    runtime = SimpleNamespace(failure=None, stop=AsyncMock())

    async def start_runtime(
        camera_notification,
        progress_notification,
        ocr_notification,
        cycle_closed_notification,
    ) -> None:
        """从监测线程发送启动和最终文字通知。

        Args:
            camera_notification: 相机状态回调。
            progress_notification: 测量进度回调。
            ocr_notification: 最终文字回调。
            cycle_closed_notification: 周期关闭回调。

        Returns:
            返回示例：
                None  # 周期身份和文字已发出
        """
        progress_notification("1", "session", "session_start", "success")
        ocr_notification("1", "session", ("003",), ("003",))
        cycle_closed_notification("1", "session")

    # 替换设备启动入口并绑定真实 Qt 信号。
    runtime.start = start_runtime
    monkeypatch.setattr("src.runtime.system_runtime_thread.load_config", Mock())
    monkeypatch.setattr(
        "src.runtime.system_runtime_thread.SystemRuntime", Mock(return_value=runtime)
    )
    runtime_thread = SystemRuntimeThread(Path("config"))
    runtime_thread.stop_requested.set()
    monkeypatch.setattr(
        "src.controller.controller.SystemRuntimeThread",
        Mock(return_value=runtime_thread),
    )
    try:
        # 等待线程结束，再由主线程处理排队信号。
        assert controller.start_monitoring().success
        assert runtime_thread.wait(5000)
        qt_application.processEvents()
        assert runtime_thread.failure_message == ""
        assert controller.runtime_thread is None
        assert page.ocr_results_by_machine_id["1"] == ("session", ("003",), ("003",))
        assert page.cards_by_machine_id["1"].ocr_result_label.text() == "003"
        assert not page.measurement_states_by_machine_id["1"]["machine_running"]
        runtime.stop.assert_awaited_once()
    finally:
        runtime_thread.wait()
        page.close()
        page.deleteLater()
