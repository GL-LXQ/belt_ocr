"""离屏验证图片入口复用历史详情、目录打开和窗口退出流程。"""

from pathlib import Path
from unittest.mock import Mock, call
import os
import subprocess
import sys

import pytest
from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import FluentWindow

from src.controller.controller import Result
from ui import evidence_directory
from ui import main_window as main_window_module
from ui.pages import history_page as history_page_module


def test_shared_ui_imports_do_not_load_history_page() -> None:
    """验证共享组件和图片、异常入口可独立加载，不再导入历史业务页。

    Args:
        无。

    Returns:
        None  # 新进程成功导入共享组件及调用方，历史业务模块未被加载
    """
    script = """
import importlib
import sys

# 沿用项目 pytest 配置中的 src 模块路径。
sys.path.insert(0, "src")

for module_name in (
    "ui.date_range_picker",
    "ui.evidence_directory",
    "ui.evidence_order",
    "ui.pages.abnormal_events_page",
    "ui.pages.image_management_page",
    "ui.image_evidence_viewer",
):
    importlib.import_module(module_name)
    assert "ui.pages.history_page" not in sys.modules, module_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class IntegrationImagesPage(QWidget):
    """仅提供主窗口集成所需接口的图片页替身。"""

    record_requested = Signal(str)

    def __init__(self, parent: QWidget, controller: Mock) -> None:
        """保存控制器并建立关闭动作替身。

        Args:
            parent: 所属主窗口。
            controller: 主窗口收到的控制器。

        Returns:
            None  # 页面导航、记录信号和资源释放接口已准备
        """
        super().__init__(parent)
        self.setObjectName("images")
        self.controller = controller
        self.shutdown = Mock()


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """复用离屏控件测试使用的 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 当前测试进程的 Qt 应用
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def directory_actions(qt_application: QApplication, monkeypatch: pytest.MonkeyPatch):
    """替换目录打开和错误提示，避免执行真实系统动作。

    Args:
        qt_application: 离屏 Qt 应用。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        返回示例：
            (
                QWidget(),  # 提示所属控件
                Mock(),  # 系统打开替身
                Mock(),  # 错误提示替身
            )
    """
    parent = QWidget()
    opener = Mock(return_value=True)
    error = Mock()
    monkeypatch.setattr(evidence_directory.QDesktopServices, "openUrl", opener)
    monkeypatch.setattr(evidence_directory.InfoBar, "error", error)
    yield parent, opener, error
    parent.deleteLater()


@pytest.fixture
def integration_window(qt_application: QApplication, monkeypatch: pytest.MonkeyPatch):
    """创建不显示窗口、不访问设备的主窗口集成环境。

    Args:
        qt_application: 离屏 Qt 应用。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        MainWindow()  # 使用图片页替身和空业务结果的主窗口
    """
    # 为页面初始化提供最小业务返回数据。
    controller = Mock()
    controller.list_enabled_machines.return_value = Result.ok({"machines": []})
    controller.list_machines.return_value = Result.ok({"machines": []})
    controller.list_record_machines.return_value = Result.ok({"machines": []})
    controller.list_measurement_records.return_value = Result.ok({
        "records": [],
        "total": 0,
        "total_pages": 1,
    })
    controller.is_monitoring_running.return_value = Result.ok({"running": False})
    controller.get_today_measurement_summary.return_value = Result.ok({
        "recognition_count": 0,
        "pending_review_count": 0,
    })

    # 隔离图片后台任务，只验证主窗口约定的调用。
    monkeypatch.setattr(main_window_module, "ImageManagementPage", IntegrationImagesPage)
    window = main_window_module.MainWindow(controller)
    yield window
    controller.is_monitoring_running.return_value = Result.ok({"running": False})
    window.close()
    window.deleteLater()


@pytest.mark.parametrize("use_string", [False, True])
def test_directory_opener_preserves_saved_path(
    tmp_path: Path,
    directory_actions,
    use_string: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证沿用 QUrl 和系统打开接口，不在主线程重新访问目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        directory_actions: 提示控件及系统动作替身。
        use_string: True 传入字符串，False 传入 Path。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 两类路径都原样传给系统且没有失败提示
    """
    parent, opener, error = directory_actions
    directory = str(tmp_path) if use_string else tmp_path
    # 打开时复用已知状态，不同步扫描或检查网络目录。
    directory_scan = Mock(side_effect=AssertionError("主线程不应枚举目录"))
    directory_stat = Mock(side_effect=AssertionError("主线程不应检查目录"))
    with monkeypatch.context() as context:
        context.setattr(os, "scandir", directory_scan)
        context.setattr(Path, "stat", directory_stat)
        assert evidence_directory.open_evidence_directory(directory, parent, "available")
    directory_scan.assert_not_called()
    directory_stat.assert_not_called()
    opener.assert_called_once()
    assert opener.call_args.args[0].toLocalFile() == str(tmp_path)
    error.assert_not_called()


@pytest.mark.parametrize("directory_kind", ["missing", "file", "empty"])
def test_directory_opener_reports_missing_saved_path(tmp_path: Path, directory_actions, directory_kind: str) -> None:
    """验证缺失目录、普通文件和空路径不会调用系统打开接口。

    Args:
        tmp_path: pytest 提供的临时目录。
        directory_actions: 提示控件及系统动作替身。
        directory_kind: 缺失目录、普通文件或空字符串场景。

    Returns:
        None  # 失败提示包含保存的路径，系统打开接口未被调用
    """
    parent, opener, error = directory_actions
    directory = tmp_path / "saved-evidence"
    if directory_kind == "file":
        directory.write_text("not a directory", encoding="utf-8")
    saved_path = "" if directory_kind == "empty" else str(directory)
    assert not evidence_directory.open_evidence_directory(saved_path, parent, "missing_directory")
    opener.assert_not_called()
    error.assert_called_once()
    assert f"保存的路径：{saved_path or '（空）'}" in error.call_args.args[1]
    expected_message = "未保存" if not saved_path else "不存在"
    assert expected_message in error.call_args.args[1]


@pytest.mark.parametrize(
    ("evidence_state", "expected_message"),
    [
        ("access_denied", "没有权限访问证据目录。"),
        ("read_error", "访问证据目录失败，请刷新后重试。"),
    ],
)
def test_directory_opener_reports_access_error(
    tmp_path: Path,
    directory_actions,
    evidence_state: str,
    expected_message: str,
) -> None:
    """验证权限及其他访问错误保留准确分类和保存路径。

    Args:
        tmp_path: pytest 提供的临时目录。
        directory_actions: 提示控件及系统动作替身。
        evidence_state: 后台读取已确认的目录状态。
        expected_message: 对应错误提示。

    Returns:
        None  # 已报告对应错误，未请求系统打开目录
    """
    parent, opener, error = directory_actions
    assert not evidence_directory.open_evidence_directory(tmp_path, parent, evidence_state)
    opener.assert_not_called()
    error.assert_called_once_with(
        "证据文件夹打开失败",
        f"{expected_message}\n保存的路径：{tmp_path}",
        duration=-1,
        parent=parent,
    )


def test_directory_opener_reports_system_rejection(tmp_path: Path, directory_actions) -> None:
    """验证系统拒绝不会误报为权限拒绝，并显示保存路径。

    Args:
        tmp_path: pytest 提供的临时目录。
        directory_actions: 提示控件及系统动作替身。

    Returns:
        None  # 返回失败并显示系统打开失败和保存路径
    """
    parent, opener, error = directory_actions
    opener.return_value = False
    assert not evidence_directory.open_evidence_directory(tmp_path, parent)
    error.assert_called_once_with(
        "证据文件夹打开失败",
        f"系统未能打开证据目录，请检查系统文件夹打开功能。\n保存的路径：{tmp_path}",
        duration=-1,
        parent=parent,
    )


def test_history_folder_action_reuses_shared_opener(integration_window, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证历史页委托共享入口并保留原有缺失目录行为。

    Args:
        integration_window: 不显示的主窗口。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 有目录时只委托一次，无当前目录时不发起打开请求
    """
    page = integration_window.history_page
    opener = Mock(return_value=True)
    monkeypatch.setattr(history_page_module, "open_evidence_directory", opener)
    page.current_evidence_directory = Path("saved-evidence")
    page.open_evidence_directory()
    opener.assert_called_once_with(Path("saved-evidence"), page.detail_dialog.widget)
    page.current_evidence_directory = None
    page.open_evidence_directory()
    assert opener.call_count == 1


def test_image_record_request_reuses_history_page(integration_window) -> None:
    """验证图片记录信号切换已有历史页并使用原有详情入口。

    Args:
        integration_window: 不显示的主窗口。

    Returns:
        None  # 控制器正确传入，导航先于详情，既有历史页实例保持不变
    """
    window = integration_window
    history_page = window.history_page
    actions = Mock()
    actions.attach_mock(Mock(wraps=window.switch_page), "switch_page")
    actions.attach_mock(Mock(), "show_record_detail")
    window.switch_page = actions.switch_page
    history_page.show_record_detail = actions.show_record_detail

    # 图片页携带原有控制器，并通过信号进入既有历史详情。
    assert window.images_page.controller is window.controller
    window.images_page.record_requested.emit("saved-session")
    assert actions.mock_calls == [
        call.switch_page("history"),
        call.show_record_detail("saved-session"),
    ]
    assert window.pages["history"] is history_page
    assert window.stackedWidget.currentWidget() is history_page


def test_window_keeps_images_running_until_monitoring_stops(integration_window) -> None:
    """验证等待监测结束时不释放图片任务，重复关闭只停监测一次。

    Args:
        integration_window: 不显示的主窗口。

    Returns:
        None  # 监测运行时两次关闭均被拦截，结束后才释放图片任务
    """
    window = integration_window
    window.controller.is_monitoring_running.return_value = Result.ok({"running": True})
    for _ in range(2):
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
    window.images_page.shutdown.assert_not_called()
    window.controller.stop_monitoring.assert_called_once_with()
    assert window.controller.monitoring_finished_signal.connect.call_args_list.count(call(window.close)) == 1

    # 监测停止后，正常关闭才释放图片后台资源。
    window.controller.is_monitoring_running.return_value = Result.ok({"running": False})
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    window.images_page.shutdown.assert_called_once_with()


def test_window_does_not_shutdown_images_when_base_close_is_ignored(
    integration_window,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证基类拒绝关闭时不释放图片任务。

    Args:
        integration_window: 不显示的主窗口。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 关闭未被接受，图片任务仍可继续使用
    """
    window = integration_window
    monkeypatch.setattr(FluentWindow, "closeEvent", lambda self, event: event.ignore())
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    window.images_page.shutdown.assert_not_called()
