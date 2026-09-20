"""桌面主窗口的导航、状态和窗口行为测试。"""

import os
import sqlite3
from contextlib import closing

# 默认使用离屏平台运行界面测试。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from src.machine_service import MachineService
from src.repo.machine_repo import MachineRepo
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
def window(application, tmp_path):
    """创建窗口并在测试后释放。

    Args:
        application: 界面应用实例。
        tmp_path: 临时数据库目录。

    Yields:
        MainWindow()  # 测试期间可操作的主窗口

    Returns:
        返回示例：
            None  # 测试结束后释放窗口，生成器不返回额外结果
    """
    # 显示测试窗口并处理初始化事件。
    database_path = tmp_path / "machines.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    main_window = MainWindow(MachineService(MachineRepo(database_path)))
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


def test_device_create_persists_and_reloads(window, application, monkeypatch):
    """验证新增持久化、必填校验、取消和重载。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。
        monkeypatch: 消息框替换工具。

    Returns:
        返回示例：
            None  # 新增设备在新页面中可读取
    """
    from PySide6.QtWidgets import QMessageBox, QPushButton
    from ui.pages.devices_page import DevicesPage

    # 在空列表中打开表单并验证必填提示。
    window.switch_page("devices")
    page = window.page_stack.currentWidget()
    assert page.table.rowCount() == 0
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *arguments: warnings.append(arguments))
    page.create_button.click()
    page.save_button.click()
    assert warnings[-1][1] == "请填写必填信息"
    assert page.editor.isVisible()

    # 保存输入并验证数据库编号、默认时间和选中行。
    for field, value in (
        ("machine_name", " 新设备 "),
        ("camera_serial", " CAM001 "),
        ("frequency_meter_serial", " FREQ001 "),
    ):
        page.field_inputs[field].setText(value)
    page.enabled_checkbox.setChecked(False)
    page.save_button.click()
    assert page.editor.isHidden()
    assert page.table.currentRow() == 0
    assert page.devices[0]["machine_name"] == "新设备"
    assert page.devices[0]["enabled"] is False
    assert page.devices[0]["remark"] == ""
    assert page.devices[0]["created_at"] == page.devices[0]["updated_at"]
    assert not page.findChild(QPushButton, "editDevice_1").isEnabled()
    assert not page.findChild(QPushButton, "deleteDevice_1").isEnabled()

    # 新页面读取同一数据库，并验证取消不新增记录。
    fresh_page = DevicesPage(MachineService(MachineRepo(page.machine_service.machine_repo.database_path)))
    assert fresh_page.devices == page.devices
    fresh_page.deleteLater()
    page.create_button.click()
    page.field_inputs["machine_name"].setText("未保存设备")
    page.cancel_button.click()
    page.create_button.click()
    assert page.field_inputs["machine_name"].text() == ""
    QTest.keyClick(page.editor, Qt.Key.Key_Escape)
    assert page.editor.isHidden()
    assert len(page.machine_service.machine_repo.list_all()) == 1
    application.processEvents()


@pytest.mark.parametrize("duplicate_field, expected_message", [
    ("machine_name", "机器名称已存在，请修改。"),
    ("camera_serial", "相机序列号已被其他设备使用。"),
    ("frequency_meter_serial", "频率仪序列号已被其他设备使用。"),
])
def test_device_duplicate_keeps_form(window, monkeypatch, duplicate_field, expected_message):
    """验证三个字段分别拒绝重复且保留表单。

    Args:
        window: 测试主窗口。
        monkeypatch: 消息框替换工具。
        duplicate_field: 重复字段名。
        expected_message: 对应中文提示。

    Returns:
        返回示例：
            None  # 重复记录未写入且表单内容保留
    """
    from PySide6.QtWidgets import QMessageBox

    # 写入已有设备并准备只重复一个字段的新记录。
    page = window.page_stack.widget(4)
    original = {
        "machine_name": "原设备",
        "camera_serial": "CAM001",
        "frequency_meter_serial": "FREQ001",
    }
    page.machine_service.machine_repo.insert(**original)
    page.create_button.click()
    for field, value in original.items():
        page.field_inputs[field].setText(value if field == duplicate_field else value + "新")

    # 提交重复记录并验证中文提示、表单和数据库内容。
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *arguments: warnings.append(arguments))
    page.save_button.click()
    assert warnings[-1][2] == expected_message
    assert page.editor.isVisible()
    assert page.field_inputs[duplicate_field].text() == original[duplicate_field]
    assert len(page.machine_service.machine_repo.list_all()) == 1
    page.editor.reject()


@pytest.mark.parametrize("failed_operation", ["insert", "list_all"])
def test_device_database_failure_message(window, monkeypatch, failed_operation):
    """验证插入失败和提交后刷新失败使用不同提示。

    Args:
        window: 测试主窗口。
        monkeypatch: 数据库和消息框替换工具。
        failed_operation: 模拟失败的数据库方法。

    Returns:
        返回示例：
            None  # 提示和表单状态与提交结果一致
    """
    from PySide6.QtWidgets import QMessageBox

    # 填写完整表单并替换指定数据库操作。
    page = window.page_stack.widget(4)
    page.create_button.click()
    for field in page.field_inputs:
        page.field_inputs[field].setText(field)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *arguments: warnings.append(arguments))

    def fail_database_operation(*arguments, **keywords):
        """模拟数据库访问失败。

        Args:
            arguments: 数据库方法的位置参数。
            keywords: 数据库方法的关键字参数。

        Returns:
            返回示例：
                None  # 不返回结果，抛出数据库异常
        """
        raise sqlite3.OperationalError("测试数据库不可用")

    monkeypatch.setattr(page.machine_service.machine_repo, failed_operation, fail_database_operation)
    page.save_button.click()

    # 用新 Repo 核对实际提交结果与弹窗状态。
    records = MachineRepo(page.machine_service.machine_repo.database_path).list_all()
    if failed_operation == "insert":
        assert warnings[-1][1] == "保存失败"
        assert page.editor.isVisible()
        assert records == []
        page.editor.reject()
    else:
        assert "设备已保存" in warnings[-1][2]
        assert page.editor.isHidden()
        assert len(records) == 1


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
