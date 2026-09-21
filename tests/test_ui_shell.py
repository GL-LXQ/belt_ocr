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

from src.service.machine_service import MachineService
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


def test_realtime_cards_follow_machine_table(window, application):
    """验证卡片按机器表重建、空态提示和状态占位。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 卡片数量和标题跟随机器表
    """
    # 空库时没有卡片，显示空态提示。
    page = window.page_stack.widget(0)
    assert page.machine_cards == []
    assert page.empty_hint.text() == "暂无机器，请先在机器管理页添加。"

    # 写入两台机器后刷新，卡片按数据库顺序重建。
    repository = window.machine_service.machine_repo
    first_id = repository.insert("1号皮带机", "CAM001", "FREQ001")
    repository.insert("2号皮带机", "CAM002", "FREQ002")
    page.reload_machines()
    application.processEvents()
    assert page.empty_hint is None
    assert [card.title.text() for card in page.machine_cards] == ["1号皮带机", "2号皮带机"]

    # 没有实时数据源的字段显示占位内容，画面为灰色占位块。
    card = page.machine_cards[0]
    assert card.badge.text() == "未启动"
    assert card.state_label.text() == "未启动"
    assert card.frequency_label.text() == "--"
    assert card.preview.pixmap().isNull()

    # 最小窗口下卡片保持最小宽度，连接线对齐相邻圆点。
    window.resize(1280, 720)
    application.processEvents()
    assert page.scroll_area.horizontalScrollBar().maximum() == 0
    for card in page.machine_cards:
        assert card.width() >= card.minimumWidth()
        for index, connector in enumerate(card.steps.connectors):
            left_dot = card.steps.dots[index].geometry()
            assert abs(connector.geometry().left() - left_dot.right()) <= 3

    # 软删除一台机器后刷新，卡片数量跟随变化。
    repository.soft_delete(first_id)
    page.reload_machines()
    application.processEvents()
    assert [card.title.text() for card in page.machine_cards] == ["2号皮带机"]

    # 每行固定三台，第四台自动换到下一行。
    for number in (3, 4, 5):
        repository.insert(f"{number}号皮带机", f"CAM00{number}", f"FREQ00{number}")
    page.reload_machines()
    application.processEvents()
    assert len(page.machine_cards) == 4
    position = page.cards_layout.getItemPosition(page.cards_layout.indexOf(page.machine_cards[3]))
    assert position[:2] == (1, 0)
    # 等布局稳定后核对第二行不会压扁卡片，卡片保持自身高度并由页面滚动。
    QTest.qWait(10)
    assert all(card.height() >= card.sizeHint().height() for card in page.machine_cards)


def test_realtime_hides_disabled_machines(window, application):
    """验证停用机器不进入实时监测卡片，但仍保留在机器列表数据中。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 卡片只包含已启用机器
    """
    # 写入一台启用机器和一台停用机器。
    repository = window.machine_service.machine_repo
    repository.insert("1号皮带机", "CAM001", "FREQ001")
    repository.insert("2号皮带机", "CAM002", "FREQ002", False)

    # 实时监测只展示已启用机器。
    page = window.page_stack.widget(0)
    page.reload_machines()
    application.processEvents()
    assert [card.title.text() for card in page.machine_cards] == ["1号皮带机"]

    # 机器管理页的数据源仍包含停用机器。
    assert len(window.machine_service.list_machines()) == 2


def test_realtime_log_appending_and_clearing(window, application):
    """验证演示日志填充、追加滚动和清空。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 日志填充、滚动与清空断言通过
    """
    # 页面构造时填入固定演示日志。
    page = window.page_stack.widget(0)
    assert page.log_table.rowCount() == len(LOG_ROWS)
    assert page.log_table.item(3, 3).text() == LOG_ROWS[-1][-1]

    # 追加超出可见范围的日志，验证自动滚动到最新行。
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

    # 重新开启自动滚动回到最新行，清空按钮移除全部日志。
    page.auto_scroll.setChecked(True)
    assert scrollbar.value() == scrollbar.maximum()
    QTest.mouseClick(page.clear_button, Qt.MouseButton.LeftButton)
    assert page.log_table.rowCount() == 0


def test_machine_create_persists_and_reloads(window, application, monkeypatch):
    """验证新增持久化、必填校验、取消和重载。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。
        monkeypatch: 消息框替换工具。

    Returns:
        返回示例：
            None  # 新增机器在新页面中可读取
    """
    from PySide6.QtWidgets import QMessageBox, QPushButton
    from ui.pages.machines_page import MachinesPage

    # 在空列表中打开表单并验证必填提示。
    window.switch_page("machines")
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
        ("machine_name", " 新机器 "),
        ("camera_serial", " CAM001 "),
        ("frequency_meter_serial", " FREQ001 "),
    ):
        page.field_inputs[field].setText(value)
    page.enabled_checkbox.setChecked(False)
    page.save_button.click()
    assert page.editor.isHidden()
    assert page.table.currentRow() == 0
    assert page.machines[0]["machine_name"] == "新机器"
    assert page.machines[0]["enabled"] is False
    assert page.machines[0]["remark"] == ""
    assert page.machines[0]["created_at"] == page.machines[0]["updated_at"]
    assert page.findChild(QPushButton, "editMachine_1").isEnabled()
    assert page.findChild(QPushButton, "deleteMachine_1").isEnabled()

    # 新页面读取同一数据库，并验证取消不新增记录。
    fresh_page = MachinesPage(MachineService(MachineRepo(page.machine_service.machine_repo.database_path)))
    assert fresh_page.machines == page.machines
    fresh_page.deleteLater()
    page.create_button.click()
    page.field_inputs["machine_name"].setText("未保存机器")
    page.cancel_button.click()
    page.create_button.click()
    assert page.field_inputs["machine_name"].text() == ""
    QTest.keyClick(page.editor, Qt.Key.Key_Escape)
    assert page.editor.isHidden()
    assert len(page.machine_service.machine_repo.list_all()) == 1
    application.processEvents()


@pytest.mark.parametrize("duplicate_field, expected_message", [
    ("machine_name", "机器名称已存在，请修改。"),
    ("camera_serial", "相机序列号已被其他机器使用。"),
    ("frequency_meter_serial", "频率仪序列号已被其他机器使用。"),
])
def test_machine_duplicate_keeps_form(window, monkeypatch, duplicate_field, expected_message):
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

    # 写入已有机器并准备只重复一个字段的新记录。
    page = window.page_stack.widget(4)
    original = {
        "machine_name": "原机器",
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
def test_machine_database_failure_message(window, monkeypatch, failed_operation):
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
        assert "机器已保存" in warnings[-1][2]
        assert page.editor.isHidden()
        assert len(records) == 1


def test_machine_layout_keeps_editor_actions_accessible(window, application):
    """验证默认和最小窗口下的表单操作区均可访问。

    Args:
        window: 主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 机器页面布局断言通过
    """
    # 分别检查两种尺寸下的全宽表格和共用弹窗。
    window.switch_page("machines")
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
        assert page.editor.windowTitle() == "新建机器"
        assert page.save_button.isVisible()
        assert page.editor.rect().contains(
            page.save_button.mapTo(page.editor, page.save_button.rect().bottomRight())
        )
        assert page.editor_scroll.horizontalScrollBar().maximum() == 0
        page.cancel_button.click()
        assert page.editor.isHidden()


def test_machine_edit_updates_record_and_selects_row(window, application):
    """验证编辑回填表单、保存后刷新列表并写回数据库。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。

    Returns:
        返回示例：
            None  # 修改后的名称、启用状态和备注写入数据库
    """
    from PySide6.QtWidgets import QPushButton

    # 写入一台机器并刷新列表。
    page = window.page_stack.widget(4)
    machine_id = page.machine_service.machine_repo.insert("1号皮带机", "CAM001", "FREQ001")
    page.machines = page.machine_service.list_machines()
    page.populate_machines()

    # 点击编辑入口应回填表单并进入编辑状态。
    QTest.mouseClick(page.findChild(QPushButton, f"editMachine_{machine_id}"), Qt.MouseButton.LeftButton)
    application.processEvents()
    assert page.editor.isVisible()
    assert page.editor.windowTitle() == "编辑机器"
    assert page.field_inputs["machine_name"].text() == "1号皮带机"
    assert page.field_inputs["camera_serial"].text() == "CAM001"
    assert page.enabled_checkbox.isChecked()
    assert page.table.currentRow() == 0

    # 修改名称、启用状态和备注后保存。
    page.field_inputs["machine_name"].setText("1号皮带机A")
    page.enabled_checkbox.setChecked(False)
    page.remark_input.setPlainText("换线停用")
    page.save_button.click()
    application.processEvents()

    # 核对弹窗、表格和数据库中的修改结果。
    assert page.editor.isHidden()
    assert page.machines[0]["machine_name"] == "1号皮带机A"
    assert page.machines[0]["enabled"] is False
    assert page.machines[0]["remark"] == "换线停用"
    assert page.table.item(0, 1).text() == "1号皮带机A"
    assert page.table.currentRow() == 0
    assert page.machine_service.machine_repo.list_all()[0]["machine_name"] == "1号皮带机A"


def test_machine_edit_keeps_own_fields(window, application, monkeypatch):
    """验证编辑不改动唯一字段时不会被判为重复。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。
        monkeypatch: 消息框替换工具。

    Returns:
        返回示例：
            None  # 表单关闭且记录保持一条
    """
    from PySide6.QtWidgets import QMessageBox, QPushButton

    # 写入机器并打开编辑弹窗。
    page = window.page_stack.widget(4)
    machine_id = page.machine_service.machine_repo.insert("1号皮带机", "CAM001", "FREQ001")
    page.machines = page.machine_service.list_machines()
    page.populate_machines()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *arguments: warnings.append(arguments))
    QTest.mouseClick(page.findChild(QPushButton, f"editMachine_{machine_id}"), Qt.MouseButton.LeftButton)

    # 只修改备注并保存。
    page.remark_input.setPlainText("只改备注")
    page.save_button.click()
    application.processEvents()

    # 核对没有重复提示且记录内容已更新。
    assert warnings == []
    assert page.editor.isHidden()
    assert len(page.machine_service.machine_repo.list_all()) == 1
    assert page.machines[0]["remark"] == "只改备注"


def test_machine_delete_after_confirmation(window, application, monkeypatch):
    """验证确认后标记删除机器并刷新列表。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。
        monkeypatch: 确认框替换工具。

    Returns:
        返回示例：
            None  # 列表移除该机器且数据库保留删除标记
    """
    from PySide6.QtWidgets import QMessageBox, QPushButton

    # 写入机器并刷新列表。
    page = window.page_stack.widget(4)
    repository = page.machine_service.machine_repo
    machine_id = repository.insert("1号皮带机", "CAM001", "FREQ001")
    page.machines = page.machine_service.list_machines()
    page.populate_machines()

    # 让确认框直接返回“删除”按钮，再点击删除入口。
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda box: next(button for button in box.buttons() if button.text() == "删除"),
    )
    QTest.mouseClick(page.findChild(QPushButton, f"deleteMachine_{machine_id}"), Qt.MouseButton.LeftButton)
    application.processEvents()

    # 核对列表、内存数据和数据库中的删除标记。
    assert page.table.rowCount() == 0
    assert page.machines == []
    assert repository.list_all() == []
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute("SELECT is_deleted FROM machine WHERE id = ?", (machine_id,)).fetchone() == (1,)


def test_machine_delete_cancel_keeps_record(window, application, monkeypatch):
    """验证取消确认后不修改机器记录。

    Args:
        window: 测试主窗口。
        application: 界面应用实例。
        monkeypatch: 确认框替换工具。

    Returns:
        返回示例：
            None  # 机器仍在列表和数据库中
    """
    from PySide6.QtWidgets import QMessageBox, QPushButton

    # 写入机器并刷新列表。
    page = window.page_stack.widget(4)
    repository = page.machine_service.machine_repo
    machine_id = repository.insert("1号皮带机", "CAM001", "FREQ001")
    page.machines = page.machine_service.list_machines()
    page.populate_machines()

    # 让确认框返回“取消”按钮，再点击删除入口。
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda box: next(button for button in box.buttons() if button.text() == "取消"),
    )
    QTest.mouseClick(page.findChild(QPushButton, f"deleteMachine_{machine_id}"), Qt.MouseButton.LeftButton)
    application.processEvents()

    # 核对机器仍在列表和数据库中。
    assert page.table.rowCount() == 1
    assert page.machines[0]["id"] == machine_id
    assert len(repository.list_all()) == 1
