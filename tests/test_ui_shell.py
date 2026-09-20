"""桌面主窗口的导航、状态和窗口行为测试。"""

import os

# 默认使用离屏平台运行界面测试。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from ui.main_window import MainWindow, PAGES


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
