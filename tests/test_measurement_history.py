"""验证测量历史的查询、筛选、证据和人工复核。"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from config_util import AppConfig
from database import Database, MeasurementRecord
from repo.machine_repo import MachineRepo
from repo.abnormal_event_repo import AbnormalEventRepo
from repo.measurement_record_repo import MeasurementRecordRepo
from src.service.abnormal_event_service import AbnormalEventService
from src.service.measurement_history_service import (
    MeasurementHistoryService,
    MeasurementHistoryServiceError,
    MeasurementReviewAlreadyCompletedError,
)
from src.service.machine_service import MachineService
from ui.main_window import MainWindow
from ui.pages.history_page import HistoryPage


@pytest.fixture
def measurement_history_service(tmp_path: Path) -> MeasurementHistoryService:
    """建立含正常、待复核和历史机器状态的业务库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            MeasurementHistoryService(...)  # 已保存四条测量记录的读取服务
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
    return MeasurementHistoryService(MeasurementRecordRepo(config.database_path))


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


def test_measurement_history_service_filters_and_reads_details(
    measurement_history_service: MeasurementHistoryService,
) -> None:
    """验证历史筛选、软删除机器和只读详情字段。

    Args:
        measurement_history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 筛选结果和详情字段均来自 measurement_records
    """
    # 查询全部、正常、待复核和指定机器的记录。
    records = measurement_history_service.list_records()
    assert [record["session_id"] for record in records] == [
        "missing-machine-session", "disabled-machine-session",
        "review-session", "normal-session"
    ]
    normal_records = measurement_history_service.list_records("normal")
    assert [record["session_id"] for record in normal_records] == [
        "missing-machine-session", "disabled-machine-session", "normal-session"
    ]
    review_records = measurement_history_service.list_records("pending")
    assert [record["session_id"] for record in review_records] == [
        "review-session"
    ]
    assert measurement_history_service.list_records("reviewed") == []
    assert measurement_history_service.list_records("pending", "1") == []
    machine_records = measurement_history_service.list_records("pending", "2")
    assert machine_records[0]["session_id"] == "review-session"
    disabled_records = measurement_history_service.list_records("normal", "3")
    assert disabled_records[0]["session_id"] == "disabled-machine-session"

    # 核对软删除机器和缺少机器信息时的展示名称。
    machines = measurement_history_service.list_record_machines()
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
    review_record = measurement_history_service.get_record("review-session")
    normal_record = measurement_history_service.get_record("normal-session")
    assert review_record["ordered_lines"] == ("待确认文字",)
    assert review_record["final_frequency_hz"] is None
    assert review_record["review_reason"] == "没有可靠的 20 位文字"
    assert normal_record["ordered_lines"] == ("12345678", "003")
    assert normal_record["review_reason"] is None
    assert measurement_history_service.get_record("unknown-session") is None


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
    measurement_history_service: MeasurementHistoryService,
) -> None:
    """验证确认原文字只写复核时间且同一记录不能再复核。

    Args:
        measurement_history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 原文字和复核原因保留，重复复核已拒绝
    """
    measurement_history_service.complete_review("review-session")
    record = measurement_history_service.get_record("review-session")
    reviewed_time = datetime.fromisoformat(record["reviewed_at"])
    assert reviewed_time.tzinfo == timezone.utc
    assert record["ordered_lines"] == ("待确认文字",)
    assert record["reviewed_lines"] is None
    assert record["needs_review"] is True
    assert record["review_reason"] == "没有可靠的 20 位文字"
    assert measurement_history_service.list_records("pending") == []
    reviewed_records = measurement_history_service.list_records("reviewed", "2")
    assert reviewed_records[0]["session_id"] == "review-session"
    assert measurement_history_service.list_records("normal", "2") == []

    # 从数据库再次确认原始字段没有被复核写入覆盖。
    database_path = measurement_history_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        saved_record = connection.execute(
            "SELECT ordered_lines, needs_review, review_reason, reviewed_lines "
            "FROM measurement_records WHERE session_id = ?",
            ("review-session",),
        ).fetchone()
    assert saved_record == ('["待确认文字"]', 1, "没有可靠的 20 位文字", None)
    with pytest.raises(MeasurementReviewAlreadyCompletedError, match="该记录已完成复核"):
        measurement_history_service.complete_review("review-session", "再次修改")
    with pytest.raises(MeasurementReviewAlreadyCompletedError):
        measurement_history_service.complete_review("normal-session")
    normal_record = measurement_history_service.get_record("normal-session")
    assert normal_record["reviewed_at"] is None


def test_edited_review_saves_lines_and_rejects_empty_input(
    measurement_history_service: MeasurementHistoryService,
) -> None:
    """验证人工文字按行保存且空输入不完成复核。

    Args:
        measurement_history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 有效文字保存为 JSON，原始 OCR 保留
    """
    with pytest.raises(MeasurementHistoryServiceError, match="至少需要一条有效文字"):
        measurement_history_service.complete_review("review-session", " \n\t ")
    pending_record = measurement_history_service.get_record("review-session")
    assert pending_record["reviewed_at"] is None

    # 保存去空白后的两条人工文字。
    measurement_history_service.complete_review("review-session", " 修正一 \n\n 修正二  ")
    record = measurement_history_service.get_record("review-session")
    assert record["ordered_lines"] == ("待确认文字",)
    assert record["reviewed_lines"] == ("修正一", "修正二")
    assert record["needs_review"] is True
    assert record["review_reason"] == "没有可靠的 20 位文字"
    assert datetime.fromisoformat(record["reviewed_at"]).tzinfo == timezone.utc
    reviewed_records = measurement_history_service.list_records("reviewed")
    assert reviewed_records[0]["reviewed_lines"] == (
        "修正一", "修正二"
    )

    database_path = measurement_history_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        saved_record = connection.execute(
            "SELECT ordered_lines, reviewed_lines FROM measurement_records "
            "WHERE session_id = ?",
            ("review-session",),
        ).fetchone()
    assert saved_record[0] == '["待确认文字"]'
    assert json.loads(saved_record[1]) == ["修正一", "修正二"]


def test_invalid_ocr_json_is_a_history_read_error(
    measurement_history_service: MeasurementHistoryService,
) -> None:
    """验证损坏的历史 OCR JSON 按普通读取错误报告。

    Args:
        measurement_history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 损坏的文字 JSON 产生历史读取错误
    """
    database_path = measurement_history_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE measurement_records SET ordered_lines = ? WHERE session_id = ?",
            ("{broken", "normal-session"),
        )

    with pytest.raises(MeasurementHistoryServiceError, match="历史记录读取失败"):
        measurement_history_service.list_records()
    with pytest.raises(MeasurementHistoryServiceError, match="历史详情读取失败"):
        measurement_history_service.get_record("normal-session")


def test_history_page_shows_filters_and_read_only_details(
    qt_application: QApplication,
    measurement_history_service: MeasurementHistoryService,
) -> None:
    """验证页面列表、筛选、频率占位和详情原因。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 表格与详情显示筛选后的已保存测量记录
    """
    page = HistoryPage(measurement_history_service)
    try:
        # 页面进入时读取全部历史记录。
        page.refresh_history()
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


@pytest.mark.parametrize(
    ("edited_text", "expected_result"),
    ((None, "待确认文字"), (" 人工修正一 \n\n 人工修正二 ", "人工修正一\n人工修正二")),
)
def test_history_page_completes_review_and_shows_original_and_final_results(
    qt_application: QApplication,
    measurement_history_service: MeasurementHistoryService,
    edited_text: str | None,
    expected_result: str,
) -> None:
    """验证两种复核按钮均刷新列表且已复核详情只读。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_history_service: 已保存测试记录的历史服务。
        edited_text: 可选的人工编辑内容。
        expected_result: 预期的最终显示文字。

    Returns:
        返回示例：
            None  # 详情显示原始文字、人工结果和本地复核时间
    """
    page = HistoryPage(measurement_history_service)
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
    measurement_history_service: MeasurementHistoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证旧详情重复提交时提示并切换为已复核只读详情。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_history_service: 已保存测试记录的历史服务。
        monkeypatch: pytest 提供的对象替换工具。

    Returns:
        返回示例：
            None  # 第二次提交没有覆盖第一次复核结果
    """
    warning_message = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning_message)
    page = HistoryPage(measurement_history_service)
    try:
        # 打开旧详情后由另一操作先完成复核。
        page.show_record_detail("review-session")
        measurement_history_service.complete_review("review-session")
        page.review_editor.setPlainText("不应覆盖原结果")
        page.save_review_button.click()

        # 重复操作只提示并显示已复核状态。
        assert warning_message.call_args.args[2] == "该记录已完成复核。"
        assert page.detail_values["status"].text() == "已复核"
        assert page.review_editor.isHidden()
        reviewed_record = measurement_history_service.get_record("review-session")
        assert reviewed_record["reviewed_lines"] is None
    finally:
        page.detail_dialog.close()
        page.close()
        page.deleteLater()


def test_history_detail_shows_evidence_images_and_opens_large_image(
    qt_application: QApplication,
    measurement_history_service: MeasurementHistoryService,
    tmp_path: Path,
) -> None:
    """验证正常与待复核记录显示证据，损坏文件不妨碍查看大图。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_history_service: 已保存测试记录的历史服务。
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

    page = HistoryPage(measurement_history_service)
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
    measurement_history_service: MeasurementHistoryService,
    tmp_path: Path,
) -> None:
    """验证不存在、空目录和全部损坏的证据目录显示占位。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_history_service: 已保存测试记录的历史服务。
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 证据缺失或损坏时详情仍能打开并显示提示
    """
    page = HistoryPage(measurement_history_service)
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
    measurement_history_service: MeasurementHistoryService,
) -> None:
    """验证主窗口只在切换进入历史页时请求刷新。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用。
        measurement_history_service: 已保存测试记录的历史服务。

    Returns:
        返回示例：
            None  # 每次从其他页面进入历史页时恰好刷新一次
    """
    database_path = measurement_history_service.measurement_record_repo.database_path
    machine_repo = MachineRepo(database_path)
    abnormal_event_service = AbnormalEventService(
        AbnormalEventRepo(database_path.with_suffix(".recovery.sqlite3"))
    )
    window = MainWindow(
        MachineService(machine_repo),
        measurement_history_service,
        abnormal_event_service,
    )
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
