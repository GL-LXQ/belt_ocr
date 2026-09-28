"""验证异常事件查询、中文原因和页面详情。"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from repo.abnormal_event_repo import AbnormalEventRepo
from src.controller.controller import AppController
from src.service.abnormal_event_service import AbnormalEventService
import ui.__main__ as desktop_entry
from ui.main_window import PAGES
from ui.pages.abnormal_events_page import AbnormalEventsPage


SESSION_ID = "5ad75ed720044b2a9da465055e8ea424"


@pytest.fixture
def abnormal_event_service(tmp_path: Path) -> AbnormalEventService:
    """建立包含多台机器和多种异常原因的事件表。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            AbnormalEventService(...)  # 已保存三条异常事件的服务
    """
    # 创建现有表结构并写入目标记录与其他筛选记录。
    database_path = tmp_path / "measurements.recovery.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        AbnormalEventRepo.create_table(connection)
    abnormal_event_repo = AbnormalEventRepo(database_path)
    abnormal_event_repo.insert(
        100.0, "1", SESSION_ID, "OCR_TIMEOUT", '{"session_errors": ["OCR_TIMEOUT"]}'
    )
    abnormal_event_repo.insert(
        150.0, "1", "session-other", "CAPTURE_FAILED", '{"message": "相机异常"}'
    )
    abnormal_event_repo.insert(
        200.0, "2", "session-two", "CUSTOM_REASON", '{"message": "未知原因"}'
    )
    return AbnormalEventService(abnormal_event_repo)


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """创建异常事件页面测试使用的 Qt 应用。

    Args:
        无外部参数。

    Returns:
        返回示例：
            QApplication([])  # 供异常事件控件使用的 Qt 应用
    """
    application = QApplication.instance()
    return application or QApplication([])


def test_desktop_startup_initializes_empty_recovery_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qt_application: QApplication,
) -> None:
    """确认桌面启动时会在新运行库中建立异常事件表。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        qt_application: 测试期间保持存活的 Qt 应用。

    Returns:
        返回示例：
            None  # 新运行库和异常事件表已建立
    """
    # 指向尚不存在的业务库和运行库，替换窗口事件循环。
    recovery_database_path = tmp_path / "recovery" / "events.sqlite3"
    settings = {
        "database_path": tmp_path / "business" / "measurements.sqlite3",
        "recovery_database_path": recovery_database_path,
        "evidence_directory": tmp_path / "evidence",
        "mvs_development_directory": tmp_path / "mvs",
    }
    application = Mock()
    application.exec.return_value = 0
    monkeypatch.setattr(
        desktop_entry, "read_configuration_settings", Mock(return_value=settings)
    )
    monkeypatch.setattr(desktop_entry, "QApplication", Mock(return_value=application))
    monkeypatch.setattr(desktop_entry, "MainWindow", Mock())

    # 运行桌面入口并核对新运行库中的现有表结构。
    assert not recovery_database_path.exists()
    assert desktop_entry.run_desktop_preview() == 0
    assert recovery_database_path.parent.is_dir()
    with closing(sqlite3.connect(recovery_database_path)) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'abnormal_events'"
        ).fetchone()
    assert table == ("abnormal_events",)
    assert AbnormalEventService(
        AbnormalEventRepo(recovery_database_path)
    ).list_events()["events"] == []

    # 打开新库对应的异常页面，确认空列表可以正常显示。
    abnormal_event_service = AbnormalEventService(
        AbnormalEventRepo(recovery_database_path)
    )
    controller = AppController(Mock(), Mock(), abnormal_event_service, Path("config"))
    page = AbnormalEventsPage(controller)
    try:
        page.refresh_events()
        assert page.table.rowCount() == 0
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


@pytest.mark.parametrize(
    "reason",
    ("测量周期超时", "OCR 识别执行失败"),
)
def test_chinese_reason_passes_through_service(
    abnormal_event_service: AbnormalEventService,
    reason: str,
) -> None:
    """确认新写入的中文原因在查询结果中保持原值。

    Args:
        abnormal_event_service: 已保存异常事件的服务。
        reason: 待核对的中文异常原因。

    Returns:
        返回示例：
            None  # 查询结果直接返回已写入的中文原因
    """
    # 写入中文原因并通过正式查询服务读取。
    session_id = f"session-{reason}"
    abnormal_event_service.abnormal_event_repo.insert(
        300.0, "1", session_id, reason, "{}"
    )
    events = abnormal_event_service.list_events(session_id=session_id)["events"]

    # 核对列表和详情均保留原始中文原因。
    assert len(events) == 1
    assert events[0]["reason"] == reason
    detail = abnormal_event_service.get_event(events[0]["abnormal_event_id"])["event"]
    assert detail["reason"] == reason


def test_service_filters_events_and_preserves_reason(
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证时间排序、精确搜索和历史英文原因原样返回。

    Args:
        abnormal_event_service: 已保存三条异常事件的服务。

    Returns:
        返回示例：
            None  # 查询结果符合筛选条件且历史原因原样保留
    """
    # 核对机器选项和默认时间倒序。
    assert abnormal_event_service.list_machine_ids()["machine_ids"] == ["1", "2"]
    events = abnormal_event_service.list_events()["events"]
    assert [event["created_at"] for event in events] == [200.0, 150.0, 100.0]
    assert events[0]["reason"] == "CUSTOM_REASON"
    assert events[1]["reason"] == "CAPTURE_FAILED"

    # 核对机器过滤与完整 Session ID 精确匹配。
    machine_events = abnormal_event_service.list_events(machine_id="1")["events"]
    assert [event["session_id"] for event in machine_events] == [
        "session-other", SESSION_ID
    ]
    target_events = abnormal_event_service.list_events(
        machine_id="1", session_id=SESSION_ID
    )["events"]
    assert len(target_events) == 1
    assert target_events[0]["reason"] == "OCR_TIMEOUT"
    assert abnormal_event_service.list_events(session_id=SESSION_ID[:8])["events"] == []

    # 按主键读取详情，确认历史英文原因和 payload 原样保留。
    target_event = abnormal_event_service.get_event(
        target_events[0]["abnormal_event_id"]
    )["event"]
    assert target_event["reason"] == "OCR_TIMEOUT"
    assert target_event["payload_json"] == '{"session_errors": ["OCR_TIMEOUT"]}'


def test_page_filters_and_shows_full_payload(
    qt_application: QApplication,
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证列表和详情直接显示历史及新写入的原因。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        abnormal_event_service: 已保存三条异常事件的服务。

    Returns:
        返回示例：
            None  # 页面展示目标异常与完整 payload
    """
    controller = AppController(Mock(), Mock(), abnormal_event_service, Path("config"))
    page = AbnormalEventsPage(controller)
    try:
        # 首次刷新按发生时间倒序展示全部异常。
        subtitle = "查看测量过程中的异常事件和原始信息"
        assert page.findChild(QLabel, "pageSubtitle").text() == subtitle
        assert PAGES["abnormal_events"][1] == subtitle
        page.refresh_events()
        assert page.table.rowCount() == 3
        assert page.table.horizontalHeaderItem(3).text() == "异常原因"
        assert page.table.item(2, 3).text() == "OCR_TIMEOUT"
        assert page.table.item(0, 3).text() == "CUSTOM_REASON"

        # 选择机器并按完整 Session ID 定位目标记录。
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))
        assert page.table.rowCount() == 2
        page.session_search.setText(SESSION_ID)
        page.reload_events()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 2).text() == SESSION_ID
        assert page.table.item(0, 3).text() == "OCR_TIMEOUT"

        # 打开历史详情并核对英文原因和完整 payload。
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["reason"].text() == "OCR_TIMEOUT"
        assert set(page.detail_values) == {
            "created_at", "machine_id", "session_id", "reason"
        }
        assert page.detail_values["machine_id"].text() == "1"
        assert page.detail_values["session_id"].text() == SESSION_ID
        assert page.detail_payload.toPlainText() == (
            '{"session_errors": ["OCR_TIMEOUT"]}'
        )
        assert page.detail_payload.isReadOnly()

        # 新写入的中文原因在刷新后的列表和详情中直接显示。
        page.detail_dialog.close()
        abnormal_event_service.abnormal_event_repo.insert(
            300.0, "1", "new-session", "测量周期超时", "{}"
        )
        page.session_search.clear()
        page.refresh_events()
        assert page.table.item(0, 3).text() == "测量周期超时"
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["reason"].text() == "测量周期超时"
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()
