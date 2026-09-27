"""验证异常事件查询、中文原因和页面详情。"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from repo.abnormal_event_repo import AbnormalEventRepo
from src.service.abnormal_event_service import AbnormalEventService
from ui.pages.abnormal_events_page import AbnormalEventsPage


SESSION_ID = "5ad75ed720044b2a9da465055e8ea424"


@pytest.fixture
def abnormal_event_service(tmp_path: Path) -> AbnormalEventService:
    """建立包含多台机器和多种原因码的异常事件表。

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


def test_service_filters_events_and_preserves_reason(
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证时间排序、精确搜索、中文映射和未知原因回退。

    Args:
        abnormal_event_service: 已保存三条异常事件的服务。

    Returns:
        返回示例：
            None  # 查询结果符合筛选条件且原始原因码未改动
    """
    # 核对机器选项和默认时间倒序。
    assert abnormal_event_service.list_machine_ids() == ["1", "2"]
    events = abnormal_event_service.list_events()
    assert [event["created_at"] for event in events] == [200.0, 150.0, 100.0]
    assert events[0]["reason_label"] == "CUSTOM_REASON"
    assert events[1]["reason_label"] == "相机采集失败"

    # 核对机器过滤与完整 Session ID 精确匹配。
    machine_events = abnormal_event_service.list_events(machine_id="1")
    assert [event["session_id"] for event in machine_events] == [
        "session-other", SESSION_ID
    ]
    target_events = abnormal_event_service.list_events(
        machine_id="1", session_id=SESSION_ID
    )
    assert len(target_events) == 1
    assert target_events[0]["reason"] == "OCR_TIMEOUT"
    assert target_events[0]["reason_label"] == "OCR 识别超时"
    assert abnormal_event_service.list_events(session_id=SESSION_ID[:8]) == []

    # 按主键读取详情，确认原始 payload 和原因码保留。
    target_event = abnormal_event_service.get_event(
        target_events[0]["abnormal_event_id"]
    )
    assert target_event["reason"] == "OCR_TIMEOUT"
    assert target_event["payload_json"] == '{"session_errors": ["OCR_TIMEOUT"]}'


def test_page_filters_and_shows_full_payload(
    qt_application: QApplication,
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证列表中文原因、筛选和详情原始信息。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        abnormal_event_service: 已保存三条异常事件的服务。

    Returns:
        返回示例：
            None  # 页面展示目标异常与完整 payload
    """
    page = AbnormalEventsPage(abnormal_event_service)
    try:
        # 首次刷新按发生时间倒序展示全部异常。
        page.refresh_events()
        assert page.table.rowCount() == 3
        assert page.table.horizontalHeaderItem(3).text() == "异常原因"
        assert page.table.item(2, 3).text() == "OCR 识别超时"
        assert page.table.item(0, 3).text() == "CUSTOM_REASON"

        # 选择机器并按完整 Session ID 定位目标记录。
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))
        assert page.table.rowCount() == 2
        page.session_search.setText(SESSION_ID)
        page.reload_events()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 2).text() == SESSION_ID
        assert page.table.item(0, 3).text() == "OCR 识别超时"

        # 打开详情并核对中文原因、原因码和完整 payload。
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["reason_label"].text() == "OCR 识别超时"
        assert page.detail_values["reason"].text() == "OCR_TIMEOUT"
        assert page.detail_values["machine_id"].text() == "1"
        assert page.detail_values["session_id"].text() == SESSION_ID
        assert page.detail_payload.toPlainText() == (
            '{"session_errors": ["OCR_TIMEOUT"]}'
        )
        assert page.detail_payload.isReadOnly()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()
