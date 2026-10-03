"""离屏验证图片管理的隔离演示交互，不运行视觉截图测试。"""

from dataclasses import replace
from datetime import date
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from ui.date_range_picker import DateRangePicker
from ui.image_management_preview import build_preview_measurements
from ui.pages.image_management_page import ImageManagementPage


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """复用离屏 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 仅供控件交互测试的应用
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def image_page(qt_application: QApplication):
    """建立没有控制器或数据库的独立页面并释放控件。

    Args:
        qt_application: 离屏 Qt 应用。

    Returns:
        ImageManagementPage()  # 默认未开启演示的页面
    """
    host = QWidget()
    host.resize(1424, 900)
    layout = QVBoxLayout(host)
    page = ImageManagementPage(host)
    layout.addWidget(page)
    host.show()
    qt_application.processEvents()
    yield page
    page.viewer.close()
    page.shutdown()
    host.close()
    host.deleteLater()
    qt_application.processEvents()


def test_preview_requires_explicit_entry_and_exit_drops_records(image_page: ImageManagementPage) -> None:
    """验证独立页面没有记录时保持真实空状态，演示必须明确开启。

    Args:
        image_page: 默认未开启演示的页面。

    Returns:
        None  # 预览入口、标识、计数、隔离动作和退出状态符合约定
    """
    assert not image_page.preview_enabled
    assert image_page.preview_records == ()
    assert image_page.cards == []
    assert image_page.filter_card.isEnabled()
    assert image_page.refresh_button.isEnabled()
    assert image_page.record_count_label.text() == "共 0 次测量"
    assert image_page.empty_title.text() == "暂无测量记录"

    # 明确点击后才产生内存记录，数量按测量统计。
    image_page.preview_button.click()
    assert image_page.preview_enabled
    assert "演示数据" in image_page.preview_label.text()
    assert len(image_page.cards) == image_page.page_size == 12
    assert image_page.record_count_label.text() == "演示：共 14 次测量"
    assert not image_page.viewer.view_record_button.isEnabled()
    assert not image_page.viewer.open_directory_button.isEnabled()

    # 再次点击退出，不保留页面记录或可误用的筛选入口。
    image_page.cards[0].click()
    assert image_page.viewer.record is not None
    image_page.preview_button.click()
    assert not image_page.preview_enabled
    assert image_page.preview_records == ()
    assert image_page.cards == []
    assert not image_page.scenario_combo.isVisible()
    assert image_page.filter_card.isEnabled()
    assert image_page.viewer.record is None
    assert image_page.viewer.canvas.image_item.pixmap().isNull()
    assert image_page.viewer.thumbnail_buttons == []
    assert image_page.viewer.thumbnail_group.buttons() == []
    assert image_page.viewer.full_text_edit.toPlainText() == ""


def test_pagination_keeps_only_current_measurements_and_grouped_images(image_page: ImageManagementPage) -> None:
    """验证分页使用测量数量，翻页边界不产生重复卡片。

    Args:
        image_page: 默认未开启演示的页面。

    Returns:
        None  # 第一页十二次、第二页两次测量，组内图片没有拆成记录
    """
    image_page.preview_button.click()
    first_sessions = [card.record.session_id for card in image_page.cards]
    assert image_page.total_pages == 2
    assert not image_page.previous_page_button.isEnabled()
    assert image_page.next_page_button.isEnabled()
    assert image_page.cards[0].image_count_label.text() == "现存 3 张 · 演示"
    assert all(card.summary_label.text().count("\n") <= 1 for card in image_page.cards)

    # 第二页只建立剩余两次测量，越界不改变页码。
    image_page.next_page_button.click()
    assert len(image_page.cards) == 2
    assert image_page.grid.count() == 2
    assert not image_page.next_page_button.isEnabled()
    assert not set(first_sessions) & {card.record.session_id for card in image_page.cards}
    image_page.change_page(3)
    assert image_page.current_page == 2
    image_page.previous_page_button.click()
    assert [card.record.session_id for card in image_page.cards] == first_sessions


def test_unsubmitted_text_is_separate_from_machine_status_and_refresh(image_page: ImageManagementPage) -> None:
    """验证草稿不混入其他筛选，查询使用同一行位数和有效文字。

    Args:
        image_page: 默认未开启演示的页面。

    Returns:
        None  # 未提交草稿不生效，人工结果和精确匹配在提交后生效
    """
    image_page.preview_button.click()
    image_page.text_query_edit.setText("不会命中的草稿")
    image_page.machine_combo.setCurrentIndex(image_page.machine_combo.findData("DEMO-M3"))
    image_page.status_filter.items["reviewed"].click()
    image_page.refresh_button.click()
    assert image_page.selected_text_query == ""
    assert image_page.cards
    assert all(card.record.machine_id == "DEMO-M3" for card in image_page.cards)

    # 提交小写与空白文本后，按完整八位人工最终文字匹配。
    image_page.text_query_edit.setText(" edit 0001 \n")
    image_page.match_mode_combo.setCurrentIndex(image_page.match_mode_combo.findData("exact"))
    image_page.text_length_combo.setCurrentIndex(image_page.text_length_combo.findData(8))
    image_page.search_button.click()
    assert image_page.selected_text_query == "EDIT0001"
    assert image_page.cards
    assert all(card.record.reviewed_lines is not None for card in image_page.cards)
    assert all("EDIT0001" in card.record.effective_lines for card in image_page.cards)

    # 通配符按普通字符处理，重置可恢复完整演示。
    image_page.text_query_edit.setText("%")
    image_page.match_mode_combo.setCurrentIndex(0)
    image_page.search_button.click()
    assert image_page.cards == []
    assert "没有符合条件" in image_page.empty_title.text()
    image_page.empty_action.click()
    assert len(image_page.cards) == 12
    assert image_page.selected_machine_id is None
    assert image_page.selected_review_status is None


def test_empty_reviewed_text_does_not_fall_back_to_ocr(image_page: ImageManagementPage) -> None:
    """验证非 NULL 的空人工结果仍优先于原 OCR。

    Args:
        image_page: 默认未开启演示的页面。

    Returns:
        None  # 摘要、搜索和复制均不悄悄回退到原文字
    """
    image_page.preview_button.click()
    record = replace(build_preview_measurements()[2], reviewed_lines=())
    image_page.preview_records = (record,)
    image_page.render_records()
    assert image_page.cards[0].summary_label.text() == "暂无识别文字"
    image_page.cards[0].click()
    assert image_page.viewer.full_text_edit.toPlainText() == "暂无识别文字"
    assert not image_page.viewer.copy_text_button.isEnabled()
    image_page.viewer.close()
    image_page.text_query_edit.setText("DEMO")
    image_page.search_button.click()
    assert image_page.cards == []


def test_viewer_browses_one_measurement_and_supports_zoom_copy_reopen(
    image_page: ImageManagementPage,
    qt_application: QApplication,
) -> None:
    """验证缩略图、翻图、比例、复制、折叠信息和重复打开。

    Args:
        image_page: 默认未开启演示的页面。
        qt_application: 离屏 Qt 应用。

    Returns:
        None  # 查看器始终对应当前测量，不执行待接入动作
    """
    image_page.preview_button.click()
    first_record = image_page.cards[0].record
    image_page.cards[0].click()
    viewer = image_page.viewer
    qt_application.processEvents()
    assert viewer.isVisible()
    assert viewer.record == first_record
    assert len(viewer.thumbnail_buttons) == len(first_record.images)
    assert not viewer.previous_image_button.isEnabled()
    assert viewer.filename_label.text() == first_record.images[0].filename

    # 翻到竖图后支持 100%、放大和恢复适应模式。
    viewer.next_image_button.click()
    assert viewer.image_index == 1
    assert viewer.canvas.image_item.pixmap().size().toTuple() == first_record.images[1].size
    viewer.actual_size_button.click()
    assert viewer.canvas.transform().m11() == pytest.approx(1.0)
    assert not viewer.canvas.fit_mode
    viewer.zoom_in_button.click()
    assert viewer.canvas.transform().m11() > 1.0
    viewer.fit_button.click()
    assert viewer.canvas.fit_mode
    viewer.thumbnail_buttons[-1].click()
    assert not viewer.next_image_button.isEnabled()
    viewer.select_image(999)
    assert viewer.image_index == len(first_record.images) - 1

    # 复制完整有效文字，定位信息默认折叠且可展开。
    viewer.copy_text_button.click()
    assert qt_application.clipboard().text() == "\n".join(first_record.effective_lines)
    assert not viewer.metadata_panel.isVisible()
    viewer.metadata_button.click()
    assert viewer.metadata_panel.isVisible()
    assert viewer.session_label.text() == first_record.session_id
    assert "DEMO" in viewer.path_label.text()

    # 关闭后立刻打开另一测量，旧关闭任务不能关闭新内容。
    viewer.close_button.click()
    assert not viewer.isVisible()
    image_page.cards[1].click()
    QTest.qWait(220)
    assert viewer.isVisible()
    assert viewer.record == image_page.cards[1].record
    assert viewer.image_index == 0
    assert not viewer.metadata_panel.isVisible()
    assert viewer.view_record_button.text() == "前往复核 · 待接入"
    assert not viewer.view_record_button.isEnabled()
    assert not viewer.open_directory_button.isEnabled()


@pytest.mark.parametrize(
    "scenario,title,count",
    (
        ("missing_directory", "证据目录不存在", "数量未知 · 演示"),
        ("no_jpg", "目录中没有 JPG", "现存 0 张 · 演示"),
        ("access_denied", "没有目录读取权限", "数量未知 · 演示"),
        ("corrupt", "图片无法解码", "现存 3 张 · 演示"),
    ),
)
def test_evidence_exceptions_keep_measurement_and_filename(
    image_page: ImageManagementPage,
    scenario: str,
    title: str,
    count: str,
) -> None:
    """验证目录与文件异常不隐藏关联测量，坏图仍可前后翻阅。

    Args:
        image_page: 默认未开启演示的页面。
        scenario: 演示异常标识。
        title: 预期异常标题。
        count: 当前图片数量或未知提示。

    Returns:
        None  # 异常原因、组内位置和只读测量信息保持可见
    """
    image_page.preview_button.click()
    image_page.scenario_combo.setCurrentIndex(image_page.scenario_combo.findData(scenario))
    assert len(image_page.cards) == 1
    card = image_page.cards[0]
    assert title in card.thumbnail.text()
    assert card.image_count_label.text() == count
    card.click()
    viewer = image_page.viewer
    assert viewer.image_stack.currentIndex() == 1
    assert viewer.image_error_title.text() == title
    assert viewer.machine_label.text() == card.record.machine_name
    assert not viewer.actual_size_button.isEnabled()
    if scenario == "corrupt":
        filename = card.record.images[0].filename
        assert filename in card.thumbnail.text()
        assert filename in viewer.image_error_body.text()
        assert viewer.filename_label.text() == filename
        assert len(viewer.thumbnail_buttons) == len(card.record.images)
        viewer.next_image_button.click()
        assert viewer.image_stack.currentIndex() == 0
        assert viewer.actual_size_button.isEnabled()


@pytest.mark.parametrize("dismiss_action", ("cancel", "escape", "toggle", "hide"))
def test_date_drafts_are_discarded_without_changing_applied_filters(
    image_page: ImageManagementPage,
    dismiss_action: str,
) -> None:
    """验证单层日期的取消、Esc、重复点击和页面离开。

    Args:
        image_page: 默认未开启演示的页面。
        dismiss_action: 本次关闭日期面板的操作。

    Returns:
        None  # 未提交日期没有刷新记录，重开恢复已应用范围
    """
    image_page.preview_button.click()
    image_page.apply_time_filter(date(2026, 9, 26), date(2026, 9, 27))
    original_render = image_page.render_records
    image_page.render_records = Mock(wraps=original_render)
    image_page.time_filter_button.click()
    panel = image_page.time_filter_panel
    picker = panel.view.findChild(DateRangePicker)
    picker.calendar._onDayItemClicked(QDate(2026, 9, 29))
    assert picker.start_date == picker.end_date == QDate(2026, 9, 29)
    if dismiss_action == "cancel":
        next(button for button in panel.view.findChildren(QPushButton) if button.text() == "取消").click()
    elif dismiss_action == "escape":
        QTest.keyClick(panel, Qt.Key.Key_Escape)
    elif dismiss_action == "toggle":
        image_page.time_filter_button.click()
    else:
        image_page.hide()
    assert image_page.time_filter_panel is None
    assert image_page.selected_start_date == date(2026, 9, 26)
    assert image_page.selected_end_date == date(2026, 9, 27)
    image_page.render_records.assert_not_called()

    # 新面板恢复已应用值，旧实例的关闭通知不影响新实例。
    image_page.show()
    image_page.time_filter_button.click()
    current_panel = image_page.time_filter_panel
    picker = current_panel.view.findChild(DateRangePicker)
    assert picker.start_date == QDate(2026, 9, 26)
    assert picker.end_date == QDate(2026, 9, 27)
    panel.closed.emit()
    assert image_page.time_filter_panel is current_panel


def test_date_apply_and_clear_preserve_machine_and_text(image_page: ImageManagementPage) -> None:
    """验证确定应用完成日期，清除日期保留其他已提交条件。

    Args:
        image_page: 默认未开启演示的页面。

    Returns:
        None  # 日期与机器、文字组合生效，清除时间不重置其他条件
    """
    image_page.preview_button.click()
    image_page.machine_combo.setCurrentIndex(image_page.machine_combo.findData("DEMO-M1"))
    image_page.text_query_edit.setText("DEMO")
    image_page.search_button.click()
    image_page.time_filter_button.click()
    panel = image_page.time_filter_panel
    picker = panel.view.findChild(DateRangePicker)
    picker.calendar._onDayItemClicked(QDate(2026, 9, 27))
    picker.end_button.click()
    picker.calendar._onDayItemClicked(QDate(2026, 9, 27))
    next(button for button in panel.view.findChildren(QPushButton) if button.text() == "确定").click()
    assert image_page.selected_start_date == image_page.selected_end_date == date(2026, 9, 27)
    assert image_page.cards
    assert all(card.record.finished_at.date() == date(2026, 9, 27) for card in image_page.cards)
    image_page.time_filter_button.click()
    panel = image_page.time_filter_panel
    next(button for button in panel.view.findChildren(QPushButton) if button.text() == "清除时间").click()
    assert image_page.selected_start_date is None
    assert image_page.selected_end_date is None
    assert image_page.selected_machine_id == "DEMO-M1"
    assert image_page.selected_text_query == "DEMO"
