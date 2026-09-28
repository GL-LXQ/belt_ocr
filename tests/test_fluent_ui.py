"""验证 Fluent 窗口和实时卡片的界面交互。"""

from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from qfluentwidgets import FluentWindow, InfoBar, MaskDialogBase, MessageBox

from src.controller.controller import Result
from ui.main_window import MainWindow
from ui.pages.realtime_page import MachineCard


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
        assert window.realtime_page.card_column_count == 3

        window.resize(1280, 720)
        window.navigationInterface.expand()
        QTest.qWait(300)
        qt_application.processEvents()
        assert window.realtime_page.card_column_count == 2
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


def test_machine_card_keeps_long_ocr_text_selectable(
    qt_application: QApplication,
) -> None:
    """验证长文字在卡片中完整显示并可复制。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # OCR 全文保留在可选择的纯文本标签中
    """
    card = MachineCard(
        {
            "title": "长机器名称" * 8,
            "tone": "idle",
            "status": "未启动",
            "state": "未启动监测",
            "frequency": "--",
        }
    )
    try:
        card.resize(360, card.sizeHint().height())
        card.show()
        qt_application.processEvents()
        initial_height = card.sizeHint().height()
        ordered_lines = tuple(f"{number:020d}" for number in range(40))
        card.set_ocr_result(ordered_lines, ordered_lines)
        qt_application.processEvents()
        assert ordered_lines[-1] in card.ocr_result_label.text()
        assert card.ocr_result_label.text().count("\n") >= 42
        assert card.ocr_result_label.textInteractionFlags()
        assert card.sizeHint().height() > initial_height
    finally:
        card.close()
        card.deleteLater()


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
