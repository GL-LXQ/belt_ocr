"""验证 Fluent 窗口和实时卡片的界面交互。"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QLabel,
    QSizePolicy,
)
from PySide6.QtTest import QTest
from qfluentwidgets import FluentWindow, InfoBar, MaskDialogBase, MessageBox

from src.controller.controller import AppController, Result
from ui.main_window import NAVIGATION_WIDTH, MainWindow
from ui.pages.realtime_page import RealtimePage


@pytest.fixture
def qt_application() -> QApplication:
    """提供界面测试使用的 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 当前测试进程的 Qt 应用
    """
    return QApplication.instance() or QApplication([])


def make_ui_controller() -> Mock:
    """创建只提供界面展示数据的控制器替身。

    Args:
        无。

    Returns:
        Mock()  # 含机器、历史、异常和监测状态结果的替身
    """
    machines = [
        {
            "id": machine_id,
            "machine_name": f"第 {machine_id} 台机器",
            "camera_serial": f"CAM-{machine_id}",
            "frequency_meter_serial": f"FREQ-{machine_id}",
            "enabled": True,
            "remark": None,
            "created_at": "2026-09-28 08:00:00",
            "updated_at": "2026-09-28 08:00:00",
        }
        for machine_id in range(1, 6)
    ]
    controller = Mock()
    controller.list_enabled_machines.return_value = Result.ok({"machines": machines})
    controller.list_machines.return_value = Result.ok({"machines": machines})
    controller.list_record_machines.return_value = Result.ok({"machines": []})
    controller.list_measurement_records.return_value = Result.ok(
        {"records": [], "total": 0, "total_pages": 1}
    )
    controller.list_abnormal_event_machine_ids.return_value = Result.ok(
        {"machine_ids": []}
    )
    controller.list_abnormal_events.return_value = Result.ok({"events": []})
    controller.is_monitoring_running.return_value = Result.ok({"running": False})
    controller.get_today_measurement_summary.return_value = Result.ok({
        "recognition_count": 128,
        "pending_review_count": 6,
    })
    return controller


def test_fluent_navigation_uses_existing_pages_and_refreshes_once(
    qt_application: QApplication,
) -> None:
    """验证导航复用页面且进入列表页只查询一次。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 页面实例和查询次数均符合预期
    """
    controller = make_ui_controller()
    window = MainWindow(controller)
    try:
        window.show()
        qt_application.processEvents()
        assert isinstance(window, FluentWindow)
        assert window.stackedWidget.count() == 7
        assert not window.history_page.detail_dialog.isVisible()
        assert not window.abnormal_events_page.detail_dialog.isVisible()
        assert not window.machines_page.editor.isVisible()
        realtime_page = window.realtime_page
        history_page = window.history_page
        abnormal_page = window.abnormal_events_page
        machines_page = window.machines_page

        # 每个页面只在实际进入时触发一次数据读取。
        for page_key in (
            "history", "abnormal_events", "machines", "images", "settings", "logs"
        ):
            window.switch_page(page_key)
            qt_application.processEvents()
            assert window.current_page_key == page_key
        controller.list_record_machines.assert_called_once()
        controller.list_measurement_records.assert_called_once()
        controller.list_abnormal_event_machine_ids.assert_called_once()
        controller.list_abnormal_events.assert_called_once()

        # 回到同一页面不会重建页面或重复刷新。
        window.switch_page("realtime")
        window.switch_page("realtime")
        qt_application.processEvents()
        assert window.realtime_page is realtime_page
        assert window.history_page is history_page
        assert window.abnormal_events_page is abnormal_page
        assert window.machines_page is machines_page
    finally:
        window.close()
        window.deleteLater()


def test_fixed_navigation_and_title_bar_stay_aligned(
    qt_application: QApplication,
) -> None:
    """验证固定导航在两种窗口尺寸下完整显示且不覆盖标题栏。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 导航、品牌、分组与标题栏保持预期布局
    """
    window = MainWindow(make_ui_controller())
    try:
        window.show()
        QTest.qWait(200)
        qt_application.processEvents()
        panel = window.navigationInterface.panel

        # 核对品牌、分组及上下导航项的顺序。
        top_items = [
            panel.topLayout.itemAt(index).widget()
            for index in range(panel.topLayout.count())
            if panel.topLayout.itemAt(index).widget().text()
        ]
        bottom_items = [
            panel.bottomLayout.itemAt(index).widget()
            for index in range(panel.bottomLayout.count())
        ]
        assert [item.text() for item in top_items] == [
            "BeltVision", "工作台", "实时监测", "历史记录", "异常事件", "机器管理"
        ]
        assert [item.text() for item in bottom_items] == [
            "图片管理", "系统配置", "日志查看"
        ]
        assert top_items[0].testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        assert not top_items[0].isSelectable
        assert top_items[1].height() > 0
        assert top_items[1].lightTextColor.name() == "#667085"
        assert not panel.menuButton.isVisible()
        assert not panel.returnButton.isVisible()
        assert "#F6F6F7" in panel.styleSheet()

        # 核对导航宽度及标题栏在缩放前后的边界。
        for width, height in ((1600, 900), (1320, 720)):
            window.resize(width, height)
            qt_application.processEvents()
            assert panel.displayMode.name == "EXPAND"
            assert window.navigationInterface.width() == NAVIGATION_WIDTH
            assert window.titleBar.x() == NAVIGATION_WIDTH
            assert window.titleBar.width() == width - NAVIGATION_WIDTH
            assert not window.titleBar.iconLabel.isVisible()
            assert not window.titleBar.titleLabel.isVisible()
            assert window.status_area.isVisible()
            # 核对状态区域紧邻右侧窗口按钮。
            status_right = window.status_area.mapTo(
                window.titleBar, QPoint(window.status_area.width(), 0)
            ).x()
            buttons_left = window.titleBar.minBtn.mapTo(
                window.titleBar, QPoint(0, 0)
            ).x()
            assert status_right == buttons_left

            assert window.clock_label.text()
            assert window.status_text.text() == "系统运行正常"
            assert window.connection_text.text() == "服务已连接"
            assert all(
                button.isVisible()
                for button in (
                    window.titleBar.minBtn,
                    window.titleBar.maxBtn,
                    window.titleBar.closeBtn,
                )
            )
        assert window.windowTitle() == "BeltVision | 实时监测"
    finally:
        window.close()
        window.deleteLater()


def test_realtime_resize_reflows_existing_cards_without_query(
    qt_application: QApplication,
) -> None:
    """验证机器卡片随窗口宽度伸缩并复用已有实例。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 列数和卡片宽度随窗口变化，查询及实例保持不变
    """
    controller = make_ui_controller()
    window = MainWindow(controller)
    try:
        window.resize(1600, 900)
        window.show()
        qt_application.processEvents()
        cards = tuple(window.realtime_page.machine_cards)
        query_count = controller.list_enabled_machines.call_count
        assert window.navigationInterface.panel.displayMode.name == "EXPAND"
        assert window.minimumWidth() == 1320
        detail_panel = window.realtime_page.detail_panel
        assert detail_panel.width() == 380
        assert detail_panel.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Preferred
        assert detail_panel.minimumHeight() == 600
        scroll_area = window.realtime_page.scroll_area
        section_title = window.realtime_page.findChild(QLabel, "machineSectionTitle")
        assert detail_panel.mapTo(window, QPoint(0, 0)).y() == (
            section_title.mapTo(window, QPoint(0, 0)).y()
        )

        # 核对两列与三列下的等宽卡片、固定间距及详情右对齐。
        card_widths = {}
        for width, height, expected_columns in (
            (1320, 720, 2),
            (1400, 800, 2),
            (1600, 900, 3),
            (1920, 1080, 3),
            (2200, 900, 3),
        ):
            window.resize(width, height)
            qt_application.processEvents()
            page = window.realtime_page
            # 核对详情区始终与左侧分区标题顶部对齐。
            assert page.detail_scroll_area.mapTo(window, QPoint(0, 0)).y() == (
                section_title.mapTo(window, QPoint(0, 0)).y()
            )
            assert page.card_column_count == expected_columns
            assert window.navigationInterface.panel.displayMode.name == "EXPAND"
            assert scroll_area.width() > 0
            assert scroll_area.horizontalScrollBar().maximum() == 0
            assert tuple(page.machine_cards) == cards
            assert controller.list_enabled_machines.call_count == query_count

            # 每列均分机器列表，最后一列紧贴详情卡。
            row_cards = cards[:expected_columns]
            row_widths = [card.width() for card in row_cards]
            card_widths[width] = row_widths[0]
            assert min(row_widths) >= 280
            assert max(row_widths) - min(row_widths) <= 1
            assert row_cards[0].mapTo(window, QPoint(0, 0)).x() == (
                scroll_area.mapTo(window, QPoint(0, 0)).x()
            )
            for left_card, right_card in zip(row_cards, row_cards[1:]):
                left_right = left_card.mapTo(window, QPoint(left_card.width(), 0)).x()
                right_left = right_card.mapTo(window, QPoint(0, 0)).x()
                assert right_left - left_right == 16
            last_card_right = row_cards[-1].mapTo(
                window, QPoint(row_cards[-1].width(), 0)
            ).x()
            detail_left = detail_panel.mapTo(window, QPoint(0, 0)).x()
            assert detail_left - last_card_right == 16
            detail_right = detail_panel.mapTo(
                window, QPoint(detail_panel.width(), 0)
            ).x()
            summary_card = page.today_detection_card
            summary_right = summary_card.mapTo(
                window, QPoint(summary_card.width(), 0)
            ).x()
            assert detail_right == summary_right

            # 核对两张总览卡的高度、宽度比例和内部指标。
            overview_card = page.device_overview_card
            assert overview_card.height() == summary_card.height() == 128
            assert overview_card.width() > summary_card.width()
            assert abs(overview_card.width() / summary_card.width() - 11 / 9) < 0.01
            assert len(page.findChildren(QFrame, "summaryCard")) == 2
            assert overview_card.findChild(QLabel, "summaryTitle").text() == "设备总览"
            assert summary_card.findChild(QLabel, "summaryTitle").text() == "今日检测"
            assert summary_card.recognition_value.text() == "128"
            assert summary_card.pending_review_value.text() == "6"

            if width == 1320:
                assert scroll_area.verticalScrollBar().maximum() > 0
                last_row = page.cards_layout.itemAtPosition(2, 0)
                assert last_row.widget() is cards[4]

        assert card_widths[1320] < card_widths[1400]
        assert card_widths[1600] < card_widths[1920] < card_widths[2200]

        # 窗口不能缩小到两列机器卡片无法完整显示的宽度。
        window.resize(1000, 700)
        qt_application.processEvents()
        assert window.width() == 1320
        assert window.height() == 720
        assert window.realtime_page.card_column_count == 2

        # 机器名称保持可读宽度，窄表格使用内部水平滚动。
        window.switch_page("machines")
        qt_application.processEvents()
        assert window.machines_page.table.columnWidth(1) >= 220
        assert window.machines_page.table.horizontalScrollBar().maximum() > 0
    finally:
        window.close()
        window.deleteLater()


def test_machine_list_fills_available_width_with_few_machines(
    qt_application: QApplication,
) -> None:
    """验证机器较少时列表仍填满详情卡左侧。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 单台和两台机器都不改变列表与详情的位置
    """
    controller = make_ui_controller()
    machine_data = controller.list_enabled_machines.return_value.data
    all_machines = machine_data["machines"]
    machine_data["machines"] = all_machines[:1]
    window = MainWindow(controller)
    try:
        window.resize(1320, 720)
        window.show()
        qt_application.processEvents()
        page = window.realtime_page
        assert page.card_column_count == 2
        list_width = page.scroll_area.width()
        detail_left = page.detail_panel.mapTo(window, QPoint(0, 0)).x()
        assert list_width > 2 * 280 + 16
        assert page.machine_cards[0].width() >= 280
        assert page.scroll_area.horizontalScrollBar().maximum() == 0

        # 加入第二台机器后列表和详情位置保持不变。
        machine_data["machines"] = all_machines[:2]
        page.reload_machines()
        qt_application.processEvents()
        assert page.card_column_count == 2
        assert page.scroll_area.width() == list_width
        assert page.detail_panel.mapTo(window, QPoint(0, 0)).x() == detail_left
        assert abs(page.machine_cards[0].width() - page.machine_cards[1].width()) <= 1
        assert page.scroll_area.horizontalScrollBar().maximum() == 0
    finally:
        window.close()
        window.deleteLater()


def test_summary_cards_use_light_shadows_and_metric_colors(qt_application) -> None:
    """确认顶部两张卡使用轻量阴影，并仅对非零异常数字着色。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        返回示例：
            None  # 顶部卡片样式和零值恢复已核对，机器卡与详情没有新增阴影
    """
    # 创建使用项目 QSS 的真实窗口。
    window = MainWindow(make_ui_controller())
    try:
        window.show()
        qt_application.processEvents()
        page = window.realtime_page

        # 核对两张总览卡的阴影、图标和分隔线。
        for card, separator_count in (
            (page.device_overview_card, 2),
            (page.today_detection_card, 1),
        ):
            shadow = card.graphicsEffect()
            assert isinstance(shadow, QGraphicsDropShadowEffect)
            assert shadow.blurRadius() == 20
            assert shadow.offset().x() == 0
            assert shadow.offset().y() == 3
            assert shadow.color() == QColor(16, 24, 40, 18)
            assert card.cursor().shape() == Qt.CursorShape.ArrowCursor
            icon_container = card.findChild(QLabel, "summaryIconContainer")
            assert icon_container.width() == icon_container.height() == 32
            assert not icon_container.pixmap().isNull()
            assert len(card.findChildren(QFrame, "summaryMetricSeparator")) == (
                separator_count
            )

        # 非零故障为红色，待复核为橙色。
        page.device_overview_card.set_values(5, 3, 1)
        page.today_detection_card.set_values(128, 6)
        fault_value = page.device_overview_card.fault_value
        pending_value = page.today_detection_card.pending_review_value
        assert fault_value.palette().color(QPalette.ColorRole.WindowText) == (
            QColor("#B42318")
        )
        assert pending_value.palette().color(QPalette.ColorRole.WindowText) == (
            QColor("#A76200")
        )

        # 数字归零后恢复普通深灰。
        page.device_overview_card.set_values(5, 3, 0)
        page.today_detection_card.set_values(128, 0)
        assert fault_value.palette().color(QPalette.ColorRole.WindowText) == (
            QColor("#182230")
        )
        assert pending_value.palette().color(QPalette.ColorRole.WindowText) == (
            QColor("#182230")
        )

        # 机器卡片和详情继续使用原有表面。
        assert all(card.graphicsEffect() is None for card in page.machine_cards)
        assert page.detail_panel.graphicsEffect() is None
    finally:
        window.close()
        window.deleteLater()


def test_today_detection_refreshes_after_storage_and_page_show(qt_application) -> None:
    """确认今日统计在正式入库、手动刷新和返回页面时重新查询。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        返回示例：
            None  # OCR 回调未提前计数，入库与页面刷新均读取正式统计
    """
    # 创建真实窗口并记录初始化后的统计查询次数。
    controller = make_ui_controller()
    window = MainWindow(controller)
    page = window.realtime_page
    try:
        assert controller.get_today_measurement_summary.call_count == 1
        window.show()
        qt_application.processEvents()
        query_count = controller.get_today_measurement_summary.call_count

        # OCR 结果和正在执行的入库阶段不触发统计查询。
        page.update_measurement_progress("1", "session", "session_start", "success")
        page.update_ocr_result("1", "session", ("003",), ("003",))
        page.update_measurement_progress("1", "session", "evidence_storage", "running")
        assert controller.get_today_measurement_summary.call_count == query_count

        # 正式入库成功后读取新的统计值。
        controller.get_today_measurement_summary.return_value = Result.ok({
            "recognition_count": 129,
            "pending_review_count": 6,
        })
        page.update_measurement_progress("1", "session", "evidence_storage", "success")
        assert controller.get_today_measurement_summary.call_count == query_count + 1
        assert page.today_detection_card.recognition_value.text() == "129"

        # 旧周期的迟到入库通知不影响当前统计。
        page.update_measurement_progress("1", "old", "evidence_storage", "success")
        assert controller.get_today_measurement_summary.call_count == query_count + 1

        # 点击现有刷新按钮时重新读取统计。
        page.refresh_button.click()
        assert controller.get_today_measurement_summary.call_count == query_count + 2

        # 从其他页面返回后同步已完成复核的统计。
        window.switch_page("history")
        qt_application.processEvents()
        controller.get_today_measurement_summary.return_value = Result.ok({
            "recognition_count": 129,
            "pending_review_count": 0,
        })
        window.switch_page("realtime")
        qt_application.processEvents()
        assert controller.get_today_measurement_summary.call_count == query_count + 3
        assert page.today_detection_card.pending_review_value.text() == "0"
        pending_value = page.today_detection_card.pending_review_value
        assert pending_value.property("tone") == "normal"
    finally:
        window.close()
        window.deleteLater()


def test_today_detection_failure_keeps_monitoring_updates(
    qt_application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """确认统计查询失败时显示占位符且继续更新实时监测。

    Args:
        qt_application: Qt 应用实例。
        monkeypatch: pytest 提供的属性替换工具。
        caplog: pytest 提供的日志捕获工具。

    Returns:
        返回示例：
            None  # 统计失败没有弹窗或阻断状态与进度更新，恢复后正常显示
    """
    # 准备统计读取失败并记录永久错误提示调用。
    controller = make_ui_controller()
    controller.get_today_measurement_summary.return_value = Result.error("读取失败")
    error_notification = Mock()
    monkeypatch.setattr("ui.pages.realtime_page.InfoBar.error", error_notification)
    page = RealtimePage(controller)
    try:
        page.show()
        qt_application.processEvents()
        assert page.today_detection_card.recognition_value.text() == "--"
        assert page.today_detection_card.pending_review_value.text() == "--"
        assert "今日检测统计读取失败" in caplog.text

        # 统计失败期间继续接收机器状态和本轮进度。
        page.update_machine_status("1", "online")
        page.update_measurement_progress("1", "session", "session_start", "success")
        page.update_measurement_progress("1", "session", "evidence_storage", "success")
        assert page.device_overview_card.online_value.text() == "1"
        assert page.cards_by_machine_id["1"].state_label.text() == "证据入库已完成"
        error_notification.assert_not_called()

        # 下一次成功查询恢复真实数量。
        controller.get_today_measurement_summary.return_value = Result.ok({
            "recognition_count": 0,
            "pending_review_count": 0,
        })
        page.refresh_today_detection_summary()
        assert page.today_detection_card.recognition_value.text() == "0"
        assert page.today_detection_card.pending_review_value.text() == "0"
        pending_value = page.today_detection_card.pending_review_value
        assert pending_value.property("tone") == "normal"
    finally:
        page.close()
        page.deleteLater()


def test_machine_detail_keeps_long_ocr_text_selectable(qt_application) -> None:
    """验证详情保留全文且大量 OCR 不撑高摘要卡。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 全文可复制，摘要高度固定
    """
    page = RealtimePage(make_ui_controller())
    try:
        page.show()
        qt_application.processEvents()
        card = page.machine_cards[0]
        ordered_lines = tuple(f"{number:020d}" for number in range(40))
        page.update_measurement_progress("1", "first", "session_start", "success")
        qt_application.processEvents()
        initial_height = card.sizeHint().height()
        page.update_ocr_result("1", "first", ordered_lines, ordered_lines)
        qt_application.processEvents()

        # 核对全文、只读属性和剪贴板复制。
        editor = page.detail_panel.ocr_text
        assert editor.parentWidget().objectName() == "detailOcrPanel"
        assert editor.frameShape() == QFrame.Shape.NoFrame
        assert editor.height() == 110
        assert all(line in editor.toPlainText() for line in ordered_lines)
        assert ordered_lines[-1] in editor.toPlainText()
        assert editor.isReadOnly()
        editor.selectAll()
        editor.copy()
        assert QApplication.clipboard().text() == editor.toPlainText()
        assert card.ocr_result_label.text() == ordered_lines[0]
        assert card.sizeHint().height() == initial_height
    finally:
        page.close()
        page.deleteLater()


def test_machine_card_shows_distinct_statuses(qt_application) -> None:
    """验证机器卡片分别展示整体状态、相机状态和当前流程。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 卡片文案、状态色点和详情序列号保持对应
    """
    page = RealtimePage(make_ui_controller())
    try:
        page.show()
        qt_application.processEvents()
        card = page.machine_cards[0]
        card_labels = [label.text() for label in card.findChildren(QLabel)]
        assert "Camera · CAM-1" not in card_labels
        assert "当前流程" in card_labels
        assert "本轮识别" in card_labels
        assert card.badge.text() == "离线"
        assert card.badge.property("tone") == "idle"
        assert page.detail_panel.badge.text() == "离线"
        assert card.camera_status_label.text() == "未启动"
        assert card.camera_status_dot.property("tone") == "idle"
        assert card.state_label.text() == "未启动监测"
        assert page.detail_panel.camera_serial_label.text() == "CAM-1"

        # 核对相机连接状态只更新相机色点和当前流程。
        page.update_connection_state("1", "连接中", "")
        assert card.badge.text() == "离线"
        assert card.camera_status_dot.property("tone") == "waiting"
        assert card.state_label.text() == "正在连接相机"
        page.update_connection_state("1", "相机已连接", "")
        assert card.badge.text() == "离线"
        assert card.camera_status_label.text() == "相机已连接"
        assert card.camera_status_dot.property("tone") == "normal"
        assert card.state_label.text() == "等待启停信号"

        # 后端发布在线后同步卡片和详情胶囊。
        page.update_machine_status("1", "online")
        assert card.badge.text() == "在线"
        assert card.property("tone") == "running"
        assert card.badge.property("tone") == "running"
        assert page.detail_panel.badge.text() == "在线"
        assert page.detail_panel.badge.property("tone") == "running"

        # 核对测量阶段和频率更新不会改变相机连接状态。
        card.set_frequency(24.6)
        page.update_measurement_progress("1", "first", "session_start", "success")
        page.update_measurement_progress(
            "1",
            "first",
            "character_recognition",
            "running",
        )
        assert card.badge.text() == "在线"
        assert card.state_label.text() == "字符识别进行中"
        assert card.frequency_label.text() == "24.6 Hz"
        assert card.camera_status_label.text() == "相机已连接"

        # 核对相机故障和测量失败分别显示在相机状态与当前流程。
        page.update_connection_state("1", "相机故障", "故障原因")
        page.update_measurement_progress(
            "1",
            "first",
            "character_recognition",
            "running",
        )
        assert card.badge.text() == "在线"
        assert card.state_label.text() == "字符识别进行中"
        page.update_measurement_progress(
            "1",
            "first",
            "character_recognition",
            "failed",
        )
        assert card.badge.text() == "在线"
        assert card.camera_status_label.text() == "相机故障"
        assert card.camera_status_dot.property("tone") == "error"
        assert card.state_label.text() == "字符识别失败"
        assert page.detail_panel.badge.text() == "在线"
        assert page.detail_panel.state_label.text() == "字符识别失败"
        assert "#138B3F" not in page.detail_panel.state_label.styleSheet()

        # 后端登记采集故障后才更新整体状态。
        page.update_machine_status("1", "fault")
        page.update_measurement_progress("1", "first", "image_capture", "failed")
        assert card.badge.text() == "故障"
        assert card.badge.property("tone") == "error"
        assert page.detail_panel.badge.text() == "故障"
        assert card.state_label.text() == "图像采集失败"

        # 后端释放资源后同步卡片和详情的离线状态。
        page.update_machine_status("1", "offline")
        page.finish_monitoring("")
        assert card.badge.text() == "离线"
        assert card.property("tone") == "idle"
        assert page.detail_panel.badge.text() == "离线"
        assert page.detail_panel.badge.property("tone") == "idle"

        # 新监测启动前清空上一轮整体状态并等待后端通知。
        page.controller.start_monitoring.return_value = Result.ok()
        page.start_monitoring()
        assert page.machine_statuses_by_machine_id == {}
        assert page.cards_by_machine_id["1"].badge.text() == "离线"
        assert page.detail_panel.badge.text() == "离线"
        assert page.cards_by_machine_id["1"].camera_status_label.text() == "连接中"
    finally:
        page.close()
        page.deleteLater()


def test_machine_detail_sections_follow_selected_machine(qt_application) -> None:
    """验证频率、OCR、步骤和占位统计随详情卡完整显示。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 分区结构和现有状态信号保持可用
    """
    page = RealtimePage(make_ui_controller())
    try:
        page.show()
        qt_application.processEvents()
        panel = page.detail_panel

        # 核对详情分区与三项固定占位统计。
        assert panel.findChild(QFrame, "detailFrequencyCard") is not None
        assert panel.findChild(QFrame, "detailOcrCard") is not None
        assert panel.findChild(QFrame, "detailStatsBar") is not None
        assert [label.text() for label in panel.stat_values.values()] == [
            "--", "--", "--"
        ]
        assert panel.frequency_label.text() == "--"

        # 连接和本轮进度继续刷新当前机器的详情。
        page.update_connection_state("1", "相机已连接", "")
        assert panel.camera_state_label.text() == "相机已连接"
        page.update_measurement_progress("1", "first", "session_start", "success")
        assert "#138B3F" not in panel.state_label.styleSheet()
        assert "#2563EB" in panel.steps.dots[0].styleSheet()
        page.select_machine("2")
        assert panel.camera_serial_label.text() == "CAM-2"
        assert [label.text() for label in panel.stat_values.values()] == [
            "--", "--", "--"
        ]
    finally:
        page.close()
        page.deleteLater()


def test_dashboard_selection_reload_and_summary(qt_application) -> None:
    """验证机器选择、重载保持和总览状态更新。

    Args:
        qt_application: Qt 应用实例。

    Returns:
        None  # 选择、详情和统计随状态同步
    """
    controller = make_ui_controller()
    page = RealtimePage(controller)
    try:
        page.show()
        qt_application.processEvents()
        assert page.selected_machine_id == "1"
        QTest.mouseClick(page.machine_cards[1], Qt.MouseButton.LeftButton)
        assert page.selected_machine_id == "2"
        assert sum(card.property("selected") for card in page.machine_cards) == 1
        assert page.detail_panel.title.text() == "第 2 台机器"
        assert page.detail_panel.camera_serial_label.text() == "CAM-2"
        assert page.detail_panel.frequency_meter_serial_label.text() == "FREQ-2"
        attributes_layout = page.detail_panel.attributes_layout
        assert isinstance(attributes_layout, QGridLayout)
        camera_widget = attributes_layout.itemAtPosition(1, 0).widget()
        assert camera_widget is page.detail_panel.camera_serial_label
        meter_widget = attributes_layout.itemAtPosition(1, 1).widget()
        assert meter_widget is page.detail_panel.frequency_meter_serial_label
        state_widget = attributes_layout.itemAtPosition(4, 0).widget()
        assert state_widget is page.detail_panel.state_label
        camera_state_widget = attributes_layout.itemAtPosition(4, 1).widget()
        assert camera_state_widget is page.detail_panel.camera_state_label
        assert page.detail_panel.frequency_label.text() == "--"
        assert set(page.detail_panel.stat_values) == {
            "运行时长", "今日识别数量", "今日待复核数量"
        }
        assert all(
            label.text() == "--" for label in page.detail_panel.stat_values.values()
        )
        assert not controller.list_measurement_records.called
        page.reload_machines()
        assert page.selected_machine_id == "2"

        # 设备总览只统计后端发布的机器整体状态。
        query_count = controller.list_enabled_machines.call_count
        page.update_connection_state("1", "相机已连接", "")
        page.update_machine_status("1", "online")
        page.update_measurement_progress("2", "first", "session_start", "success")
        page.update_connection_state("2", "相机故障", "故障")
        page.update_measurement_progress("2", "first", "image_capture", "failed")
        page.update_machine_status("2", "fault")
        assert page.device_overview_card.total_value.text() == "5"
        assert page.device_overview_card.online_value.text() == "1"
        assert page.device_overview_card.fault_value.text() == "1"
        assert page.detail_panel.badge.text() == "故障"
        assert page.detail_panel.camera_state_label.text() == "相机故障"

        # 单独登记的机器级故障也进入总览。
        page.update_machine_status("3", "fault")
        assert page.device_overview_card.fault_value.text() == "2"

        # 刷新机器列表后恢复后端整体状态和选中详情。
        page.reload_machines()
        assert page.cards_by_machine_id["2"].badge.text() == "故障"
        assert page.cards_by_machine_id["3"].badge.text() == "故障"
        assert page.detail_panel.badge.text() == "故障"
        query_count = controller.list_enabled_machines.call_count

        page.update_cycle_closed("2", "first")
        assert page.device_overview_card.online_value.text() == "1"
        assert page.device_overview_card.fault_value.text() == "2"

        # 后端释放资源后发布离线，设备总览随之归零。
        for machine_id in ("1", "2", "3"):
            page.update_machine_status(machine_id, "offline")
        page.finish_monitoring("")
        assert page.device_overview_card.online_value.text() == "0"
        assert page.device_overview_card.fault_value.text() == "0"
        assert controller.list_enabled_machines.call_count == query_count

        # 删除选中机器后退回首台，空列表清空详情。
        remaining_machines = page.machines[:1]
        machine_data = controller.list_enabled_machines.return_value.data
        machine_data["machines"] = remaining_machines
        page.reload_machines()
        assert page.selected_machine_id == "1"
        controller.list_enabled_machines.return_value.data["machines"] = []
        page.reload_machines()
        assert page.selected_machine_id is None
        assert page.detail_panel.title.text() == "未选择机器"
    finally:
        page.close()
        page.deleteLater()



def test_machine_editor_and_delete_confirmation_use_mask_and_cancel(
    qt_application: QApplication,
    monkeypatch,
) -> None:
    """验证机器表单遮罩与删除确认的默认取消操作。

    Args:
        qt_application: 测试使用的 Qt 应用。
        monkeypatch: pytest 对象替换工具。

    Returns:
        None  # 表单遮罩不可点穿且取消确认不删除机器
    """
    controller = make_ui_controller()
    window = MainWindow(controller)
    try:
        window.show()
        window.switch_page("machines")
        qt_application.processEvents()
        assert isinstance(window.machines_page.editor, MaskDialogBase)
        assert not window.machines_page.editor.isClosableOnMaskClicked()

        # 检查取消按钮默认选中并模拟取消结果。
        def cancel_confirmation(message_box: MessageBox) -> int:
            """检查确认框后返回取消结果。

            Args:
                message_box: 删除确认框。

            Returns:
                0  # 用户取消删除
            """
            message_box.show()
            message_box.window().activateWindow()
            QTest.qWait(50)
            qt_application.processEvents()
            assert message_box.cancelButton.isDefault()
            assert not message_box.yesButton.isDefault()
            assert message_box.cancelButton.hasFocus()
            message_box.close()
            return 0

        monkeypatch.setattr(MessageBox, "exec", cancel_confirmation)
        window.machines_page.delete_machine(1)
        controller.delete_machine.assert_not_called()

        # 明确确认后才调用删除接口。
        controller.delete_machine.return_value = Result.ok()
        monkeypatch.setattr(MessageBox, "exec", lambda message_box: 1)
        window.machines_page.delete_machine(1)
        controller.delete_machine.assert_called_once_with(1)
    finally:
        window.close()
        window.deleteLater()


def test_machine_editor_preserves_validation_and_save_order(
    qt_application: QApplication,
    monkeypatch,
) -> None:
    """验证机器表单失败时保留输入并在成功后关闭。

    Args:
        qt_application: 测试使用的 Qt 应用。
        monkeypatch: pytest 对象替换工具。

    Returns:
        None  # 表单校验、字段定位和成功关闭顺序均正确
    """
    controller = make_ui_controller()
    controller.create_machine.return_value = Result.error(
        "相机序列号已存在。", data={"field": "camera_serial"}
    )
    messages = Mock()
    monkeypatch.setattr(InfoBar, "error", messages)
    window = MainWindow(controller)
    try:
        window.show()
        window.switch_page("machines")
        page = window.machines_page
        page.create_machine()
        qt_application.processEvents()

        # 空白表单先提示必填项，不提交保存请求。
        page.save_button.click()
        controller.create_machine.assert_not_called()
        assert page.editor.isVisible()
        assert messages.call_args.args[1] == "请填写机器名称。"
        assert messages.call_args.kwargs["parent"] is page.editor.widget

        # 去空白后提交字段，并将重复错误定位到相机序列号。
        page.field_inputs["machine_name"].setText("  新机器  ")
        page.field_inputs["camera_serial"].setText("  CAM-NEW  ")
        page.field_inputs["frequency_meter_serial"].setText("  FREQ-NEW  ")
        page.remark_input.setPlainText("  ")
        page.save_button.click()
        controller.create_machine.assert_called_once_with(
            machine_name="新机器",
            camera_serial="CAM-NEW",
            frequency_meter_serial="FREQ-NEW",
            enabled=True,
            remark=None,
        )
        assert page.editor.isVisible()
        assert page.field_inputs["camera_serial"].hasFocus()

        # 保存成功后关闭表单，再读取并选中列表记录。
        controller.create_machine.return_value = Result.ok({"machine_id": 1})
        page.save_button.click()
        QTest.qWait(300)
        assert not page.editor.isVisible()
        assert not page.save_button.isEnabled()
        assert page.table.selectedItems()[0].text() == "1"
        page.create_machine()
        assert page.save_button.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_fluent_window_waits_for_monitoring_before_close(
    qt_application: QApplication,
) -> None:
    """验证重复关闭仅请求停止一次，监测结束后窗口关闭。

    Args:
        qt_application: 测试使用的 Qt 应用。

    Returns:
        None  # 监测运行时关闭被拦截，结束信号发出后窗口关闭
    """
    machine_service = Mock()
    machine_service.list_enabled_machines.return_value = {"machines": []}
    machine_service.list_machines.return_value = {"machines": []}
    controller = AppController(machine_service, Mock(), Mock(), Path("config"))
    controller.measurement_record_service.get_daily_summary.return_value = {
        "recognition_count": 0,
        "pending_review_count": 0,
    }
    runtime_thread = Mock()
    controller.runtime_thread = runtime_thread
    controller.stop_monitoring = Mock(wraps=controller.stop_monitoring)
    window = MainWindow(controller)
    try:
        # 运行中的首次关闭等待监测清理。
        window.show()
        qt_application.processEvents()
        assert window.isVisible()
        window.close()
        qt_application.processEvents()
        assert window.isVisible()
        assert window.realtime_page.closing_requested
        controller.stop_monitoring.assert_called_once_with()
        runtime_thread.stop_requested.set.assert_called_once_with()

        # 再次关闭仍等待结束，且不重复提交停止请求。
        window.close()
        qt_application.processEvents()
        assert window.isVisible()
        controller.stop_monitoring.assert_called_once_with()
        runtime_thread.stop_requested.set.assert_called_once_with()

        # 模拟 Controller 清理线程后发出结束信号。
        controller.runtime_thread = None
        assert not controller.is_monitoring_running().data["running"]
        controller.monitoring_finished_signal.emit("")
        qt_application.processEvents()
        assert not window.isVisible()
    finally:
        controller.runtime_thread = None
        window.close()
        window.deleteLater()
