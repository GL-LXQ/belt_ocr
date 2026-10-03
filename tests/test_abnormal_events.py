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
from src.controller.controller import AppController, Result
from src.service.abnormal_event_service import AbnormalEventService
import ui.__main__ as desktop_entry
from ui.pages.abnormal_events_page import AbnormalEventsPage, format_event_time


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
    machine_service = Mock()
    machine_service.list_machines.return_value = {"machines": []}
    controller = AppController(machine_service, Mock(), abnormal_event_service, Path("config"))
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


@pytest.fixture
def event_controller(abnormal_event_service: AbnormalEventService) -> AppController:
    """建立可以读取真实异常记录和机器名称的控制器。

    Args:
        abnormal_event_service: 已保存异常事件的服务。

    Returns:
        返回示例：
            AppController(...)  # 提供机器 1 的名称，机器 2 使用编号回退
    """
    machine_service = Mock()
    machine_service.list_machines.return_value = {
        "machines": [
            {
                "id": 1,  # 机器编号
                "machine_name": "一号皮带机",  # 机器名称
            },
        ],
    }
    return AppController(machine_service, Mock(), abnormal_event_service, Path("config"))


def test_page_filters_and_shows_full_payload(
    qt_application: QApplication,
    abnormal_event_service: AbnormalEventService,
    event_controller: AppController,
) -> None:
    """验证机器筛选、原始原因和按事件主键读取详情。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        abnormal_event_service: 已保存三条异常事件的服务。
        event_controller: 可读取机器名称和真实异常记录的控制器。

    Returns:
        返回示例：
            None  # 列表、机器筛选和详情均使用真实记录
    """
    page = AbnormalEventsPage(event_controller)
    try:
        # 首次刷新保留记录时间倒序，名称不存在时回退到编号。
        page.refresh_events()
        assert page.findChild(QLabel, "pageSubtitle") is None
        assert not hasattr(page, "session_search")
        assert page.table.rowCount() == 3
        assert [page.table.horizontalHeaderItem(column).text() for column in range(5)] == [
            "机器", "记录时间", "异常原因", "摘要", "详情"
        ]
        assert page.table.item(0, 0).text() == "2"
        assert page.table.item(2, 0).text() == "一号皮带机"
        assert page.table.item(2, 1).text() == format_event_time(100.0)
        assert page.table.item(2, 2).text() == "OCR_TIMEOUT"
        assert page.table.item(0, 2).text() == "CUSTOM_REASON"
        assert page.table.item(2, 3).text() == '{"session_errors": ["OCR_TIMEOUT"]}'
        assert page.event_count_label.text() == "3 条事件"
        assert page.empty_label.isHidden()

        # 切换机器立即查询，禁用的日期入口不会产生查询。
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))
        assert page.machine_filter.currentText() == "一号皮带机"
        assert page.table.rowCount() == 2
        assert not page.time_filter_button.isEnabled()
        assert "待接入" in page.time_filter_button.text()
        event_controller.list_abnormal_events = Mock(wraps=event_controller.list_abnormal_events)
        page.time_filter_button.click()
        event_controller.list_abnormal_events.assert_not_called()

        # 按第二条异常的主键读取详情，显示原始周期编号与内容。
        page.table.cellWidget(1, 4).click()
        assert page.detail_values["reason"].text() == "OCR_TIMEOUT"
        assert page.detail_values["machine_name"].text() == "一号皮带机"
        assert page.detail_values["machine_id"].text() == "1"
        assert page.detail_values["session_id"].text() == SESSION_ID
        assert page.detail_payload.toPlainText() == '{"session_errors": ["OCR_TIMEOUT"]}'
        assert page.detail_payload.isReadOnly()
        assert page.payload_container.isHidden()
        assert not page.payload_toggle_button.isChecked()
        page.copy_session_button.click()
        assert qt_application.clipboard().text() == SESSION_ID

        # 刷新保留机器选择并显示新增的中文异常原因。
        page.detail_dialog.close()
        abnormal_event_service.abnormal_event_repo.insert(300.0, "1", "new-session", "测量周期超时", "{}")
        page.refresh_button.click()
        event_controller.list_abnormal_events.assert_called_once_with("1", None)
        assert page.machine_filter.currentData() == "1"
        assert page.table.item(0, 2).text() == "测量周期超时"
        page.table.cellWidget(0, 4).click()
        assert page.detail_values["reason"].text() == "测量周期超时"
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_page_collapses_each_detail_and_copies_original_json(
    qt_application: QApplication,
    abnormal_event_service: AbnormalEventService,
    event_controller: AppController,
) -> None:
    """验证原始数据重复展开、切换详情复位和逐字复制。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        abnormal_event_service: 已保存异常事件的服务。
        event_controller: 可读取机器名称和真实异常记录的控制器。

    Returns:
        返回示例：
            None  # 原始 JSON 复制未改写，切换详情后恢复折叠状态
    """
    raw_json = '{\r\n  "message": "相机\\n断开", "session_errors": []\r\n}'
    abnormal_event_service.abnormal_event_repo.insert(350.0, None, None, "<设备异常>", raw_json)
    page = AbnormalEventsPage(event_controller)
    try:
        # 缺失机器与周期时使用占位，并禁用周期复制。
        page.refresh_events()
        assert page.table.item(0, 0).text() == "--"
        page.table.cellWidget(0, 4).click()
        assert page.detail_values["reason"].text() == "<设备异常>"
        assert page.detail_values["machine_name"].text() == "--"
        assert page.detail_values["session_id"].text() == "--"
        assert not page.copy_session_button.isEnabled()

        # 展开和复制只读数据，保留原始换行和 JSON 转义。
        page.payload_toggle_button.click()
        assert not page.payload_container.isHidden()
        page.copy_payload_button.click()
        assert qt_application.clipboard().text() == raw_json
        page.payload_toggle_button.click()
        assert page.payload_container.isHidden()
        page.payload_toggle_button.click()
        assert not page.payload_container.isHidden()

        # 再次打开同一记录或其他记录都从折叠状态开始。
        page.detail_dialog.close()
        page.table.cellWidget(0, 4).click()
        assert page.payload_container.isHidden()
        page.detail_dialog.close()
        page.table.cellWidget(1, 4).click()
        assert page.payload_container.isHidden()
        assert page.detail_values["machine_id"].text() == "2"
        assert page.detail_values["session_id"].text() == "session-two"
        assert page.current_payload_json == '{"message": "未知原因"}'
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_page_handles_empty_results_and_name_lookup_failure(
    qt_application: QApplication,
    event_controller: AppController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证名称读取失败回退及空列表、读取失败的提示。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        event_controller: 可读取机器名称和真实异常记录的控制器。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 名称失败仍可读取事件，空结果和读取失败不会混淆
    """
    page = AbnormalEventsPage(event_controller)
    try:
        # 机器名称读取失败不阻断事件查询。
        event_controller.list_machines = Mock(return_value=Result.error("机器读取失败"))
        page.refresh_events()
        assert page.table.rowCount() == 3
        assert page.table.item(2, 0).text() == "1"
        assert page.machine_filter.itemText(page.machine_filter.findData("1")) == "1"

        # 正常空结果清除旧行并显示零条。
        event_controller.list_abnormal_events = Mock(return_value=Result.ok({"events": []}))
        page.reload_events()
        assert page.table.rowCount() == 0
        assert page.event_count_label.text() == "0 条事件"
        assert not page.empty_label.isHidden()
        assert page.empty_label.text() == "暂无异常事件"

        # 读取错误清空旧列表，并与正常空结果使用不同提示。
        error_bar = Mock()
        monkeypatch.setattr("ui.pages.abnormal_events_page.InfoBar.error", error_bar)
        event_controller.list_abnormal_events.return_value = Result.error("异常事件读取失败")
        page.reload_events()
        assert page.event_count_label.text() == "-- 条事件"
        assert "读取失败" in page.empty_label.text()
        error_bar.assert_called_once()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()
