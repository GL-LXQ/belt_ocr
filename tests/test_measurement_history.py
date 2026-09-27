"""验证测量历史的查询、筛选和只读页面。"""

import os
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from config_util import AppConfig
from database import Database, MeasurementRecord
from repo.machine_repo import MachineRepo
from repo.measurement_repo import MeasurementRepo
from src.service.history_service import HistoryService, HistoryServiceError
from src.service.machine_service import MachineService
from ui.main_window import MainWindow
from ui.pages.history_page import HistoryPage


@pytest.fixture
def history_service(tmp_path: Path) -> HistoryService:
    """建立含正常、待复核和历史机器状态的业务库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            HistoryService(...)  # 已保存四条测量记录的读取服务
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
    return HistoryService(MeasurementRepo(config.database_path))


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


def test_history_service_filters_and_reads_details(
    history_service: HistoryService,
) -> None:
    """验证历史筛选、软删除机器和只读详情字段。

    Args:
        history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 筛选结果和详情字段均来自 measurements
    """
    # 查询全部、正常、待复核和指定机器的记录。
    records = history_service.list_records()
    assert [record["session_id"] for record in records] == [
        "missing-machine-session", "disabled-machine-session",
        "review-session", "normal-session"
    ]
    assert [record["session_id"] for record in history_service.list_records(False)] == [
        "missing-machine-session", "disabled-machine-session", "normal-session"
    ]
    assert [record["session_id"] for record in history_service.list_records(True)] == [
        "review-session"
    ]
    assert history_service.list_records(True, "1") == []
    assert history_service.list_records(True, "2")[0]["session_id"] == "review-session"
    disabled_records = history_service.list_records(False, "3")
    assert disabled_records[0]["session_id"] == "disabled-machine-session"

    # 核对软删除机器和缺少机器信息时的展示名称。
    machines = history_service.list_record_machines()
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
    review_record = history_service.get_record("review-session")
    normal_record = history_service.get_record("normal-session")
    assert review_record["ordered_lines"] == ("待确认文字",)
    assert review_record["final_frequency_hz"] is None
    assert review_record["review_reason"] == "没有可靠的 20 位文字"
    assert normal_record["ordered_lines"] == ("12345678", "003")
    assert normal_record["review_reason"] is None
    assert history_service.get_record("unknown-session") is None


def test_invalid_ocr_json_is_a_history_read_error(
    history_service: HistoryService,
) -> None:
    """验证损坏的历史 OCR JSON 按普通读取错误报告。

    Args:
        history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 损坏的文字 JSON 产生历史读取错误
    """
    database_path = history_service.measurement_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE measurements SET ordered_lines = ? WHERE session_id = ?",
            ("{broken", "normal-session"),
        )

    with pytest.raises(HistoryServiceError, match="历史记录读取失败"):
        history_service.list_records()
    with pytest.raises(HistoryServiceError, match="历史详情读取失败"):
        history_service.get_record("normal-session")


def test_history_page_shows_filters_and_read_only_details(
    qt_application: QApplication,
    history_service: HistoryService,
) -> None:
    """验证页面列表、筛选、频率占位和详情原因。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 表格与详情显示筛选后的已保存测量记录
    """
    page = HistoryPage(history_service)
    try:
        # 页面进入时读取全部历史记录。
        page.refresh_history()
        assert page.table.rowCount() == 4
        assert page.table.item(0, 1).text() == "99"
        assert page.table.item(2, 3).text() == "--"
        assert page.table.item(2, 4).text() == "待复核"
        assert page.table.item(2, 4).background().color().name() == "#fff0d8"

        # 组合状态和机器筛选，仅保留软删除机器的待复核记录。
        page.status_buttons[True].click()
        page.machine_filter.setCurrentIndex(page.machine_filter.findData("2"))
        assert page.table.rowCount() == 1
        assert page.table.item(0, 1).text() == "二号皮带"
        page.table.cellWidget(0, 5).click()
        assert page.detail_values["session_id"].text() == "review-session"
        assert page.detail_values["frequency"].text() == "--"
        assert page.detail_ocr_text.toPlainText() == "待确认文字"
        assert page.review_reason_value.text() == "没有可靠的 20 位文字"
        assert page.detail_ocr_text.isReadOnly()
        page.detail_dialog.close()

        # 正常记录使用绿色状态且不显示复核原因。
        page.machine_filter.setCurrentIndex(0)
        page.status_buttons[False].click()
        assert page.table.rowCount() == 3
        assert page.table.item(2, 4).text() == "正常"
        assert page.table.item(2, 4).background().color().name() == "#dcf8e9"
        page.table.cellWidget(2, 5).click()
        assert page.detail_ocr_text.toPlainText() == "12345678\n003"
        assert page.detail_values["frequency"].text() == "50.0 Hz"
        assert page.review_reason_title.isHidden()
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_main_window_refreshes_only_when_entering_history(
    qt_application: QApplication,
    history_service: HistoryService,
) -> None:
    """验证主窗口只在切换进入历史页时请求刷新。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 每次从其他页面进入历史页时恰好刷新一次
    """
    machine_repo = MachineRepo(history_service.measurement_repo.database_path)
    window = MainWindow(MachineService(machine_repo), history_service)
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
