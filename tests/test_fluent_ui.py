"""验证 Fluent 窗口和实时卡片的界面交互。"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QSizePolicy
from PySide6.QtTest import QTest
from qfluentwidgets import FluentWindow, InfoBar, MaskDialogBase, MessageBox

from src.controller.controller import AppController, Result
from ui.main_window import NAVIGATION_WIDTH, MainWindow
from ui.pages.realtime_page import RealtimePage


@pytest.fixture
def qt_application() -> QApplication:
    """提供界面测试使用的 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 当前测试进程的 Qt 应用
    """
    return QApplication.instance() or QApplication([])


def make_ui_controller() -> Mock:
    """创建只提供界面展示数据的控制器替身。

    Args:
        无。

    Returns:
        Mock()  # 含机器、历史、异常和监测状态结果的替身
    """
    machines = [
        {
            "id": machine_id,
            "machine_name": f"第 {machine_id} 台机器",
            "camera_serial": f"CAM-{machine_id}",
            "frequency_meter_serial": f"FREQ-{machine_id}",
            "enabled": True,
            "remark": None,
            "created_at": "2026-09-28 08:00:00",
            "updated_at": "2026-09-28 08:00:00",
        }
        for machine_id in range(1, 6)
    ]
    controller = Mock()
    controller.list_enabled_machines.return_value = Result.ok({"machines": machines})
    controller.list_machines.return_value = Result.ok({"machines": machines})
    controller.list_record_machines.return_value = Result.ok({"machines": []})
    controller.list_measurement_records.return_value = Result.ok(
        {"records": [], "total": 0, "total_pages": 1}
    )
    controller.list_abnormal_event_machine_ids.return_value = Result.ok(
        {"machine_ids": []}
    )
    controller.list_abnormal_events.return_value = Result.ok({"events": []})
    controller.is_monitoring_running.return_value = Result.ok({"running": False})
    return controller


def test_fluent_navigation_uses_existing_pages_and_refreshes_once(
    qt_application: QApplication,
) -> None:
    """验证导航复用页面且进入列表页只查询一次。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 页面实例和查询次数均符合预期
    """
    controller = make_ui_controller()
    window = MainWindow(controller)
    try:
        window.show()
        qt_application.processEvents()
        assert isinstance(window, FluentWindow)
        assert window.stackedWidget.count() == 7
        assert not window.history_page.detail_dialog.isVisible()
        assert not window.abnormal_events_page.detail_dialog.isVisible()
        assert not window.machines_page.editor.isVisible()
        realtime_page = window.realtime_page
        history_page = window.history_page
        abnormal_page = window.abnormal_events_page
        machines_page = window.machines_page

        # 每个页面只在实际进入时触发一次数据读取。
        for page_key in (
            "history", "abnormal_events", "machines", "images", "settings", "logs"
        ):
            window.switch_page(page_key)
            qt_application.processEvents()
            assert window.current_page_key == page_key
        controller.list_record_machines.assert_called_once()
        controller.list_measurement_records.assert_called_once()
        controller.list_abnormal_event_machine_ids.assert_called_once()
        controller.list_abnormal_events.assert_called_once()

        # 回到同一页面不会重建页面或重复刷新。
        window.switch_page("realtime")
        window.switch_page("realtime")
        qt_application.processEvents()
        assert window.realtime_page is realtime_page
        assert window.history_page is history_page
        assert window.abnormal_events_page is abnormal_page
        assert window.machines_page is machines_page
    finally:
        window.close()
        window.deleteLater()


def test_fixed_navigation_and_title_bar_stay_aligned(
    qt_application: QApplication,
) -> None:
    """验证固定导航在两种窗口尺寸下完整显示且不覆盖标题栏。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 导航、品牌、分组与标题栏保持预期布局
    """
    window = MainWindow(make_ui_controller())
    try:
        window.show()
        QTest.qWait(200)
        qt_application.processEvents()
        panel = window.navigationInterface.panel

        # 核对品牌、分组及上下导航项的顺序。
        top_items = [
            panel.topLayout.itemAt(index).widget()
            for index in range(panel.topLayout.count())
            if panel.topLayout.itemAt(index).widget().text()
        ]
        bottom_items = [
            panel.bottomLayout.itemAt(index).widget()
            for index in range(panel.bottomLayout.count())
        ]
        assert [item.text() for item in top_items] == [
            "BeltVision", "工作台", "实时监测", "历史记录", "异常事件", "机器管理"
        ]
        assert [item.text() for item in bottom_items] == [
            "图片管理", "系统配置", "日志查看"
        ]
        assert top_items[0].testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        assert not top_items[0].isSelectable
        assert top_items[1].height() > 0
        assert top_items[1].lightTextColor.name() == "#667085"
        assert not panel.menuButton.isVisible()
        assert not panel.returnButton.isVisible()
        assert "#F7F8FA" in panel.styleSheet()

        # 核对导航宽度及标题栏在缩放前后的边界。
        for width, height in ((1600, 900), (1280, 720)):
            window.resize(width, height)
            qt_application.processEvents()
            assert panel.displayMode.name == "EXPAND"
            assert window.navigationInterface.width() == NAVIGATION_WIDTH
            assert window.titleBar.x() == NAVIGATION_WIDTH
            assert window.titleBar.width() == width - NAVIGATION_WIDTH
            assert not window.titleBar.iconLabel.isVisible()
            assert not window.titleBar.titleLabel.isVisible()
            assert window.status_area.isVisible()
            assert window.clock_label.text()
            assert window.status_text.text() == "系统运行正常"
            assert window.connection_text.text() == "服务已连接"
            assert all(
                button.isVisible()
                for button in (
                    window.titleBar.minBtn,
                    window.titleBar.maxBtn,
                    window.titleBar.closeBtn,
                )
            )
        assert window.windowTitle() == "BeltVision | 实时监测"
    finally:
        window.close()
        window.deleteLater()


def test_realtime_resize_reflows_existing_cards_without_query(
    qt_application: QApplication,
) -> None:
    """验证窗口宽度变化只移动已有卡片。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 列数变化且机器查询与卡片实例保持不变
    """
    controller = make_ui_controller()
    window = MainWindow(controller)
    try:
        window.resize(1600, 900)
        window.show()
        qt_application.processEvents()
        cards = tuple(window.realtime_page.machine_cards)
        query_count = controller.list_enabled_machines.call_count
        assert window.navigationInterface.panel.displayMode.name == "EXPAND"
        assert window.realtime_page.card_column_count == 3
        assert all(280 <= card.width() <= 330 for card in cards)
        detail_panel = window.realtime_page.detail_panel
        assert detail_panel.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum
        assert detail_panel.height() < window.realtime_page.scroll_area.height()
        scroll_area = window.realtime_page.scroll_area
        assert detail_panel.mapTo(window, QPoint(0, 0)).y() == (
            scroll_area.mapTo(window, QPoint(0, 0)).y()
        )

        window.resize(1280, 720)
        QTest.qWait(300)
        qt_application.processEvents()
        assert window.size().width() == 1280
        assert window.size().height() == 720
        assert window.navigationInterface.panel.displayMode.name == "EXPAND"
        assert window.realtime_page.card_column_count == 2
        assert all(280 <= card.width() <= 330 for card in cards)
        assert window.realtime_page.scroll_area.horizontalScrollBar().maximum() == 0
        assert tuple(window.realtime_page.machine_cards) == cards
        assert controller.list_enabled_machines.call_count == query_count
        card_at_last_row = window.realtime_page.cards_layout.itemAtPosition(2, 0)
        assert card_at_last_row.widget() is cards[4]

        # 机器名称保持可读宽度，窄表格使用内部水平滚动。
        window.switch_page("machines")
        qt_application.processEvents()
        assert window.machines_page.table.columnWidth(1) >= 220
        assert window.machines_page.table.horizontalScrollBar().maximum() > 0
    finally:
        window.close()
        window.deleteLater()


def test_machine_detail_keeps_long_ocr_text_selectable(qt_application) -> None:
    """验证详情保留全文且大量 OCR 不撑高摘要卡。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 全文可复制，摘要高度固定
    """
    page = RealtimePage(make_ui_controller())
    try:
        page.show()
        qt_application.processEvents()
        card = page.machine_cards[0]
        ordered_lines = tuple(f"{number:020d}" for number in range(40))
        page.update_measurement_progress("1", "first", "session_start", "success")
        qt_application.processEvents()
        initial_height = card.sizeHint().height()
        page.update_ocr_result("1", "first", ordered_lines, ordered_lines)
        qt_application.processEvents()

        # 核对全文、只读属性和剪贴板复制。
        editor = page.detail_panel.ocr_text
        assert editor.parentWidget().objectName() == "detailOcrPanel"
        assert editor.frameShape() == QFrame.Shape.NoFrame
        assert editor.height() == 110
        assert all(line in editor.toPlainText() for line in ordered_lines)
        assert ordered_lines[-1] in editor.toPlainText()
        assert editor.isReadOnly()
        editor.selectAll()
        editor.copy()
        assert QApplication.clipboard().text() == editor.toPlainText()
        assert card.ocr_result_label.text() == ordered_lines[0]
        assert card.sizeHint().height() == initial_height
    finally:
        page.close()
        page.deleteLater()


def test_machine_detail_sections_follow_selected_machine(qt_application) -> None:
    """验证频率、OCR、步骤和占位统计随详情卡完整显示。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 分区结构和现有状态信号保持可用
    """
    page = RealtimePage(make_ui_controller())
    try:
        page.show()
        qt_application.processEvents()
        panel = page.detail_panel

        # 核对详情分区与三项固定占位统计。
        assert panel.findChild(QFrame, "detailFrequencyCard") is not None
        assert panel.findChild(QFrame, "detailOcrCard") is not None
        assert panel.findChild(QFrame, "detailStatsBar") is not None
        assert [label.text() for label in panel.stat_values.values()] == [
            "--", "--", "--"
        ]
        assert panel.frequency_label.text() == "--"

        # 连接和本轮进度继续刷新当前机器的详情。
        page.update_connection_state("1", "相机已连接", "")
        assert panel.camera_state_label.text() == "相机已连接"
        page.update_measurement_progress("1", "first", "session_start", "success")
        assert "#138B3F" in panel.state_label.styleSheet()
        assert "#2563EB" in panel.steps.dots[0].styleSheet()
        page.select_machine("2")
        assert panel.camera_serial_label.text() == "CAM-2"
        assert [label.text() for label in panel.stat_values.values()] == [
            "--", "--", "--"
        ]
    finally:
        page.close()
        page.deleteLater()


def test_dashboard_selection_reload_and_summary(qt_application) -> None:
    """验证机器选择、重载保持和总览状态更新。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 选择、详情和统计随状态同步
    """
    controller = make_ui_controller()
    page = RealtimePage(controller)
    try:
        page.show()
        qt_application.processEvents()
        assert page.selected_machine_id == "1"
        QTest.mouseClick(page.machine_cards[1], Qt.MouseButton.LeftButton)
        assert page.selected_machine_id == "2"
        assert sum(card.property("selected") for card in page.machine_cards) == 1
        assert page.detail_panel.title.text() == "第 2 台机器"
        assert page.detail_panel.camera_serial_label.text() == "CAM-2"
        assert page.detail_panel.frequency_meter_serial_label.text() == "FREQ-2"
        attributes_layout = page.detail_panel.attributes_layout
        assert isinstance(attributes_layout, QGridLayout)
        camera_widget = attributes_layout.itemAtPosition(1, 0).widget()
        assert camera_widget is page.detail_panel.camera_serial_label
        meter_widget = attributes_layout.itemAtPosition(1, 1).widget()
        assert meter_widget is page.detail_panel.frequency_meter_serial_label
        state_widget = attributes_layout.itemAtPosition(4, 0).widget()
        assert state_widget is page.detail_panel.state_label
        camera_state_widget = attributes_layout.itemAtPosition(4, 1).widget()
        assert camera_state_widget is page.detail_panel.camera_state_label
        assert page.detail_panel.frequency_label.text() == "--"
        assert set(page.detail_panel.stat_values) == {
            "运行时长", "今日识别数量", "今日待复核数量"
        }
        assert all(
            label.text() == "--" for label in page.detail_panel.stat_values.values()
        )
        assert not controller.list_measurement_records.called
        page.reload_machines()
        assert page.selected_machine_id == "2"

        # 连接与测量故障按机器编号合并统计。
        query_count = controller.list_enabled_machines.call_count
        page.update_connection_state("1", "相机已连接", "")
        page.update_measurement_progress("2", "first", "session_start", "success")
        page.update_connection_state("2", "相机故障", "故障")
        page.update_measurement_progress("2", "first", "image_capture", "failed")
        assert [card.value_label.text() for card in page.summary_cards] == [
            "5", "1", "1", "1",
        ]
        assert page.detail_panel.badge.text() == "相机故障"
        assert page.detail_panel.camera_state_label.text() == "相机故障"
        page.update_cycle_closed("2", "first")
        assert page.summary_cards[2].value_label.text() == "0"
        page.finish_monitoring("")
        assert page.summary_cards[1].value_label.text() == "0"
        assert controller.list_enabled_machines.call_count == query_count

        # 删除选中机器后退回首台，空列表清空详情。
        remaining_machines = page.machines[:1]
        machine_data = controller.list_enabled_machines.return_value.data
        machine_data["machines"] = remaining_machines
        page.reload_machines()
        assert page.selected_machine_id == "1"
        controller.list_enabled_machines.return_value.data["machines"] = []
        page.reload_machines()
        assert page.selected_machine_id is None
        assert page.detail_panel.title.text() == "未选择机器"
    finally:
        page.close()
        page.deleteLater()



def test_machine_editor_and_delete_confirmation_use_mask_and_cancel(
    qt_application: QApplication,
    monkeypatch,
) -> None:
    """验证机器表单遮罩与删除确认的默认取消操作。

    Args:
        qt_application: 测试使用的 Qt 应用。
        monkeypatch: pytest 对象替换工具。

    Returns:
        None  # 表单遮罩不可点穿且取消确认不删除机器
    """
    controller = make_ui_controller()
    window = MainWindow(controller)
    try:
        window.show()
        window.switch_page("machines")
        qt_application.processEvents()
        assert isinstance(window.machines_page.editor, MaskDialogBase)
        assert not window.machines_page.editor.isClosableOnMaskClicked()

        # 检查取消按钮默认选中并模拟取消结果。
        def cancel_confirmation(message_box: MessageBox) -> int:
            """检查确认框后返回取消结果。

            Args:
                message_box: 删除确认框。

            Returns:
                0  # 用户取消删除
            """
            message_box.show()
            message_box.window().activateWindow()
            QTest.qWait(50)
            qt_application.processEvents()
            assert message_box.cancelButton.isDefault()
            assert not message_box.yesButton.isDefault()
            assert message_box.cancelButton.hasFocus()
            message_box.close()
            return 0

        monkeypatch.setattr(MessageBox, "exec", cancel_confirmation)
        window.machines_page.delete_machine(1)
        controller.delete_machine.assert_not_called()

        # 明确确认后才调用删除接口。
        controller.delete_machine.return_value = Result.ok()
        monkeypatch.setattr(MessageBox, "exec", lambda message_box: 1)
        window.machines_page.delete_machine(1)
        controller.delete_machine.assert_called_once_with(1)
    finally:
        window.close()
        window.deleteLater()


def test_machine_editor_preserves_validation_and_save_order(
    qt_application: QApplication,
    monkeypatch,
) -> None:
    """验证机器表单失败时保留输入并在成功后关闭。

    Args:
        qt_application: 测试使用的 Qt 应用。
        monkeypatch: pytest 对象替换工具。

    Returns:
        None  # 表单校验、字段定位和成功关闭顺序均正确
    """
    controller = make_ui_controller()
    controller.create_machine.return_value = Result.error(
        "相机序列号已存在。", data={"field": "camera_serial"}
    )
    messages = Mock()
    monkeypatch.setattr(InfoBar, "error", messages)
    window = MainWindow(controller)
    try:
        window.show()
        window.switch_page("machines")
        page = window.machines_page
        page.create_machine()
        qt_application.processEvents()

        # 空白表单先提示必填项，不提交保存请求。
        page.save_button.click()
        controller.create_machine.assert_not_called()
        assert page.editor.isVisible()
        assert messages.call_args.args[1] == "请填写机器名称。"
        assert messages.call_args.kwargs["parent"] is page.editor.widget

        # 去空白后提交字段，并将重复错误定位到相机序列号。
        page.field_inputs["machine_name"].setText("  新机器  ")
        page.field_inputs["camera_serial"].setText("  CAM-NEW  ")
        page.field_inputs["frequency_meter_serial"].setText("  FREQ-NEW  ")
        page.remark_input.setPlainText("  ")
        page.save_button.click()
        controller.create_machine.assert_called_once_with(
            machine_name="新机器",
            camera_serial="CAM-NEW",
            frequency_meter_serial="FREQ-NEW",
            enabled=True,
            remark=None,
        )
        assert page.editor.isVisible()
        assert page.field_inputs["camera_serial"].hasFocus()

        # 保存成功后关闭表单，再读取并选中列表记录。
        controller.create_machine.return_value = Result.ok({"machine_id": 1})
        page.save_button.click()
        QTest.qWait(150)
        assert not page.editor.isVisible()
        assert not page.save_button.isEnabled()
        assert page.table.selectedItems()[0].text() == "1"
        page.create_machine()
        assert page.save_button.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_fluent_window_waits_for_monitoring_before_close(
    qt_application: QApplication,
) -> None:
    """验证重复关闭仅请求停止一次，监测结束后窗口关闭。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 监测运行时关闭被拦截，结束信号发出后窗口关闭
    """
    machine_service = Mock()
    machine_service.list_enabled_machines.return_value = {"machines": []}
    machine_service.list_machines.return_value = {"machines": []}
    controller = AppController(machine_service, Mock(), Mock(), Path("config"))
    runtime_thread = Mock()
    controller.runtime_thread = runtime_thread
    controller.stop_monitoring = Mock(wraps=controller.stop_monitoring)
    window = MainWindow(controller)
    try:
        # 运行中的首次关闭等待监测清理。
        window.show()
        qt_application.processEvents()
        assert window.isVisible()
        window.close()
        qt_application.processEvents()
        assert window.isVisible()
        assert window.realtime_page.closing_requested
        controller.stop_monitoring.assert_called_once_with()
        runtime_thread.stop_requested.set.assert_called_once_with()

        # 再次关闭仍等待结束，且不重复提交停止请求。
        window.close()
        qt_application.processEvents()
        assert window.isVisible()
        controller.stop_monitoring.assert_called_once_with()
        runtime_thread.stop_requested.set.assert_called_once_with()

        # 模拟 Controller 清理线程后发出结束信号。
        controller.runtime_thread = None
        assert not controller.is_monitoring_running().data["running"]
        controller.monitoring_finished_signal.emit("")
        qt_application.processEvents()
        assert not window.isVisible()
    finally:
        controller.runtime_thread = None
        window.close()
        window.deleteLater()
