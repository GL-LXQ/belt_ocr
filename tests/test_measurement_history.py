"""验证测量历史的查询、筛选、证据和人工复核。"""

import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from config_util import AppConfig
from database import Database, MeasurementRecord
from src.controller.controller import AppController, Result
from repo.machine_repo import MachineRepo
from repo.abnormal_event_repo import AbnormalEventRepo
from repo.measurement_record_repo import MeasurementRecordRepo
from src.service.abnormal_event_service import AbnormalEventService
from src.service.measurement_record_service import (
    MeasurementRecordService,
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)
from src.service.machine_service import MachineService
from ui.main_window import MainWindow
from ui.pages.history_page import HistoryPage


@pytest.fixture
def measurement_record_service(tmp_path: Path) -> MeasurementRecordService:
    """建立含正常、待复核和历史机器状态的业务库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            MeasurementRecordService(...)  # 已保存四条测量记录的读取服务
    """
    # 使用现有数据库入口创建业务表和三台机器。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    database = Database(config)
    database.initialize_result_database()
    machine_repo = MachineRepo(config.database_path)
    first_machine_id = machine_repo.insert("一号皮带", "camera-1", "meter-1")
    second_machine_id = machine_repo.insert("二号皮带", "camera-2", "meter-2")
    third_machine_id = machine_repo.insert(
        "三号皮带", "camera-3", "meter-3", enabled=False
    )
    machine_repo.soft_delete(second_machine_id)

    # 写入正常、待复核、停用机器和缺少机器信息的历史记录。
    records = (
        MeasurementRecord(
            machine_id=str(first_machine_id),
            session_id="normal-session",
            start_time="2026-09-27T08:00:00+00:00",
            finish_time="2026-09-27T08:01:00+00:00",
            ordered_lines=("12345678", "003"),
            final_frequency_hz=50.0,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "normal-evidence",
            needs_review=False,
            review_reason=None,
        ),
        MeasurementRecord(
            machine_id=str(second_machine_id),
            session_id="review-session",
            start_time="2026-09-27T09:00:00+00:00",
            finish_time="2026-09-27T09:01:00+00:00",
            ordered_lines=("待确认文字",),
            final_frequency_hz=None,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "review-evidence",
            needs_review=True,
            review_reason="没有可靠的 20 位文字",
        ),
        MeasurementRecord(
            machine_id=str(third_machine_id),
            session_id="disabled-machine-session",
            start_time="2026-09-27T09:30:00+00:00",
            finish_time="2026-09-27T09:31:00+00:00",
            ordered_lines=("停用机器记录",),
            final_frequency_hz=25.0,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "disabled-machine-evidence",
            needs_review=False,
            review_reason=None,
        ),
        MeasurementRecord(
            machine_id="99",
            session_id="missing-machine-session",
            start_time="2026-09-27T10:00:00+00:00",
            finish_time="2026-09-27T10:01:00+00:00",
            ordered_lines=(),
            final_frequency_hz=0.0,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "missing-machine-evidence",
            needs_review=False,
            review_reason=None,
        ),
    )
    for record in records:
        database.write_measurement_record(record)
    return MeasurementRecordService(MeasurementRecordRepo(config.database_path))


@pytest.fixture
def paged_measurement_record_service(
    measurement_record_service: MeasurementRecordService,
) -> MeasurementRecordService:
    """在已有业务库中准备二十五条可分页记录。

    Args:
        measurement_record_service: 已建立业务库和机器信息的测量记录服务。

    Returns:
        返回示例：
            MeasurementRecordService(...)  # 已保存二十五条分页测试记录的服务
    """
    # 准备跨页的正常及待复核记录。
    database_path = measurement_record_service.measurement_record_repo.database_path
    records_to_insert = []
    for record_number in range(25):
        finish_second = 5 if record_number == 4 else record_number
        records_to_insert.append((
            f"page-{record_number:02}",
            "1" if record_number < 21 else "2",
            "2026-09-27T08:00:00+00:00",
            f"2026-09-27T08:00:{finish_second:02}+00:00",
            json.dumps([f"文字 {record_number:02}"], ensure_ascii=False),
            str(database_path.parent / f"page-{record_number:02}"),
            int(record_number < 21),
        ))

    # 替换原有测量记录。
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM measurement_records")
        connection.executemany(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, "
            "ordered_lines, evidence_directory, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            records_to_insert,
        )
    return measurement_record_service


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """创建历史记录页面测试所需的 Qt 应用。

    Args:
        无外部参数。

    Returns:
        返回示例：
            QApplication([])  # 供历史记录控件使用的 Qt 应用
    """
    application = QApplication.instance()
    return application or QApplication([])


def test_measurement_record_service_filters_and_reads_details(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证历史筛选、软删除机器和只读详情字段。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 筛选结果和详情字段均来自 measurement_records
    """
    # 查询全部、正常、待复核和指定机器的记录。
    records = measurement_record_service.list_records()["records"]
    assert [record["session_id"] for record in records] == [
        "missing-machine-session", "disabled-machine-session",
        "review-session", "normal-session"
    ]
    normal_records = measurement_record_service.list_records("normal")["records"]
    assert [record["session_id"] for record in normal_records] == [
        "missing-machine-session", "disabled-machine-session", "normal-session"
    ]
    review_records = measurement_record_service.list_records("pending")["records"]
    assert [record["session_id"] for record in review_records] == [
        "review-session"
    ]
    assert measurement_record_service.list_records("reviewed")["records"] == []
    assert measurement_record_service.list_records("pending", "1")["records"] == []
    machine_records = measurement_record_service.list_records("pending", "2")["records"]
    assert machine_records[0]["session_id"] == "review-session"
    disabled_records = measurement_record_service.list_records("normal", "3")["records"]
    assert disabled_records[0]["session_id"] == "disabled-machine-session"

    # 核对软删除机器和缺少机器信息时的展示名称。
    machines = measurement_record_service.list_record_machines()["machines"]
    machine_names = {
        machine["machine_id"]: machine["machine_name"] for machine in machines
    }
    assert machine_names == {
        "1": "一号皮带",
        "2": "二号皮带",
        "3": "三号皮带",
        "99": "99",
    }
    assert records[0]["machine_name"] == "99"

    # 读取详情并确认文字、空频率和复核原因。
    review_record = measurement_record_service.get_record("review-session")["record"]
    normal_record = measurement_record_service.get_record("normal-session")["record"]
    assert review_record["ordered_lines"] == ("待确认文字",)
    assert review_record["final_frequency_hz"] is None
    assert review_record["review_reason"] == "没有可靠的 20 位文字"
    assert normal_record["ordered_lines"] == ("12345678", "003")
    assert normal_record["review_reason"] is None
    assert measurement_record_service.get_record("unknown-session")["record"] is None


def test_measurement_records_use_database_pages_and_stable_time_order(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证数据库分页保留结束时间及周期编号的倒序。

    Args:
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 两页记录连续且分页总数准确
    """
    # 读取两页并核对记录数量与分页业务字段。
    first_page = paged_measurement_record_service.list_records(page=1)
    second_page = paged_measurement_record_service.list_records(page=2)
    assert len(first_page["records"]) == 20
    assert len(second_page["records"]) == 5
    pagination = {
        key: first_page[key]
        for key in ("page", "page_size", "total", "total_pages")
    }
    assert pagination == {
        "page": 1,
        "page_size": 20,
        "total": 25,
        "total_pages": 2,
    }
    assert second_page["page"] == 2

    # 核对页间顺序和结束时间相同的记录顺序。
    session_ids = [record["session_id"] for record in first_page["records"]]
    session_ids += [record["session_id"] for record in second_page["records"]]
    assert session_ids == [
        f"page-{record_number:02}" for record_number in reversed(range(25))
    ]
    assert first_page["records"][-1]["session_id"] == "page-05"
    assert second_page["records"][0]["session_id"] == "page-04"


def test_measurement_record_pages_count_only_filtered_records(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证状态和机器筛选的总数仅统计匹配记录。

    Args:
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 筛选后的页数和记录均按对应条件计算
    """
    # 核对跨页的待复核记录和指定机器的正常记录。
    pending_page = paged_measurement_record_service.list_records("pending", page=2)
    machine_page = paged_measurement_record_service.list_records("normal", "2")
    assert pending_page["total"] == 21
    assert pending_page["total_pages"] == 2
    assert [record["session_id"] for record in pending_page["records"]] == ["page-00"]
    assert machine_page["total"] == 4
    assert machine_page["total_pages"] == 1
    assert len(machine_page["records"]) == 4

    # 无匹配记录时保留第一页的显示页数。
    empty_page = paged_measurement_record_service.list_records("reviewed")
    assert empty_page["records"] == []
    assert empty_page["total"] == 0
    assert empty_page["total_pages"] == 1


def test_measurement_records_filter_by_local_day_boundaries(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证本地日期开始边界包含且次日开始边界排除。

    Args:
        measurement_record_service: 已建立业务库的测量记录服务。

    Returns:
        返回示例：
            None  # 查询仅返回所选本地日期内的记录
    """
    # 按当前系统本地时区计算所选日期的 UTC 边界。
    selected_date = date(2026, 9, 28)
    local_start = datetime.combine(selected_date, time.min).astimezone(timezone.utc)
    local_end = datetime.combine(
        selected_date + timedelta(days=1), time.min
    ).astimezone(timezone.utc)
    local_midday = datetime.combine(
        selected_date, time(12, 0)
    ).astimezone(timezone.utc)
    finish_times = {
        "before": local_start - timedelta(seconds=1),
        "start": local_start,
        "inside": local_midday,
        "after": local_end,
    }

    # 替换原有记录并写入边界两侧的 UTC 时间。
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM measurement_records")
        connection.executemany(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, "
            "ordered_lines, evidence_directory) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    "1",
                    finish_time.isoformat(),
                    finish_time.isoformat(),
                    '["文字"]',
                    str(database_path.parent / session_id),
                )
                for session_id, finish_time in finish_times.items()
            ],
        )

    # 查询单日本地日期并核对排他结束边界。
    page = measurement_record_service.list_records(
        start_date=selected_date, end_date=selected_date
    )
    assert page["total"] == 2
    assert page["total_pages"] == 1
    assert [record["session_id"] for record in page["records"]] == [
        "inside", "start"
    ]


def test_measurement_records_combine_date_status_machine_and_pagination(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证日期、状态、机器和分页使用相同筛选条件。

    Args:
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 总数和当前页仅包含同时匹配全部筛选条件的记录
    """
    # 在相同状态和机器下增加下一本地日期的记录。
    selected_date = datetime.fromisoformat(
        "2026-09-27T08:00:00+00:00"
    ).astimezone().date()
    next_day_start = datetime.combine(
        selected_date + timedelta(days=1), time.min
    ).astimezone(timezone.utc).isoformat()
    database_path = (
        paged_measurement_record_service.measurement_record_repo.database_path
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, "
            "ordered_lines, evidence_directory, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "outside-date",
                "1",
                next_day_start,
                next_day_start,
                '["其他日期"]',
                str(database_path.parent / "outside-date"),
                1,
            ),
        )

    # 同时应用全部筛选条件并核对两页结果与稳定排序。
    second_page = paged_measurement_record_service.list_records(
        "pending", "1", 2, 5, selected_date, selected_date
    )
    fourth_page = paged_measurement_record_service.list_records(
        "pending", "1", 4, 5, selected_date, selected_date
    )
    assert second_page["total"] == 21
    assert second_page["total_pages"] == 5
    assert second_page["page_size"] == 5
    assert [record["session_id"] for record in second_page["records"]] == [
        "page-15", "page-14", "page-13", "page-12", "page-11"
    ]
    assert [record["session_id"] for record in fourth_page["records"][:2]] == [
        "page-05", "page-04"
    ]


def test_existing_measurement_table_adds_review_columns_without_losing_records(
    tmp_path: Path,
) -> None:
    """验证旧开发库补列后保留已有测量记录。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 旧记录仍在且两个复核字段可重复初始化
    """
    database_path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE measurement_records (session_id TEXT PRIMARY KEY, "
            "ordered_lines TEXT NOT NULL, needs_review INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO measurement_records VALUES (?, ?, ?)",
            ("existing-session", '["原始文字"]', 1),
        )

        # 重复初始化时仅补齐缺失字段。
        MeasurementRecordRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)
        table_columns = connection.execute("PRAGMA table_info(measurement_records)")
        columns = {column[1] for column in table_columns}
        saved_record = connection.execute(
            "SELECT session_id, ordered_lines, needs_review, "
            "reviewed_at, reviewed_lines "
            "FROM measurement_records"
        ).fetchone()

    assert {"reviewed_at", "reviewed_lines"} <= columns
    assert saved_record == ("existing-session", '["原始文字"]', 1, None, None)


def test_confirm_original_ocr_preserves_original_record_and_prevents_repeat(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证确认原文字只写复核时间且同一记录不能再复核。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 原文字和复核原因保留，重复复核已拒绝
    """
    measurement_record_service.complete_review("review-session")
    record = measurement_record_service.get_record("review-session")["record"]
    reviewed_time = datetime.fromisoformat(record["reviewed_at"])
    assert reviewed_time.tzinfo == timezone.utc
    assert record["ordered_lines"] == ("待确认文字",)
    assert record["reviewed_lines"] is None
    assert record["needs_review"] is True
    assert record["review_reason"] == "没有可靠的 20 位文字"
    assert measurement_record_service.list_records("pending")["records"] == []
    reviewed_records = measurement_record_service.list_records(
        "reviewed", "2"
    )["records"]
    assert reviewed_records[0]["session_id"] == "review-session"
    assert measurement_record_service.list_records("normal", "2")["records"] == []

    # 从数据库再次确认原始字段没有被复核写入覆盖。
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        saved_record = connection.execute(
            "SELECT ordered_lines, needs_review, review_reason, reviewed_lines "
            "FROM measurement_records WHERE session_id = ?",
            ("review-session",),
        ).fetchone()
    assert saved_record == ('["待确认文字"]', 1, "没有可靠的 20 位文字", None)
    with pytest.raises(MeasurementReviewAlreadyCompletedError, match="该记录已完成复核"):
        measurement_record_service.complete_review("review-session", "再次修改")
    with pytest.raises(MeasurementReviewAlreadyCompletedError):
        measurement_record_service.complete_review("normal-session")
    normal_record = measurement_record_service.get_record("normal-session")["record"]
    assert normal_record["reviewed_at"] is None


def test_edited_review_saves_lines_and_rejects_empty_input(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证人工文字按行保存且空输入不完成复核。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 有效文字保存为 JSON，原始 OCR 保留
    """
    with pytest.raises(MeasurementRecordServiceError, match="至少需要一条有效文字"):
        measurement_record_service.complete_review("review-session", " \n\t ")
    pending_record = measurement_record_service.get_record("review-session")["record"]
    assert pending_record["reviewed_at"] is None

    # 保存去空白后的两条人工文字。
    measurement_record_service.complete_review("review-session", " 修正一 \n\n 修正二  ")
    record = measurement_record_service.get_record("review-session")["record"]
    assert record["ordered_lines"] == ("待确认文字",)
    assert record["reviewed_lines"] == ("修正一", "修正二")
    assert record["needs_review"] is True
    assert record["review_reason"] == "没有可靠的 20 位文字"
    assert datetime.fromisoformat(record["reviewed_at"]).tzinfo == timezone.utc
    reviewed_records = measurement_record_service.list_records("reviewed")["records"]
    assert reviewed_records[0]["reviewed_lines"] == (
        "修正一", "修正二"
    )

    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        saved_record = connection.execute(
            "SELECT ordered_lines, reviewed_lines FROM measurement_records "
            "WHERE session_id = ?",
            ("review-session",),
        ).fetchone()
    assert saved_record[0] == '["待确认文字"]'
    assert json.loads(saved_record[1]) == ["修正一", "修正二"]


def test_invalid_ocr_json_is_a_history_read_error(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证损坏的历史 OCR JSON 按普通读取错误报告。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 损坏的文字 JSON 产生历史读取错误
    """
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE measurement_records SET ordered_lines = ? WHERE session_id = ?",
            ("{broken", "normal-session"),
        )

    # 核对列表读取的对外提示。
    with pytest.raises(MeasurementRecordServiceError) as list_error:
        measurement_record_service.list_records()
    assert str(list_error.value) == "历史记录读取失败。"

    # 核对详情读取的对外提示。
    with pytest.raises(MeasurementRecordServiceError) as detail_error:
        measurement_record_service.get_record("normal-session")
    assert str(detail_error.value) == "历史详情读取失败。"


def test_history_page_shows_filters_and_read_only_details(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证页面列表、筛选、频率占位和详情原因。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 表格与详情显示筛选后的已保存测量记录
    """
    controller = AppController(
        Mock(), measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 页面进入时读取全部历史记录。
        page.refresh_history()
        assert page.unlimited_time_checkbox.isChecked()
        assert not page.start_date_edit.isEnabled()
        assert not page.end_date_edit.isEnabled()
        assert page.table.rowCount() == 4
        assert page.table.item(0, 1).text() == "99"
        assert page.table.item(2, 3).text() == "--"
        assert page.table.item(2, 4).text() == "待复核"
        assert page.table.item(2, 4).background().color().name() == "#fff0d8"

        # 组合状态和机器筛选，仅保留软删除机器的待复核记录。
        page.status_buttons["pending"].click()
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("2"))
        assert page.table.rowCount() == 1
        assert page.table.item(0, 1).text() == "二号皮带"
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["session_id"].text() == "review-session"
        assert page.detail_values["frequency"].text() == "--"
        assert page.detail_ocr_text.toPlainText() == "待确认文字"
        assert page.review_reason_value.text() == "没有可靠的 20 位文字"
        assert page.detail_ocr_text.isReadOnly()
        assert page.review_editor.toPlainText() == "待确认文字"
        assert not page.confirm_review_button.isHidden()
        assert not page.save_review_button.isHidden()
        page.detail_dialog.close()

        # 正常记录使用绿色状态且不显示复核原因。
        page.machine_filter.setCurrentIndex(0)
        page.status_buttons["normal"].click()
        assert page.table.rowCount() == 3
        assert page.table.item(2, 4).text() == "正常"
        assert page.table.item(2, 4).background().color().name() == "#dcf8e9"
        page.table.cellWidget(2, 5).click()
        assert page.detail_ocr_text.toPlainText() == "12345678\n003"
        assert page.detail_values["frequency"].text() == "50.0 Hz"
        assert page.review_reason_title.isHidden()
        assert page.review_editor.isHidden()
        assert page.confirm_review_button.isHidden()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_history_page_turns_pages_and_resets_on_filter_changes(
    qt_application: QApplication,
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证历史页翻页、页码展示及筛选变化后的第一页。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 翻页与筛选后显示正确的分页记录
    """
    controller = AppController(
        Mock(), paged_measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 初次进入历史页显示最新二十条记录。
        page.refresh_history()
        assert page.current_page == 1
        assert page.table.rowCount() == 20
        assert page.table.item(0, 2).text() == "文字 24"
        assert page.record_count_label.text() == "共 25 条"
        assert page.page_label.text() == "第 1 / 2 页"
        assert not page.previous_page_button.isEnabled()
        assert page.next_page_button.isEnabled()

        # 下一页和上一页分别显示连续的记录。
        page.next_page_button.click()
        assert page.current_page == 2
        assert page.table.rowCount() == 5
        assert page.table.item(0, 2).text() == "文字 04"
        assert page.table.item(4, 2).text() == "文字 00"
        assert page.previous_page_button.isEnabled()
        assert not page.next_page_button.isEnabled()
        page.previous_page_button.click()
        assert page.current_page == 1
        assert page.table.item(0, 2).text() == "文字 24"

        # 状态筛选回第一页并更新筛选后的总数。
        page.next_page_button.click()
        page.status_buttons["pending"].click()
        assert page.current_page == 1
        assert page.table.item(0, 2).text() == "文字 20"
        assert page.record_count_label.text() == "共 21 条"
        assert page.page_label.text() == "第 1 / 2 页"

        # 机器筛选和重新进入历史页均回第一页。
        page.status_buttons[None].click()
        page.next_page_button.click()
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("2"))
        assert page.current_page == 1
        assert page.table.rowCount() == 4
        assert page.table.item(0, 2).text() == "文字 24"
        assert page.record_count_label.text() == "共 4 条"
        page.machine_filter.setCurrentIndex(0)
        page.next_page_button.click()
        page.refresh_history()
        assert page.current_page == 1
        assert page.table.item(0, 2).text() == "文字 24"
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_history_page_filters_dates_and_restores_unlimited_time(
    qt_application: QApplication,
    paged_measurement_record_service: MeasurementRecordService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证日期筛选交互、日期顺序同步和重新进入时重置。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        paged_measurement_record_service: 已保存二十五条分页记录的服务。
        monkeypatch: pytest 提供的对象替换工具。

    Returns:
        返回示例：
            None  # 日期变化只查询一次第一页，重新进入恢复不限时间
    """
    controller = AppController(
        Mock(), paged_measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 设置无记录的日期并确认默认不限时间仍显示全部记录。
        page.refresh_history()
        selected_date = datetime.fromisoformat(
            "2026-09-27T08:00:00+00:00"
        ).astimezone().date()
        selected_qdate = QDate(
            selected_date.year, selected_date.month, selected_date.day
        )
        previous_qdate = selected_qdate.addDays(-1)
        page.start_date_edit.setDate(previous_qdate)
        page.end_date_edit.setDate(previous_qdate)
        assert page.record_count_label.text() == "共 25 条"
        page.next_page_button.click()
        assert page.current_page == 2

        # 启用日期筛选后读取新范围的第一页。
        page.unlimited_time_checkbox.click()
        assert not page.unlimited_time_checkbox.isChecked()
        assert page.start_date_edit.isEnabled()
        assert page.end_date_edit.isEnabled()
        assert page.current_page == 1
        assert page.record_count_label.text() == "共 0 条"

        # 开始日期超过结束日期时同步结束日期并只查询一次。
        list_measurement_records = Mock(wraps=controller.list_measurement_records)
        monkeypatch.setattr(
            controller, "list_measurement_records", list_measurement_records
        )
        page.start_date_edit.setDate(selected_qdate)
        assert page.end_date_edit.date() == selected_qdate
        assert list_measurement_records.call_count == 1
        assert page.record_count_label.text() == "共 25 条"
        assert page.page_label.text() == "第 1 / 2 页"

        # 结束日期提前时同步开始日期并回到第一页。
        page.next_page_button.click()
        assert page.current_page == 2
        list_measurement_records.reset_mock()
        page.end_date_edit.setDate(previous_qdate)
        assert page.start_date_edit.date() == previous_qdate
        assert list_measurement_records.call_count == 1
        assert page.current_page == 1
        assert page.record_count_label.text() == "共 0 条"

        # 重新进入历史页恢复不限时间并只读取一次记录。
        list_measurement_records.reset_mock()
        page.refresh_history()
        assert list_measurement_records.call_count == 1
        assert page.unlimited_time_checkbox.isChecked()
        assert not page.start_date_edit.isEnabled()
        assert not page.end_date_edit.isEnabled()
        assert page.start_date_edit.date() == previous_qdate
        assert page.end_date_edit.date() == previous_qdate
        assert page.record_count_label.text() == "共 25 条"
        assert page.current_page == 1
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("failure_source", ("machines", "records"))
def test_history_page_clears_pagination_after_query_failure(
    qt_application: QApplication,
    paged_measurement_record_service: MeasurementRecordService,
    monkeypatch: pytest.MonkeyPatch,
    failure_source: str,
) -> None:
    """验证机器或记录读取失败时清空旧分页状态。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        paged_measurement_record_service: 已保存二十五条分页记录的服务。
        monkeypatch: pytest 提供的对象替换工具。
        failure_source: 发生读取失败的查询入口。

    Returns:
        返回示例：
            None  # 旧记录和分页信息已清空，翻页按钮不可用
    """
    controller = AppController(
        Mock(), paged_measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 先保留成功查询后的总数和可用翻页按钮。
        page.refresh_history()
        if failure_source == "machines":
            page.next_page_button.click()
            assert page.current_page == 2
            assert page.previous_page_button.isEnabled()
        else:
            assert page.current_page == 1
            assert page.next_page_button.isEnabled()
        assert page.record_count_label.text() == "共 25 条"
        warning_message = Mock()
        monkeypatch.setattr(QMessageBox, "warning", warning_message)

        # 让对应查询失败并触发页面刷新。
        if failure_source == "machines":
            monkeypatch.setattr(
                controller,
                "list_record_machines",
                Mock(return_value=Result.error("模拟读取失败")),
            )
            page.refresh_history()
        else:
            monkeypatch.setattr(
                controller,
                "list_measurement_records",
                Mock(return_value=Result.error("模拟读取失败")),
            )
            page.reload_records()

        # 核对旧记录和分页入口已清空。
        assert page.table.rowCount() == 0
        assert page.current_page == 1
        assert page.record_count_label.text() == "共 0 条"
        assert page.page_label.text() == "第 1 / 1 页"
        assert not page.previous_page_button.isEnabled()
        assert not page.next_page_button.isEnabled()
        warning_message.assert_called_once_with(
            page, "历史记录读取失败", "模拟读取失败"
        )
        page.previous_page_button.click()
        page.next_page_button.click()
        assert page.current_page == 1
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_history_page_returns_to_last_page_after_review_reduces_results(
    qt_application: QApplication,
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证复核使待复核末页消失时页面自动回退。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 当前页回到新的最后一页并显示剩余待复核记录
    """
    controller = AppController(
        Mock(), paged_measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 打开待复核的最后一页并完成唯一一条记录。
        page.refresh_history()
        page.status_buttons["pending"].click()
        page.next_page_button.click()
        assert page.current_page == 2
        assert page.table.rowCount() == 1
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["session_id"].text() == "page-00"
        page.confirm_review_button.click()

        # 筛选结果缩为一页后显示新的最后一页。
        assert page.current_page == 1
        assert page.table.rowCount() == 20
        assert page.table.item(0, 2).text() == "文字 20"
        assert page.record_count_label.text() == "共 20 条"
        assert page.page_label.text() == "第 1 / 1 页"
        assert not page.previous_page_button.isEnabled()
        assert not page.next_page_button.isEnabled()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


@pytest.mark.parametrize(
    ("edited_text", "expected_result"),
    ((None, "待确认文字"), (" 人工修正一 \n\n 人工修正二 ", "人工修正一\n人工修正二")),
)
def test_history_page_completes_review_and_shows_original_and_final_results(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
    edited_text: str | None,
    expected_result: str,
) -> None:
    """验证两种复核按钮均刷新列表且已复核详情只读。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。
        edited_text: 可选的人工编辑内容。
        expected_result: 预期的最终显示文字。

    Returns:
        返回示例：
            None  # 详情显示原始文字、人工结果和本地复核时间
    """
    controller = AppController(
        Mock(), measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 从待复核列表打开详情并完成当前记录。
        page.refresh_history()
        page.status_buttons["pending"].click()
        assert page.table.rowCount() == 1
        page.table.cellWidget(0, 5).click()
        if edited_text is None:
            page.confirm_review_button.click()
        else:
            page.review_editor.setPlainText(edited_text)
            page.save_review_button.click()
        assert page.table.rowCount() == 0

        # 从已复核列表核对摘要、状态和只读详情。
        page.status_buttons["reviewed"].click()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 2).text() == expected_result.replace("\n", "；")
        assert page.table.item(0, 4).text() == "已复核"
        assert page.table.item(0, 4).background().color().name() == "#e2eeff"
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["status"].text() == "已复核"
        assert page.detail_ocr_text.toPlainText() == "待确认文字"
        assert page.final_result_text.toPlainText() == expected_result
        assert page.final_result_text.isReadOnly()
        assert page.reviewed_at_value.text()
        assert page.review_reason_value.text() == "没有可靠的 20 位文字"
        assert page.review_editor.isHidden()
        assert page.confirm_review_button.isHidden()
        assert page.save_review_button.isHidden()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_stale_history_detail_cannot_review_record_twice(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证旧详情重复提交时刷新只读状态且不覆盖已保存结果。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。
        monkeypatch: pytest 提供的对象替换工具。

    Returns:
        返回示例：
            None  # 第二次提交提示并显示最新已复核详情
    """
    warning_message = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning_message)
    controller = AppController(
        Mock(), measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 打开旧详情后由另一操作先完成复核。
        page.show_record_detail("review-session")
        assert page.detail_values["status"].text() == "待复核"
        measurement_record_service.complete_review("review-session")
        page.review_editor.setPlainText("不应覆盖原结果")
        page.save_review_button.click()

        # 重复操作提示失败并重新显示已复核详情。
        assert warning_message.call_args.args[2] == "该记录已完成复核。"
        assert page.detail_values["status"].text() == "已复核"
        assert page.review_editor.isHidden()
        assert page.confirm_review_button.isHidden()
        assert page.save_review_button.isHidden()

        # 首次保存的复核结果保持不变。
        reviewed_record = measurement_record_service.get_record(
            "review-session"
        )["record"]
        assert reviewed_record["reviewed_lines"] is None
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_other_review_error_keeps_current_detail(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证普通复核失败只提示，不重新读取当前详情。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。
        monkeypatch: pytest 提供的对象替换工具。

    Returns:
        返回示例：
            None  # 普通失败保留当前待复核详情
    """
    warning_message = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning_message)
    controller = AppController(
        Mock(), measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 打开待复核详情并提交没有有效文字的修改。
        page.show_record_detail("review-session")
        page.show_record_detail = Mock(wraps=page.show_record_detail)
        page.review_editor.setPlainText(" \n ")
        page.save_review_button.click()

        # 普通失败保留编辑状态且不重新读取详情。
        assert warning_message.call_args.args[2] == "人工复核结果至少需要一条有效文字。"
        page.show_record_detail.assert_not_called()
        assert page.detail_values["status"].text() == "待复核"
        assert not page.review_editor.isHidden()
        assert not page.confirm_review_button.isHidden()
        assert not page.save_review_button.isHidden()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_history_detail_shows_evidence_images_and_opens_large_image(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
    tmp_path: Path,
) -> None:
    """验证正常与待复核记录显示证据，损坏文件不妨碍查看大图。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 两类记录可查看图片且损坏图片已跳过
    """
    # 在两条记录已有的目录中写入测试图片和一张损坏图片。
    normal_directory = tmp_path / "normal-evidence"
    review_directory = tmp_path / "review-evidence"
    for image_path in (
        normal_directory / "normal-frame.jpg",
        review_directory / "review-frame-1.jpg",
        review_directory / "review-frame-2.jpg",
        review_directory / "review-frame-3.jpg",
        review_directory / "review-frame-4.jpg",
    ):
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(16, 12, QImage.Format.Format_RGB32)
        image.fill(QColor("blue"))
        assert image.save(str(image_path), "JPG")
    (review_directory / "broken.jpg").write_bytes(b"not an image")
    (review_directory / "old-frame.ppm").write_bytes(b"legacy image")

    controller = AppController(
        Mock(), measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 正常记录使用自己的目录并显示一张缩略图。
        page.show_record_detail("normal-session")
        assert page.evidence_grid.count() == 1
        normal_thumbnail = page.evidence_grid.itemAt(0).widget()
        assert isinstance(normal_thumbnail, QPushButton)
        assert normal_thumbnail.toolTip() == "normal-frame.jpg"
        page.detail_dialog.close()

        # 待复核记录显示多张可读取图片并保留复核原因。
        page.show_record_detail("review-session")
        assert page.evidence_grid.count() == 4
        review_thumbnails = [
            page.evidence_grid.itemAt(index).widget()
            for index in range(page.evidence_grid.count())
        ]
        assert [thumbnail.toolTip() for thumbnail in review_thumbnails] == [
            "review-frame-1.jpg",
            "review-frame-2.jpg",
            "review-frame-3.jpg",
            "review-frame-4.jpg",
        ]
        assert page.evidence_grid.itemAtPosition(1, 0).widget() is review_thumbnails[3]
        assert page.review_reason_value.text() == "没有可靠的 20 位文字"

        # 点击缩略图后检查大图并关闭临时窗口。
        large_images = []

        def inspect_and_close_image_dialog() -> None:
            """检查当前大图窗口中的图片并关闭窗口。

            Args:
                无外部参数。

            Returns:
                返回示例：
                    None  # 已记录大图状态并关闭窗口
            """
            image_dialog = QApplication.activeModalWidget()
            image_label = image_dialog.findChild(QLabel)
            large_images.append(image_label.pixmap().size())
            image_dialog.accept()

        QTimer.singleShot(0, inspect_and_close_image_dialog)
        review_thumbnails[0].click()
        assert len(large_images) == 1
        assert large_images[0].width() > 0
        assert large_images[0].height() > 0
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_history_detail_handles_missing_empty_and_unreadable_evidence(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
    tmp_path: Path,
) -> None:
    """验证不存在、空目录和全部损坏的证据目录显示占位。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 证据缺失或损坏时详情仍能打开并显示提示
    """
    controller = AppController(
        Mock(), measurement_record_service, Mock(), Path("config")
    )
    page = HistoryPage(controller)
    try:
        # 记录目录不存在时显示无图片提示。
        page.show_record_detail("normal-session")
        assert page.evidence_grid.itemAt(0).widget().text() == "暂无证据图片"
        page.detail_dialog.close()

        # 记录的证据目录为空时显示同样的提示。
        page.populate_evidence_images("")
        assert page.evidence_grid.itemAt(0).widget().text() == "暂无证据图片"

        # 目录存在但为空时保持相同提示。
        review_directory = tmp_path / "review-evidence"
        review_directory.mkdir()
        page.show_record_detail("review-session")
        assert page.evidence_grid.itemAt(0).widget().text() == "暂无证据图片"
        page.detail_dialog.close()

        # 只有损坏的 JPG 时显示读取失败，不影响复核原因。
        (review_directory / "broken.jpg").write_bytes(b"not an image")
        page.show_record_detail("review-session")
        assert page.evidence_grid.itemAt(0).widget().text() == "证据图片读取失败"
        assert page.review_reason_value.text() == "没有可靠的 20 位文字"
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_main_window_refreshes_only_when_entering_history(
    qt_application: QApplication,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证主窗口只在切换进入历史页时请求刷新。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 每次从其他页面进入历史页时恰好刷新一次
    """
    database_path = measurement_record_service.measurement_record_repo.database_path
    machine_repo = MachineRepo(database_path)
    abnormal_event_service = AbnormalEventService(
        AbnormalEventRepo(database_path.with_suffix(".recovery.sqlite3"))
    )
    controller = AppController(
        MachineService(machine_repo),
        measurement_record_service,
        abnormal_event_service,
        Path("config"),
    )
    window = MainWindow(controller)
    try:
        window.history_page.refresh_history = Mock(
            wraps=window.history_page.refresh_history
        )
        window.switch_page("history")
        assert window.history_page.refresh_history.call_count == 1
        window.switch_page("history")
        assert window.history_page.refresh_history.call_count == 1
        window.switch_page("realtime")
        window.switch_page("history")
        assert window.history_page.refresh_history.call_count == 2
    finally:
        window.close()
        window.deleteLater()
