"""桌面主窗口的导航、状态和窗口行为测试。"""

import os

# 默认使用离屏平台运行界面测试。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from ui.main_window import MainWindow, PAGES
from ui.demo_data import LOG_ROWS


@pytest.fixture(scope="module")
def application():
    """提供测试共用的界面应用。

    Args:
        无。

    Returns:
        返回示例：
            QApplication()  # 测试使用的应用实例
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(application):
    """创建窗口并在测试后释放。

    Args:
        application: 界面应用实例。

    Yields:
        MainWindow()  # 测试期间可操作的主窗口

    Returns:
        返回示例：
            None  # 测试结束后释放窗口，生成器不返回额外结果
    """
    # 显示测试窗口并处理初始化事件。
    main_window = MainWindow()
    main_window.show()
    application.processEvents()
    yield main_window

    # 关闭窗口并移除全局事件过滤器。
    application.removeEventFilter(main_window)
    main_window.close()
    main_window.deleteLater()
    application.processEvents()


def test_navigation_reuses_pages_and_updates_titles(window, application):
    """验证六个入口同步切换且不重新创建页面。

    Args:
        window: 主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 导航与页面实例断言通过
    """
    # 保存初始页面实例并检查默认入口。
    pages = [window.page_stack.widget(index) for index in range(6)]
    assert window.current_page_key == "realtime"

    # 逐一点击导航按钮并检查页面文案和选中状态。
    for page_key, (title, description) in PAGES.items():
        QTest.mouseClick(window.nav_buttons[page_key], Qt.MouseButton.LeftButton)
        application.processEvents()
        page = window.page_stack.currentWidget()
        assert page.objectName() == page_key
        assert page.findChild(QLabel, "pageTitle").text() == title
        assert page.findChild(QLabel, "pageSubtitle").text() == description
        assert window.title_bar.caption.text() == f"| {title}"
        assert sum(button.isChecked() for button in window.nav_buttons.values()) == 1
        assert window.current_page_key == page_key

    # 确认导航切换后仍复用原有页面实例。
    assert pages == [window.page_stack.widget(index) for index in range(6)]


def test_status_and_settings_entry(window):
    """验证业务状态接口、时钟和设置快捷入口。

    Args:
        window: 主窗口。

    Returns:
        返回示例：
            None  # 状态与设置入口断言通过
    """
    # 验证三种系统状态的文字和圆点属性。
    for status in ("normal", "warning", "error"):
        window.set_system_status("测试状态", status)
        assert window.title_bar.status_text.text() == "测试状态"
        assert window.title_bar.status_dot.property("status") == status

    # 验证断开和恢复连接后的文字及圆点状态。
    window.set_connection_status(False)
    assert window.connection_text.text() == "服务未连接"
    assert window.connection_dot.property("status") == "disconnected"
    window.set_connection_status(True)
    assert window.connection_text.text() == "服务已连接"
    assert window.connection_dot.property("status") == "normal"

    # 检查时钟已启动，并验证设置快捷入口。
    assert window.clock_timer.isActive()
    assert len(window.title_bar.clock_label.text()) == 19
    QTest.mouseClick(window.title_bar.control_buttons["settings"], Qt.MouseButton.LeftButton)
    assert window.current_page_key == "settings"


def test_resize_and_window_controls(window, application):
    """验证固定区域、内容伸缩和窗口控制。

    Args:
        window: 主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 几何尺寸与窗口状态断言通过
    """
    # 在最小尺寸和默认尺寸下检查固定区域及页面伸缩。
    for width, height in ((1280, 720), (1600, 900)):
        window.resize(width, height)
        application.processEvents()
        assert window.title_bar.height() == 48
        assert window.sidebar.width() == 168
        assert window.page_stack.width() == width - 168
        assert window.page_stack.height() == height - 48

    # 验证按钮和标题栏双击同步窗口状态。
    maximize = window.title_bar.control_buttons["maximize"]
    QTest.mouseClick(maximize, Qt.MouseButton.LeftButton)
    application.processEvents()
    assert window.isMaximized()
    assert maximize.toolTip() == "还原窗口"

    # 双击标题栏还原窗口并核对按钮提示。
    QTest.mouseDClick(window.title_bar, Qt.MouseButton.LeftButton)
    application.processEvents()
    assert not window.isMaximized()
    assert maximize.toolTip() == "最大化"

    # 最小化后还原窗口，再点击关闭按钮。
    QTest.mouseClick(window.title_bar.control_buttons["minimize"], Qt.MouseButton.LeftButton)
    assert window.isMinimized()
    window.showNormal()
    QTest.mouseClick(window.title_bar.control_buttons["close"], Qt.MouseButton.LeftButton)
    assert not window.isVisible()


def test_realtime_demo_refresh_preserves_cards_and_restores_logs(window, application):
    """验证演示展示、清空日志和刷新复用已有卡片。

    Args:
        window: 主窗口。
        application: 界面应用实例。

    Returns:
        None  # 演示交互和卡片复用断言通过
    """
    # 检查三台设备的不同状态、图片资源和禁用入口。
    page = window.page_stack.widget(0)
    cards = tuple(page.machine_cards)
    assert [card.badge.text() for card in cards] == ["运行中", "待机中", "等待关闭"]
    assert all(not card.preview.source.isNull() for card in cards)
    assert all(not card.state_icon.pixmap().isNull() for card in cards)
    assert not page.start_button.isEnabled()
    assert not page.stop_button.isEnabled()
    assert all(not card.more_button.isEnabled() for card in cards)

    # 清空日志后刷新，同时恢复被改动的卡片文字。
    QTest.mouseClick(page.clear_button, Qt.MouseButton.LeftButton)
    assert page.log_table.rowCount() == 0
    cards[0].state_label.setText("临时展示")
    page.auto_scroll.setChecked(False)
    QTest.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    application.processEvents()
    assert tuple(page.machine_cards) == cards
    assert cards[0].state_label.text() == "测量中"
    assert page.log_table.rowCount() == len(LOG_ROWS)
    assert page.log_table.item(3, 3).text() == LOG_ROWS[-1][-1]
    assert not page.auto_scroll.isChecked()


def test_realtime_log_scrolling_and_small_window_access(window, application):
    """验证日志滚动选项和小窗口中的完整内容访问。

    Args:
        window: 主窗口。
        application: 界面应用实例。

    Returns:
        None  # 日志滚动和页面布局断言通过
    """
    # 填入超出可见范围的日志，并验证自动滚动至最新行。
    page = window.page_stack.widget(0)
    for number in range(30):
        page.append_log(("14:33:00", "INFO", "Demo", f"测试日志 {number}"))
    application.processEvents()
    scrollbar = page.log_table.verticalScrollBar()
    assert scrollbar.maximum() > 0
    assert scrollbar.value() == scrollbar.maximum()

    # 关闭自动滚动后追加日志，保持当前阅读位置。
    page.auto_scroll.setChecked(False)
    scrollbar.setValue(0)
    page.append_log(("14:34:00", "INFO", "Demo", "追加日志"))
    application.processEvents()
    assert scrollbar.value() == 0
    page.auto_scroll.setChecked(True)
    assert scrollbar.value() == scrollbar.maximum()

    # 在最小窗口下保留三列卡片，并通过纵向滚动访问日志。
    window.resize(1280, 720)
    application.processEvents()
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    assert page.scroll_area.verticalScrollBar().maximum() > 0
    for card in page.machine_cards:
        assert card.width() >= card.minimumWidth()
        assert card.preview.pixmap().width() <= card.preview.width()
        # 检查连接线横跨相邻圆点之间的空间。
        for index, connector in enumerate(card.steps.connectors):
            left_dot = card.steps.dots[index].geometry()
            right_dot = card.steps.dots[index + 1].geometry()
            assert connector.width() > 20
            assert abs(connector.geometry().left() - left_dot.right()) <= 3
            assert abs(connector.geometry().right() - right_dot.left()) <= 4
    page.scroll_area.verticalScrollBar().setValue(page.scroll_area.verticalScrollBar().maximum())
    log_position = page.log_table.mapTo(page.scroll_area.viewport(), page.log_table.rect().topLeft())
    assert page.scroll_area.viewport().rect().intersects(page.log_table.rect().translated(log_position))


def test_device_edit_validation_cancel_and_page_reuse(window, application, monkeypatch):
    """验证设备编辑、必填校验、取消和页面复用。

    Args:
        window: 主窗口。
        application: 界面应用实例。
        monkeypatch: 消息框替换工具。

    Returns:
        返回示例：
            None  # 编辑与取消行为断言通过
    """
    from PySide6.QtWidgets import QMessageBox, QPushButton

    # 打开设备页并检查默认记录与启用状态。
    window.switch_page("devices")
    page = window.page_stack.currentWidget()
    assert page.table.rowCount() == 3
    assert page.editor.isHidden()
    position = page.table.visualItemRect(page.table.item(2, 1)).center()
    QTest.mouseClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert page.table.currentRow() == 2
    assert page.editor.isHidden()
    page.findChild(QPushButton, "editDevice_3").click()
    assert page.editor.isVisible()
    assert page.editor.isModal()
    assert page.editor.windowTitle() == "编辑设备"
    assert not page.enabled_checkbox.isChecked()

    # 保存编辑内容并验证导航切换保留页面内存。
    page.field_inputs["machine_name"].setText("测试设备")
    page.enabled_checkbox.setChecked(True)
    page.save_button.click()
    assert page.devices[2]["machine_name"] == "测试设备"
    assert page.devices[2]["enabled"] is True
    assert page.editor.isHidden()
    window.switch_page("realtime")
    window.switch_page("devices")
    assert window.page_stack.currentWidget() is page

    # 缺少必填字段时保留原记录，并展示提示。
    page.show_device(2)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *arguments: warnings.append(arguments))
    page.field_inputs["camera_serial"].setText("  ")
    page.save_button.click()
    assert warnings
    assert page.editor.isVisible()
    assert page.devices[2]["camera_serial"] == "MV-CA013458"

    # 取消未保存内容后重新打开，恢复内存中的记录。
    page.cancel_button.click()
    assert page.editor.isHidden()
    page.show_device(2)
    assert page.field_inputs["camera_serial"].text() == "MV-CA013458"
    page.field_inputs["machine_name"].setText("未保存修改")
    page.editor.close()
    page.show_device(2)
    assert page.field_inputs["machine_name"].text() == "测试设备"
    QTest.keyClick(page.editor, Qt.Key.Key_Escape)
    assert page.editor.isHidden()
    application.processEvents()


def test_device_create_delete_and_restart(window, monkeypatch):
    """验证新增、删除确认、空列表及重新创建页面恢复演示数据。

    Args:
        window: 主窗口。
        monkeypatch: 确认对话框替换工具。

    Returns:
        返回示例：
            None  # 设备内存操作与初始数据隔离断言通过
    """
    from PySide6.QtWidgets import QMessageBox
    from ui.pages.devices_page import DevicesPage

    # 新增设备并核对编号和表格结果。
    window.switch_page("devices")
    page = window.page_stack.currentWidget()
    page.create_button.click()
    for field, value in (
        ("machine_name", "4号皮带机"),
        ("camera_serial", "MV-CA013459"),
        ("frequency_meter_serial", "FM-1004"),
    ):
        page.field_inputs[field].setText(value)
    page.save_button.click()
    assert page.editor.isHidden()
    assert page.devices[-1]["id"] == 4
    assert page.table.rowCount() == 4
    assert page.devices[-1]["created_at"] == page.devices[-1]["updated_at"]

    # 取消删除时保留记录，确认后可删除到空列表。
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: None)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda dialog: dialog.defaultButton())
    page.delete_device(3)
    assert len(page.devices) == 4
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda dialog: next(button for button in dialog.buttons() if button.text() == "删除"),
    )
    for row in reversed(range(4)):
        page.delete_device(row)
    assert page.table.rowCount() == 0
    assert page.editor.isHidden()
    page.create_button.click()
    assert page.editor.isVisible()
    assert page.field_inputs["machine_name"].text() == ""

    # 新页面读取原始演示记录，不继承已删除的数据。
    fresh_page = DevicesPage()
    assert len(fresh_page.devices) == 3
    assert fresh_page.devices[0]["machine_name"] == "1号皮带机"
    fresh_page.deleteLater()


def test_device_layout_keeps_editor_actions_accessible(window, application):
    """验证默认和最小窗口下的表单操作区均可访问。

    Args:
        window: 主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 设备页面布局断言通过
    """
    # 分别检查两种尺寸下的全宽表格和共用弹窗。
    window.switch_page("devices")
    page = window.page_stack.currentWidget()
    editor = page.editor
    for width, height in ((1600, 900), (1280, 720)):
        window.resize(width, height)
        application.processEvents()
        assert page.table.width() > page.width() - 100
        assert page.table.horizontalScrollBar().maximum() == 0
        page.create_button.click()
        application.processEvents()
        assert page.editor is editor
        assert page.editor.windowTitle() == "新建设备"
        assert page.save_button.isVisible()
        assert page.editor.rect().contains(
            page.save_button.mapTo(page.editor, page.save_button.rect().bottomRight())
        )
        assert page.editor_scroll.horizontalScrollBar().maximum() == 0
        page.cancel_button.click()
        assert page.editor.isHidden()
