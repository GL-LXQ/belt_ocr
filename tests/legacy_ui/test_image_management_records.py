"""使用真实临时业务链离屏验证图片管理、证据查看和历史复核。"""

from contextlib import closing
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Callable
from unittest.mock import Mock
import json
import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from src.controller.controller import AppController
from src.repo.abnormal_event_repo import AbnormalEventRepo
from src.repo.machine_repo import MachineRepo
from src.repo.measurement_record_repo import MeasurementRecordRepo
from src.service.abnormal_event_service import AbnormalEventService
from src.service.machine_service import MachineService
from src.service.measurement_record_service import MeasurementRecordService
from ui import evidence_directory
from ui import image_evidence
from ui.main_window import MainWindow
from ui.pages.image_management_page import ImageManagementPage


def wait_for_result(condition: Callable[[], bool], timeout_ms: int = 5000) -> None:
    """处理主线程事件并在有限时间内等待异步结果。

    Args:
        condition: 返回当前界面是否已接收目标结果的检查函数。
        timeout_ms: 最长等待毫秒数。

    Returns:
        None  # 条件已满足，超时则断言失败
    """
    deadline = monotonic() + timeout_ms / 1000
    while not condition() and monotonic() < deadline:
        QTest.qWait(20)
    assert condition(), "等待真实图片读取结果超时"


def wait_for_records(page: ImageManagementPage, total: int, count: int) -> None:
    """等待当前查询及其卡片目录读取全部回到界面。

    Args:
        page: 正在显示的真实图片页面。
        total: 当前筛选下的测量总数。
        count: 当前页应显示的卡片数量。

    Returns:
        None  # 测量总数、卡片数量和目录状态均已更新
    """
    wait_for_result(
        lambda: page.record_count_label.text() == f"共 {total} 次测量"
        and len(page.cards) == count
        and all(card.record.evidence_state != "loading" for card in page.cards)
    )


def save_jpeg(path: Path, size: QSize = QSize(1600, 800)) -> None:
    """在临时证据目录保存指定尺寸的合成 JPEG。

    Args:
        path: 临时图片的完整路径。
        size: 原始图片的像素尺寸。

    Returns:
        None  # JPEG 已写入临时目录
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(size, QImage.Format.Format_RGB32)
    image.fill(QColor("#3275a2"))
    assert image.save(str(path), "JPEG")


def insert_record(
    controller: AppController,
    session_id: str,
    *,
    machine_id: str = "1",
    finish_time: str = "2026-10-01T12:01:00+00:00",
    lines: tuple[str, ...] = ("ABC00001",),
    needs_review: bool = True,
    reviewed_lines: tuple[str, ...] | None = None,
    evidence_directory: Path | None = None,
) -> Path:
    """向临时业务库写入一条真实查询和复核使用的记录。

    Args:
        controller: 连接临时业务库的真实控制器。
        session_id: 本次记录的唯一编号。
        machine_id: 已创建的临时机器编号。
        finish_time: 保存的 UTC 完成时间。
        lines: 原始 OCR 文字行。
        needs_review: 是否需要人工复核。
        reviewed_lines: 已保存的人工结果，None 表示尚未复核。
        evidence_directory: 保存的证据目录，None 时使用记录对应临时路径。

    Returns:
        Path("temporary/session")  # 本条记录保存的证据目录
    """
    database_path = controller.measurement_record_service.measurement_record_repo.database_path
    directory = evidence_directory or database_path.parent / "evidence" / session_id
    reviewed_at = "2026-10-01T14:00:00+00:00" if reviewed_lines is not None else None

    # 直接写入测试样本，查询和复核仍经过正式业务链。
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, recognized_lines, "
            "final_frequency_hz, evidence_directory, needs_review, review_reason, reviewed_at, reviewed_lines) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                machine_id,
                "2026-10-01T12:00:00+00:00",
                finish_time,
                json.dumps(lines, ensure_ascii=False),
                48.5,
                str(directory),
                int(needs_review),
                "需要确认文字" if needs_review else None,
                reviewed_at,
                json.dumps(reviewed_lines, ensure_ascii=False) if reviewed_lines is not None else None,
            ),
        )
    return directory


def release_image_page(page: ImageManagementPage) -> None:
    """隐藏界面并在销毁控件前清理已完成的线程池。

    Args:
        page: 即将释放的真实图片页面。

    Returns:
        None  # 页面和查看器均已停止后台读取
    """
    page.hide()
    page.viewer.close()
    pools = (page.thread_pool, page.viewer.thread_pool)
    wait_for_result(lambda: all(pool is None or pool.activeThreadCount() == 0 for pool in pools))
    QTest.qWait(20)
    page.shutdown()


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """复用离屏控件测试的 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 当前进程的离屏 Qt 应用
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def real_controller(tmp_path: Path, qt_application: QApplication) -> AppController:
    """建立只访问临时库且没有监测线程的完整控制器。

    Args:
        tmp_path: pytest 提供的临时目录。
        qt_application: 离屏 Qt 应用。

    Returns:
        AppController(...)  # 机器、测量及异常服务均连接临时数据库
    """
    database_path = tmp_path / "image-records.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)
        AbnormalEventRepo.create_table(connection)

    # 初始化真实机器和业务服务，不读取配置或启动硬件。
    machine_repo = MachineRepo(database_path)
    machine_repo.insert("一号测试皮带", "test-camera-1", "test-meter-1")
    machine_repo.insert("二号测试皮带", "test-camera-2", "test-meter-2")
    return AppController(
        MachineService(machine_repo),
        MeasurementRecordService(MeasurementRecordRepo(database_path)),
        AbnormalEventService(AbnormalEventRepo(database_path)),
        tmp_path / "unused-configuration",
    )


@pytest.fixture
def real_image_page(real_controller: AppController, qt_application: QApplication):
    """建立尚未显示的图片页，供测试先写入真实样本。

    Args:
        real_controller: 使用临时业务库的控制器。
        qt_application: 离屏 Qt 应用。

    Returns:
        ImageManagementPage(...)  # 已绑定真实控制器的图片页面
    """
    host = QWidget()
    host.resize(1424, 900)
    layout = QVBoxLayout(host)
    page = ImageManagementPage(host, controller=real_controller)
    layout.addWidget(page)
    yield page

    # 先处理取消后的回调，再销毁拥有后台任务的控件。
    release_image_page(page)
    host.close()
    host.deleteLater()
    qt_application.processEvents()


@pytest.fixture
def real_main_window(real_controller: AppController, qt_application: QApplication):
    """建立完整主窗口并在退出前释放图片读取任务。

    Args:
        real_controller: 使用临时业务库的控制器。
        qt_application: 离屏 Qt 应用。

    Returns:
        MainWindow(...)  # 复用正式导航和历史复核页面的窗口
    """
    window = MainWindow(real_controller)
    yield window

    # 关闭详情和图片任务，避免线程池等待阻塞未处理的界面回调。
    window.history_page.detail_dialog.close()
    release_image_page(window.images_page)
    window.close()
    window.deleteLater()
    qt_application.processEvents()


def test_real_default_empty_never_inserts_demo_records(real_image_page: ImageManagementPage) -> None:
    """验证真实空业务库不会自动展示演示记录。

    Args:
        real_image_page: 已绑定空临时库的图片页面。

    Returns:
        None  # 真实零条状态和可用查询入口均正确
    """
    page = real_image_page
    page.window().show()
    wait_for_records(page, 0, 0)
    assert not page.preview_enabled
    assert page.preview_records == ()
    assert page.empty_title.text() == "暂无测量记录"
    assert page.page_label.text() == "1 / 1"
    assert page.filter_card.isEnabled()
    assert page.refresh_button.isEnabled()
    assert not page.previous_page_button.isEnabled()
    assert not page.next_page_button.isEnabled()
    assert page.controller.list_measurement_records().data["total"] == 0


def test_real_pages_use_list_directories_without_card_detail_queries(
    real_image_page: ImageManagementPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证每页十二条、同条件总数及列表目录投影避免逐卡详情查询。

    Args:
        real_image_page: 已绑定临时业务库的图片页面。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 三页完整覆盖二十五条记录，组合筛选保留十三条且不查询详情
    """
    page = real_image_page
    directories = {}
    for record_number in range(25):
        session_id = f"page-{record_number:02}"
        directory = insert_record(
            page.controller,
            session_id,
            machine_id="1" if record_number < 13 else "2",
            finish_time=f"2026-10-01T12:01:{record_number:02}+00:00",
            needs_review=record_number < 13,
        )
        save_jpeg(directory / "saved.JPG", QSize(240, 120))
        directories[session_id] = str(directory)

    # 记录目录枚举和详情调用，但保留真实文件系统及控制器实现。
    scanner = Mock(wraps=image_evidence.os.scandir)
    detail_reader = Mock(wraps=page.controller.get_measurement_record)
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)
    monkeypatch.setattr(page.controller, "get_measurement_record", detail_reader)
    page.window().show()
    wait_for_records(page, 25, 12)

    # 每次翻页仅枚举本页十二条或最后一条记录的保存目录。
    observed_sessions = []
    for page_number, expected_count in ((1, 12), (2, 12), (3, 1)):
        if page_number > 1:
            scanner.reset_mock()
            page.next_page_button.click()
            wait_for_records(page, 25, expected_count)
        sessions = [card.record.session_id for card in page.cards]
        observed_sessions.extend(sessions)
        assert page.page_label.text() == f"{page_number} / 3"
        assert {call.args[0] for call in scanner.call_args_list} == {directories[session] for session in sessions}
        assert scanner.call_count == expected_count
        assert all(card.image_count_label.text() == "现存 1 张" for card in page.cards)
        assert all(not card.thumbnail.source_pixmap.isNull() for card in page.cards)
        assert all(card.record.images == () for card in page.cards)
    assert observed_sessions == [f"page-{number:02}" for number in range(24, -1, -1)]
    assert not page.next_page_button.isEnabled()
    detail_reader.assert_not_called()

    # 机器、状态、日期、文字和完整行位数一起参与同一分页查询。
    page.machine_combo.setCurrentIndex(page.machine_combo.findData("1"))
    page.status_filter.items["pending"].click()
    page.apply_time_filter(date(2026, 10, 1), date(2026, 10, 1))
    page.text_query_edit.setText(" a b c 00001 ")
    page.text_length_combo.setCurrentIndex(page.text_length_combo.findData(8))
    page.match_mode_combo.setCurrentIndex(page.match_mode_combo.findData("exact"))
    page.search_button.click()
    wait_for_records(page, 13, 12)
    assert page.current_page == 1
    assert page.page_label.text() == "1 / 2"
    assert page.selected_text_query == "ABC00001"
    assert all(card.record.machine_id == "1" and card.record.review_status == "pending" for card in page.cards)
    page.next_page_button.click()
    wait_for_records(page, 13, 1)
    assert page.cards[0].record.session_id == "page-00"
    assert page.page_label.text() == "2 / 2"
    detail_reader.assert_not_called()


@pytest.mark.parametrize(
    "query,match_mode,text_length",
    [(" a\tb c00001 ", "exact", 8), ("a b c", "contains", 8), ("%", "contains", 3), ("_", "contains", 3)],
)
def test_real_filters_are_literal_normalized_and_combined(
    real_image_page: ImageManagementPage,
    query: str,
    match_mode: str,
    text_length: int,
) -> None:
    """验证标准化和字面查询同机器、日期、状态及同一行位数组合。

    Args:
        real_image_page: 已绑定临时业务库的图片页面。
        query: 含空白、小写或 SQL 通配字符的查询草稿。
        match_mode: 精确或包含匹配方式。
        text_length: 被查询完整文字行的长度。

    Returns:
        None  # 只有同时满足全部条件的真实记录被选中
    """
    page = real_image_page
    matching_lines = ("ABC00001", "A%B", "A_B", "003")
    insert_record(page.controller, "match", lines=matching_lines)
    insert_record(page.controller, "other-machine", machine_id="2", lines=matching_lines)
    insert_record(page.controller, "other-date", finish_time="2026-10-02T12:01:00+00:00", lines=matching_lines)
    insert_record(page.controller, "normal", needs_review=False, lines=matching_lines)
    insert_record(page.controller, "reviewed", lines=matching_lines, reviewed_lines=("EDIT0001",))
    insert_record(page.controller, "wildcard-decoy", lines=("AAB", "ACB"))
    insert_record(page.controller, "same-line-decoy", lines=("ABC", "00000000"))
    page.window().show()
    wait_for_records(page, 7, 7)

    # 控件逐项提交，最终结果要求所有条件共同成立。
    page.machine_combo.setCurrentIndex(page.machine_combo.findData("1"))
    page.status_filter.items["pending"].click()
    page.apply_time_filter(date(2026, 10, 1), date(2026, 10, 1))
    page.text_query_edit.setText(query)
    page.match_mode_combo.setCurrentIndex(page.match_mode_combo.findData(match_mode))
    page.text_length_combo.setCurrentIndex(page.text_length_combo.findData(text_length))
    page.search_button.click()
    wait_for_records(page, 1, 1)
    assert page.cards[0].record.session_id == "match"
    assert page.selected_text_query == "".join(query.split()).upper()
    assert page.page_label.text() == "1 / 1"

    # 未提交的新草稿不覆盖已经应用的查询。
    page.text_query_edit.setText("未提交的无结果草稿")
    page.refresh_button.click()
    wait_for_records(page, 1, 1)
    assert page.cards[0].record.session_id == "match"


def test_empty_text_keeps_existing_history_length_semantics(real_image_page: ImageManagementPage) -> None:
    """验证空白文字沿用历史规则，不单独应用文字位数。

    Args:
        real_image_page: 已绑定临时业务库的图片页面。

    Returns:
        None  # 提交空白查询保留记录，位数不单独筛选
    """
    page = real_image_page
    insert_record(page.controller, "eight-characters")
    page.window().show()
    wait_for_records(page, 1, 1)
    page.text_query_edit.setText(" \t\u3000 ")
    page.text_length_combo.setCurrentIndex(page.text_length_combo.findData(20))
    page.search_button.click()
    wait_for_records(page, 1, 1)
    assert page.selected_text_query == ""
    assert page.selected_text_length == 20
    assert page.cards[0].record.session_id == "eight-characters"


@pytest.mark.parametrize("final_lines", [("EDIT0001", "003"), ()])
def test_real_reviewed_text_overrides_ocr_even_when_empty(
    real_image_page: ImageManagementPage,
    final_lines: tuple[str, ...],
    qt_application: QApplication,
) -> None:
    """验证卡片、查看器、复制和搜索统一使用非 NULL 人工结果。

    Args:
        real_image_page: 已绑定临时业务库的图片页面。
        final_lines: 真实保存的人工文字，包含空元组边界。
        qt_application: 离屏 Qt 应用。

    Returns:
        None  # 人工空结果和非空结果均不会回退到 OCR
    """
    page = real_image_page
    directory = insert_record(page.controller, "reviewed-text", lines=("HIDDEN01",), reviewed_lines=final_lines)
    save_jpeg(directory / "reviewed.JPEG")
    page.window().show()
    wait_for_records(page, 1, 1)
    assert page.cards[0].record.effective_lines == final_lines
    assert page.cards[0].summary_label.toolTip() == ("\n".join(final_lines) or "暂无识别文字")
    page.cards[0].click()
    viewer = page.viewer
    wait_for_result(lambda: viewer.record is not None and viewer.loaded_image_index == 0)
    assert viewer.full_text_edit.toPlainText() == ("\n".join(final_lines) or "暂无识别文字")
    assert viewer.copy_text_button.isEnabled() == bool(final_lines)
    if final_lines:
        viewer.copy_text_button.click()
        assert qt_application.clipboard().text() == "\n".join(final_lines)
    viewer.close()

    # 原 OCR 已被人工结果覆盖，真实列表筛选不再命中。
    page.text_query_edit.setText("HIDDEN01")
    page.search_button.click()
    wait_for_records(page, 0, 0)
    assert page.empty_title.text() == "没有符合条件的测量"


def test_history_previews_card_cover_and_viewer_share_frame_order(real_main_window: MainWindow) -> None:
    """验证历史前四张、图片封面和查看器均沿用同一采集组的数值帧顺序。

    Args:
        real_main_window: 仅连接临时业务库的完整主窗口。

    Returns:
        None  # 历史前四帧与查看器前四帧一致，卡片封面为第一张可用帧
    """
    window = real_main_window
    directory = insert_record(window.controller, "numeric-frame-order")
    capture_id = "11111111111141118111111111111111"

    # 按乱序写入六张合成图片，不使用文件写入时间作为顺序。
    for frame_number in (10, 2, 100, 11, 1, 3):
        save_jpeg(directory / f"{capture_id}-{frame_number}.jpg", QSize(64, 48))
    expected = [f"{capture_id}-{frame_number}.jpg" for frame_number in (1, 2, 3, 10, 11, 100)]

    # 真实卡片按共享顺序选择封面，查看器重新读取同一目录。
    window.show()
    window.switch_page("images")
    page = window.images_page
    wait_for_records(page, 1, 1)
    assert page.cards[0].thumbnail.toolTip() == expected[0]
    page.cards[0].click()
    viewer = page.viewer
    wait_for_result(lambda: viewer.record is not None and viewer.loaded_image_index == 0)
    assert [image.filename for image in viewer.record.images] == expected
    assert [button.toolTip() for button in viewer.thumbnail_buttons] == expected

    # 逐张前进验证查看器导航保持数值帧顺序。
    for image_index, filename in enumerate(expected):
        if image_index:
            viewer.next_image_button.click()
            wait_for_result(lambda: viewer.loaded_image_index == image_index)
        assert viewer.filename_label.text() == filename

    # 从查看器进入已有历史详情，只预览相同顺序的前四张。
    viewer.view_record_button.click()
    history = window.history_page
    wait_for_result(lambda: history.detail_dialog.isVisible() and window.current_page_key == "history")
    assert history.evidence_grid.count() == 4
    assert [history.evidence_grid.itemAt(index).widget().toolTip() for index in range(4)] == expected[:4]


def test_card_click_reads_fresh_detail_and_corrupt_first_image_can_navigate(
    real_image_page: ImageManagementPage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qt_application: QApplication,
) -> None:
    """验证点击重读最新目录和文字，保留坏图文件名并允许继续查看。

    Args:
        real_image_page: 已绑定临时业务库的图片页面。
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        qt_application: 离屏 Qt 应用。

    Returns:
        None  # 最新详情、原样路径、坏图导航、适应及原尺寸均正确
    """
    page = real_image_page
    old_directory = insert_record(page.controller, "fresh-detail")
    save_jpeg(old_directory / "stale.jpg")
    page.window().show()
    wait_for_records(page, 1, 1)

    # 卡片加载后更新保存目录和复核结果，查看器必须重新读详情。
    stored_directory = tmp_path / "saved directory" / "actual-session"
    stored_directory.mkdir(parents=True)
    corrupt_filename = "001 原始证据.JPEG"
    (stored_directory / corrupt_filename).write_bytes(b"broken jpeg")
    save_jpeg(stored_directory / "002-good.JpEg")
    save_jpeg(stored_directory / "nested" / "excluded.jpg")
    save_jpeg(stored_directory.parent / "neighbor" / "excluded.jpg")
    database_path = page.controller.measurement_record_service.measurement_record_repo.database_path
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "UPDATE measurement_records SET evidence_directory = ?, reviewed_at = ?, reviewed_lines = ? "
            "WHERE session_id = ?",
            (str(stored_directory), "2026-10-01T14:00:00+00:00", '["EDIT0002", "003"]', "fresh-detail"),
        )

    # 保留真实读取，仅记录详情调用和枚举目录。
    detail_reader = Mock(wraps=page.controller.get_measurement_record)
    scanner = Mock(wraps=image_evidence.os.scandir)
    opener = Mock(return_value=True)
    monkeypatch.setattr(page.controller, "get_measurement_record", detail_reader)
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)
    monkeypatch.setattr(evidence_directory.QDesktopServices, "openUrl", opener)
    page.cards[0].click()
    viewer = page.viewer
    wait_for_result(lambda: viewer.record is not None and viewer.loaded_image_index == 0)
    detail_reader.assert_called_once_with("fresh-detail")
    scanner.assert_called_once_with(str(stored_directory))
    assert viewer.record.evidence_directory == str(stored_directory)
    assert [image.filename for image in viewer.record.images] == [corrupt_filename, "002-good.JpEg"]
    assert viewer.filename_label.text() == corrupt_filename
    assert viewer.image_position_label.text() == "第 1 / 2 张"
    assert viewer.image_error_title.text() == "图片无法解码"
    assert corrupt_filename in viewer.image_error_body.text()
    assert viewer.canvas.image_item.pixmap().isNull()
    assert not viewer.actual_size_button.isEnabled()
    assert viewer.next_image_button.isEnabled()

    # 下一张有效原图支持适应和 100%，损坏图片仍可返回查看。
    viewer.next_image_button.click()
    wait_for_result(lambda: viewer.loaded_image_index == 1)
    assert viewer.filename_label.text() == "002-good.JpEg"
    assert viewer.canvas.image_item.pixmap().size() == QSize(1600, 800)
    assert viewer.canvas.fit_mode
    assert viewer.actual_size_button.isEnabled()
    assert not viewer.next_image_button.isEnabled()
    viewer.actual_size_button.click()
    assert viewer.canvas.transform().m11() == pytest.approx(1.0)
    assert viewer.zoom_label.text() == "100%"
    assert not viewer.canvas.fit_mode
    viewer.fit_button.click()
    assert viewer.canvas.fit_mode
    assert 0 < viewer.canvas.transform().m11() < 1

    # 复制和文件夹打开都使用最新详情的原样目录及人工文字。
    assert viewer.full_text_edit.toPlainText() == "EDIT0002\n003"
    assert viewer.status_badge.text() == "已复核"
    viewer.copy_text_button.click()
    assert qt_application.clipboard().text() == "EDIT0002\n003"
    viewer.copy_path_button.click()
    assert qt_application.clipboard().text() == str(stored_directory)
    viewer.open_directory_button.click()
    assert opener.call_args.args[0].toLocalFile() == str(stored_directory)
    viewer.previous_image_button.click()
    wait_for_result(lambda: viewer.loaded_image_index == 0)
    assert viewer.filename_label.text() == corrupt_filename
    assert viewer.record.image_count == 2


@pytest.mark.parametrize("pending_filter", [False, True])
def test_real_main_window_review_round_trip_refreshes_text_and_preserves_filters(
    real_main_window: MainWindow,
    pending_filter: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证真实记录信号复用历史复核，返回后刷新且保留图片筛选。

    Args:
        real_main_window: 绑定真实临时业务链的完整主窗口。
        pending_filter: 是否保留待复核筛选并要求复核后移出结果。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 正式复核已落库，返回图片页显示最新结果并保留提交条件
    """
    window = real_main_window
    directory = insert_record(window.controller, "review-round-trip")
    save_jpeg(directory / "review.JPG")
    config_reader = Mock(side_effect=AssertionError("测试不允许读取运行配置"))
    hardware_start = Mock(side_effect=AssertionError("测试不允许启动监测硬件"))
    monkeypatch.setattr(window.controller, "read_configuration", config_reader)
    monkeypatch.setattr(window.controller, "start_monitoring", hardware_start)
    window.show()
    window.switch_page("images")
    page = window.images_page
    wait_for_records(page, 1, 1)

    # 设置保存复核前后都能命中的已提交条件，并保留另一份输入草稿。
    page.machine_combo.setCurrentIndex(page.machine_combo.findData("1"))
    page.apply_time_filter(date(2026, 10, 1), date(2026, 10, 1))
    page.text_query_edit.setText(" a b c ")
    page.text_length_combo.setCurrentIndex(page.text_length_combo.findData(8))
    page.search_button.click()
    if pending_filter:
        page.status_filter.items["pending"].click()
    wait_for_records(page, 1, 1)
    page.text_query_edit.setText("尚未提交的输入草稿")
    page.cards[0].click()
    wait_for_result(lambda: page.viewer.record is not None and page.viewer.loaded_image_index == 0)

    # 点击真实查看器入口，信号经主窗口导航到已有历史详情。
    page.viewer.view_record_button.click()
    history = window.history_page
    wait_for_result(lambda: history.detail_dialog.isVisible() and window.current_page_key == "history")
    assert window.stackedWidget.currentWidget() is history
    assert history.detail_values["session_id"].text() == "review-round-trip"
    assert history.detail_values["status"].text() == "待复核"
    assert history.review_editor_section.isVisible()
    assert not page.viewer.isVisible()

    # 使用历史页已有保存按钮，验证真实人工结果和复核时间落库。
    history.review_editor.setPlainText(" a bc00002 \n 0 03 ")
    history.save_review_button.click()
    wait_for_result(lambda: not history.detail_dialog.isVisible())
    saved_record = window.controller.get_measurement_record("review-round-trip").data["record"]
    assert saved_record["recognized_lines"] == ("ABC00001",)
    assert saved_record["reviewed_lines"] == ("ABC00002", "003")
    assert saved_record["reviewed_at"] is not None

    # 返回时沿用筛选；待复核条件会自然移除刚完成的记录。
    window.switch_page("images")
    wait_for_records(page, 0 if pending_filter else 1, 0 if pending_filter else 1)
    assert page.selected_machine_id == "1"
    assert page.selected_start_date == page.selected_end_date == date(2026, 10, 1)
    assert page.selected_text_query == "ABC"
    assert page.selected_match_mode == "contains"
    assert page.selected_text_length == 8
    assert page.selected_review_status == ("pending" if pending_filter else None)
    assert page.text_query_edit.text() == "尚未提交的输入草稿"
    if pending_filter:
        assert page.empty_title.text() == "没有符合条件的测量"
        page.status_filter.items["reviewed"].click()
        wait_for_records(page, 1, 1)

    # 最新卡片和再次打开的查看器都展示已复核的人工文字。
    assert page.cards[0].record.effective_lines == ("ABC00002", "003")
    assert page.cards[0].status_badge.text() == "已复核"
    assert page.cards[0].summary_label.toolTip() == "ABC00002\n003"
    page.cards[0].click()
    wait_for_result(lambda: page.viewer.record is not None and page.viewer.loaded_image_index == 0)
    assert page.viewer.full_text_edit.toPlainText() == "ABC00002\n003"
    assert page.viewer.status_badge.text() == "已复核"
    config_reader.assert_not_called()
    hardware_start.assert_not_called()
