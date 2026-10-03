"""验证异常事件查询、中文原因和页面详情。"""

import json
import os
import sqlite3
import time as system_time
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from repo.abnormal_event_repo import AbnormalEventRepo
from src.controller.controller import AppController, Result
from src.service.abnormal_event_service import AbnormalEventService, format_event_summary
import ui.__main__ as desktop_entry
from ui.date_range_picker import DateRangePicker
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
        assert page.table.item(2, 3).text() == "OCR 识别超时"
        assert page.event_count_label.text() == "3 条事件"
        assert page.empty_label.isHidden()

        # 切换机器立即查询，打开日期草稿不产生查询。
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))
        assert page.machine_filter.currentText() == "一号皮带机"
        assert page.table.rowCount() == 2
        assert page.time_filter_button.isEnabled()
        assert page.time_filter_button.text() == "时间范围  ▾"
        event_controller.list_abnormal_events = Mock(wraps=event_controller.list_abnormal_events)
        page.time_filter_button.click()
        event_controller.list_abnormal_events.assert_not_called()
        page.time_filter_panel.close()

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
        event_controller.list_abnormal_events.assert_called_once_with("1", None, start_date=None, end_date=None)
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


@pytest.mark.parametrize(
    ("timezone_name", "selected_date", "utc_start", "utc_end"),
    (
        ("UTC", date(2026, 9, 27), "2026-09-27T00:00:00+00:00", "2026-09-28T00:00:00+00:00"),
        ("Asia/Shanghai", date(2026, 9, 27), "2026-09-26T16:00:00+00:00", "2026-09-27T16:00:00+00:00"),
        ("America/New_York", date(2026, 3, 8), "2026-03-08T05:00:00+00:00", "2026-03-09T04:00:00+00:00"),
        ("America/New_York", date(2026, 11, 1), "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
    ),
)
@pytest.mark.skipif(not hasattr(system_time, "tzset"), reason="当前系统不支持测试进程切换本地时区")
def test_service_combines_local_record_dates_machine_and_session(
    abnormal_event_service: AbnormalEventService,
    event_controller: AppController,
    monkeypatch: pytest.MonkeyPatch,
    timezone_name: str,
    selected_date: date,
    utc_start: str,
    utc_end: str,
) -> None:
    """验证本地日期边界、夏令时、组合筛选和同时间稳定排序。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。
        event_controller: 使用该服务的真实控制器。
        monkeypatch: pytest 提供的环境变量替换工具。
        timezone_name: 测试进程使用的本地时区。
        selected_date: 需要查询的本地自然日。
        utc_start: 该自然日起点对应的 UTC 时间。
        utc_end: 下一自然日起点对应的 UTC 时间。

    Returns:
        返回示例：
            None  # 起点包含、终点排除，日期与机器和精确周期条件同时生效
    """
    # 临时切换运行时区并按已知 UTC 边界准备记录。
    with monkeypatch.context() as timezone_patch:
        timezone_patch.setenv("TZ", timezone_name)
        system_time.tzset()
        try:
            start_timestamp = datetime.fromisoformat(utc_start).timestamp()
            end_timestamp = datetime.fromisoformat(utc_end).timestamp()
            middle_timestamp = (start_timestamp + end_timestamp) / 2
            repo = abnormal_event_service.abnormal_event_repo
            for timestamp, machine_id, session_id in (
                (start_timestamp - 0.001, "1", "before"),
                (start_timestamp, "1", "lower"),
                (middle_timestamp, "1", "middle"),
                (end_timestamp - 0.001, "1", "upper-inside"),
                (end_timestamp, "1", "after"),
                (middle_timestamp, "2", "other-machine"),
                (middle_timestamp, "1", "tie"),
            ):
                repo.insert(timestamp, machine_id, session_id, "测量周期超时", "{}")

            # 控制器保留原有空白清理，日期条件在数据库中与机器组合。
            result = event_controller.list_abnormal_events(
                " 1 ", " ", start_date=selected_date, end_date=selected_date
            )
            assert result.success
            assert [event["session_id"] for event in result.data["events"]] == [
                "upper-inside", "tie", "middle", "lower"
            ]
            all_machines = abnormal_event_service.list_events(start_date=selected_date, end_date=selected_date)
            assert len(all_machines["events"]) == 5

            # 完整周期编号继续精确匹配，SQL 参数不会被当作语句。
            matched = abnormal_event_service.list_events("1", "middle", selected_date, selected_date)
            assert [event["session_id"] for event in matched["events"]] == ["middle"]
            assert abnormal_event_service.list_events("1", "midd", selected_date, selected_date)["events"] == []
            injected_query = abnormal_event_service.list_events("1' OR 1=1 --", None, selected_date, selected_date)
            assert injected_query["events"] == []

            # 后端允许只设一侧边界，不传日期时仍返回原有全部记录。
            lower_only = abnormal_event_service.list_events("1", start_date=selected_date)["events"]
            assert all(event["created_at"] >= start_timestamp for event in lower_only)
            assert lower_only[0]["session_id"] == "after"
            upper_only = abnormal_event_service.list_events("1", end_date=selected_date)["events"]
            assert all(event["created_at"] < end_timestamp for event in upper_only)
            assert len(abnormal_event_service.list_events()["events"]) == 10
        finally:
            timezone_patch.undo()
            system_time.tzset()


def test_repo_applies_zero_timestamp_boundary(abnormal_event_service: AbnormalEventService) -> None:
    """验证零时间戳作为真实边界，不被忽略。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。

    Returns:
        返回示例：
            None  # 仅返回包含下界且小于上界的记录
    """
    repo = abnormal_event_service.abnormal_event_repo
    repo.insert(-1.0, "1", "before-epoch", "历史记录", "{}")
    repo.insert(0.0, "1", "at-epoch", "历史记录", "{}")
    events = repo.list_events("1", start_created_at=0.0, end_created_at=100.0)
    assert [event["session_id"] for event in events] == ["at-epoch"]


@pytest.mark.parametrize(
    ("payload_json", "expected_summary"),
    (
        ('{"session_errors": ["OCR_TIMEOUT", "FREQUENCY_NO_VALID_MEASUREMENT"]}', "OCR 识别超时；没有有效频率读数"),
        ('{"session_errors": ["StopGrabbing 失败", "相机采集失败"]}', "StopGrabbing 失败；相机采集失败"),
        ('{"session_errors": ["FUTURE_ERROR", null, 3, {}]}', "FUTURE_ERROR"),
        ('{"message": "相机\\n断开"}', "相机 断开"),
        ('{"event_type": "MachineClosed", "payload": null}', "事件：机器关闭"),
        ('{"event_type": "OCRFailed", "payload": "模型执行失败"}', "事件：OCR 识别执行失败；模型执行失败"),
        ('{"event_type": "FutureEvent", "payload": {"message": "已保存的消息"}}', "事件：FutureEvent；已保存的消息"),
        ('{"event_type": [], "session_errors": "错误字段格式", "message": {}}', "其他事件内容，请查看原始数据"),
        ('{"other": [1, 2, 3]}', "其他事件内容，请查看原始数据"),
        ('["unknown", "structure"]', "其他事件内容，请查看原始数据"),
        ("null", "其他事件内容，请查看原始数据"),
        ("{}", "无附加信息"),
        ('"已保存的文本"', "已保存的文本"),
        ("not JSON", "原始内容格式异常，请查看原始数据"),
        ("", "原始内容格式异常，请查看原始数据"),
    ),
)
def test_event_summary_reads_known_fields_and_handles_unknown_payload(
    payload_json: str,
    expected_summary: str,
) -> None:
    """验证可读摘要只使用已保存内容，并容忍未知和损坏内容。

    Args:
        payload_json: 需要解析的历史或运行事件内容。
        expected_summary: 预期的单行可读摘要。

    Returns:
        返回示例：
            None  # 摘要符合已保存字段，未知内容不影响列表加载
    """
    assert format_event_summary(payload_json) == expected_summary


def test_summary_supports_real_frequency_payload_and_preserves_raw_detail(
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证真实运行事件序列化后的频率摘要和未改写的原始详情。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。

    Returns:
        返回示例：
            None  # 摘要显示已保存的频率和序列号，原因和原始 JSON 完整保留
    """
    from database import serialize_value
    from enums import EventType
    from models import FrequencyMeasurement, RuntimeEvent

    # 使用运行时实际数据结构生成保存的事件 JSON。
    event = RuntimeEvent(
        EventType.FREQUENCY_MEASURED,
        "1",
        SESSION_ID,
        FrequencyMeasurement(SESSION_ID, "FM01", 0.0),
    )
    raw_json = json.dumps(serialize_value(event), ensure_ascii=False, indent=2)
    event_id = abnormal_event_service.abnormal_event_repo.insert(300.0, "1", SESSION_ID, "迟到的频率读数", raw_json)

    # 列表使用真实读数，不把零值当成缺失，也不改写原因和内容。
    events = abnormal_event_service.list_events(session_id=SESSION_ID)["events"]
    assert events[0]["payload_summary"] == "事件：收到频率读数；频率：0.0 Hz；频率仪：FM01"
    assert events[0]["reason"] == "迟到的频率读数"
    assert events[0]["payload_json"] == raw_json
    assert abnormal_event_service.get_event(event_id)["event"]["payload_json"] == raw_json


def test_summary_limits_length_without_changing_saved_payload(abnormal_event_service: AbnormalEventService) -> None:
    """验证长摘要被截断而完整原始内容仍可读取。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。

    Returns:
        返回示例：
            None  # 摘要最多八十字，列表和详情中的原始内容没有改动
    """
    raw_json = json.dumps({"message": "采集错误" * 100}, ensure_ascii=False, indent=2)
    event_id = abnormal_event_service.abnormal_event_repo.insert(400.0, "1", SESSION_ID, "原因保持原值", raw_json)
    event = abnormal_event_service.list_events()["events"][0]
    assert len(event["payload_summary"]) == 80
    assert event["payload_summary"].endswith("…")
    assert event["payload_json"] == raw_json
    assert abnormal_event_service.get_event(event_id)["event"]["payload_json"] == raw_json


def test_page_applies_date_draft_with_machine_and_clears_only_dates(
    qt_application: QApplication,
    abnormal_event_service: AbnormalEventService,
    event_controller: AppController,
) -> None:
    """验证单层日期草稿、组合查询、刷新保留和只清除日期。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        abnormal_event_service: 使用临时数据库的异常事件服务。
        event_controller: 可查询真实临时记录的控制器。

    Returns:
        返回示例：
            None  # 应用和清除各查询一次，刷新和切换机器保留已应用日期
    """
    # 在两日范围内、范围外及另一台机器上准备异常记录。
    start_date = date(2026, 9, 26)
    end_date = date(2026, 9, 27)
    repo = abnormal_event_service.abnormal_event_repo
    for record_date, machine_id, session_id in (
        (start_date, "1", "range-first"),
        (end_date, "1", "range-last"),
        (end_date + timedelta(days=1), "1", "excluded-next"),
        (start_date, "2", "other-machine"),
    ):
        timestamp = datetime.combine(record_date, time(1)).astimezone(timezone.utc).timestamp()
        repo.insert(timestamp, machine_id, session_id, session_id, "{}")
    page = AbnormalEventsPage(event_controller)
    try:
        # 先选择机器，再打开日期草稿面板。
        page.refresh_events()
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))
        page.show()
        qt_application.processEvents()
        query_events = Mock(wraps=event_controller.list_abnormal_events)
        event_controller.list_abnormal_events = query_events
        page.time_filter_button.click()
        panel = page.time_filter_panel
        picker = panel.view.findChild(DateRangePicker)
        assert panel.windowType() == Qt.WindowType.Tool
        assert picker.start_date == QDate.currentDate()
        assert picker.end_date == QDate.currentDate()

        # 日历点击只修改草稿，同层日历保持打开。
        picker.calendar._onDayItemClicked(QDate(start_date))
        picker.end_button.click()
        picker.calendar._onDayItemClicked(QDate(end_date))
        assert panel.isVisible()
        assert page.time_filter_panel is panel
        assert picker.start_date == QDate(start_date)
        assert picker.end_date == QDate(end_date)
        assert page.selected_start_date is None
        query_events.assert_not_called()

        # 确定后只发起一次机器与日期组合查询。
        apply_button = next(button for button in panel.view.findChildren(QPushButton) if button.text() == "确定")
        apply_button.click()
        query_events.assert_called_once_with("1", None, start_date=start_date, end_date=end_date)
        assert page.time_filter_panel is None
        assert page.table.rowCount() == 2
        assert [page.table.item(row, 2).text() for row in range(2)] == ["range-last", "range-first"]
        assert page.time_filter_button.text() == "2026-09-26 ～ 2026-09-27  ▾"

        # 刷新记录仍使用当前机器和日期条件。
        timestamp = datetime.combine(end_date, time(2)).astimezone(timezone.utc).timestamp()
        repo.insert(timestamp, "1", "new-in-range", "范围内新增", "{}")
        query_events.reset_mock()
        page.refresh_button.click()
        query_events.assert_called_once_with("1", None, start_date=start_date, end_date=end_date)
        assert page.machine_filter.currentData() == "1"
        assert page.table.rowCount() == 3
        assert page.table.item(0, 2).text() == "范围内新增"

        # 切换机器继续使用已应用日期，再切回原机器。
        query_events.reset_mock()
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("2"))
        query_events.assert_called_once_with("2", None, start_date=start_date, end_date=end_date)
        assert page.table.rowCount() == 1
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))

        # 再打开恢复已应用草稿，清除日期不会改变机器选择。
        page.time_filter_button.click()
        panel = page.time_filter_panel
        picker = panel.view.findChild(DateRangePicker)
        assert picker.start_date == QDate(start_date)
        assert picker.end_date == QDate(end_date)
        query_events.reset_mock()
        clear_button = next(button for button in panel.view.findChildren(QPushButton) if button.text() == "清除时间")
        clear_button.click()
        query_events.assert_called_once_with("1", None, start_date=None, end_date=None)
        assert page.time_filter_panel is None
        assert page.machine_filter.currentData() == "1"
        assert page.selected_start_date is None
        assert page.selected_end_date is None
        assert page.time_filter_button.text() == "时间范围  ▾"
        assert page.table.rowCount() == 6
    finally:
        if page.time_filter_panel is not None:
            page.time_filter_panel.close()
        page.detail_dialog.close()
        page.close()
        page.deleteLater()
        qt_application.processEvents()


@pytest.mark.parametrize("dismiss_action", ("cancel", "escape", "outside", "toggle", "hide"))
def test_page_discards_unapplied_date_draft_and_reopens_applied_range(
    qt_application: QApplication,
    event_controller: AppController,
    dismiss_action: str,
) -> None:
    """验证日期草稿取消、重复打开和页面离开时不触发查询。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        event_controller: 可查询真实临时记录的控制器。
        dismiss_action: 关闭日期草稿的控件交互。

    Returns:
        返回示例：
            None  # 日期草稿被丢弃，旧面板通知不会影响新面板
    """
    page = AbnormalEventsPage(event_controller)
    try:
        # 保留已应用机器与日期，再打开并修改草稿。
        page.refresh_events()
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("1"))
        page.apply_time_filter(date(2026, 9, 26), date(2026, 9, 27))
        assert page.empty_label.text() == "当前筛选条件下暂无异常事件"
        page.show()
        qt_application.processEvents()
        query_events = Mock(wraps=event_controller.list_abnormal_events)
        event_controller.list_abnormal_events = query_events
        page.time_filter_button.click()
        panel = page.time_filter_panel
        picker = panel.view.findChild(DateRangePicker)
        picker.calendar._onDayItemClicked(QDate(2026, 9, 29))
        assert picker.start_date == QDate(2026, 9, 29)
        assert picker.end_date == QDate(2026, 9, 29)

        # 使用控件事件取消草稿或离开页面。
        if dismiss_action == "cancel":
            cancel_button = next(button for button in panel.view.findChildren(QPushButton) if button.text() == "取消")
            cancel_button.click()
        elif dismiss_action == "escape":
            QTest.keyClick(panel, Qt.Key.Key_Escape)
        elif dismiss_action == "outside":
            QTest.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(-10, -10))
        elif dismiss_action == "toggle":
            page.time_filter_button.click()
        else:
            page.hide()
        query_events.assert_not_called()
        assert not panel.isVisible()
        assert page.time_filter_panel is None
        assert page.machine_filter.currentData() == "1"
        assert page.selected_start_date == date(2026, 9, 26)
        assert page.selected_end_date == date(2026, 9, 27)

        # 重新打开恢复已应用日期，旧面板通知只影响旧实例。
        page.show()
        page.time_filter_button.click()
        current_panel = page.time_filter_panel
        picker = current_panel.view.findChild(DateRangePicker)
        assert picker.start_date == QDate(2026, 9, 26)
        assert picker.end_date == QDate(2026, 9, 27)
        panel.closed.emit()
        assert page.time_filter_panel is current_panel
        assert current_panel.isVisible()
        query_events.assert_not_called()
    finally:
        if page.time_filter_panel is not None:
            page.time_filter_panel.close()
        page.detail_dialog.close()
        page.close()
        page.deleteLater()
        qt_application.processEvents()
