"""展示已保存的测量历史和只读详情。"""

from datetime import datetime

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.service.history_service import HistoryService, HistoryServiceError
from ui.theme import create_icon


def format_history_time(timestamp: str) -> str:
    """将保存的时间转换为本地显示时间。

    Args:
        timestamp: 测量记录中的 ISO 时间。

    Returns:
        返回示例：
            "2026-09-27 16:00:00"  # 本地时间
    """
    return datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_history_frequency(frequency: float | None) -> str:
    """将最终频率转换为列表和详情共用的文字。

    Args:
        frequency: 最终频率，None 表示没有有效读数。

    Returns:
        返回示例：
            "50.0 Hz"  # 有效频率
            "--"  # 没有有效频率
    """
    return "--" if frequency is None else f"{frequency:.1f} Hz"


class HistoryPage(QWidget):
    """组织测量历史筛选、表格和只读详情。"""

    def __init__(self, history_service: HistoryService) -> None:
        """建立历史记录页面并连接筛选交互。

        Args:
            history_service: 测量历史读取服务。

        Returns:
            返回示例：
                None  # 页面控件已建立，进入页面时再读取历史记录
        """
        super().__init__()
        self.setObjectName("history")
        self.history_service = history_service
        self.selected_review_status: bool | None = None

        # 创建页面标题和说明。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(create_icon("history", "blue").pixmap(QSize(24, 24)))
        header.addWidget(icon)
        heading = QVBoxLayout()
        title = QLabel("历史记录")
        title.setObjectName("pageTitle")
        subtitle = QLabel("查看已保存的正常和待复核测量结果")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        layout.addLayout(header)

        # 建立状态按钮和机器筛选框。
        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.status_buttons: dict[bool | None, QPushButton] = {}
        status_group = QButtonGroup(self)
        status_group.setExclusive(True)
        for review_status, caption in ((None, "全部"), (False, "正常"), (True, "待复核")):
            button = QPushButton(caption)
            button.setCheckable(True)
            button.setProperty("historyFilter", True)
            status_group.addButton(button)
            button.clicked.connect(
                lambda checked=False, status=review_status: self.select_status(status)
            )
            self.status_buttons[review_status] = button
            filters.addWidget(button)
        self.status_buttons[None].setChecked(True)
        filters.addSpacing(24)
        filters.addWidget(QLabel("机器："))
        self.machine_filter = QComboBox()
        self.machine_filter.setObjectName("historyMachineFilter")
        self.machine_filter.addItem("全部机器", None)
        self.machine_filter.setMinimumWidth(170)
        self.machine_filter.currentIndexChanged.connect(self.reload_records)
        filters.addWidget(self.machine_filter)
        filters.addStretch()
        layout.addLayout(filters)

        # 建立六列只读历史记录表格。
        content = QFrame()
        content.setObjectName("pageContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("historyTable")
        self.table.setHorizontalHeaderLabels((
            "时间", "机器", "OCR 结果摘要", "最终频率", "状态", "操作"
        ))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table_header = self.table.horizontalHeader()
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 170), (1, 140), (3, 110), (4, 100), (5, 80)):
            self.table.setColumnWidth(column, width)
        content_layout.addWidget(self.table)
        layout.addWidget(content, 1)

        # 创建共用的只读详情弹窗。
        self.build_detail_dialog()

    def build_detail_dialog(self) -> None:
        """创建历史记录的共用只读详情弹窗。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 详情字段和关闭按钮已建立
        """
        # 创建弹窗和基本字段展示区。
        self.detail_dialog = QDialog(self)
        self.detail_dialog.setObjectName("historyDetail")
        self.detail_dialog.setWindowTitle("测量记录详情")
        self.detail_dialog.setWindowFlag(
            Qt.WindowType.WindowContextHelpButtonHint, False
        )
        self.detail_dialog.resize(640, 590)
        detail_layout = QVBoxLayout(self.detail_dialog)
        fields = QFormLayout()
        self.detail_values: dict[str, QLabel] = {}
        selectable_text = Qt.TextInteractionFlag.TextSelectableByMouse
        for field_name, caption in (
            ("machine", "机器"),
            ("session_id", "Session ID"),
            ("start_time", "开始时间"),
            ("finish_time", "结束时间"),
            ("status", "状态"),
            ("frequency", "最终频率"),
            ("evidence_directory", "证据图片目录"),
        ):
            value_label = QLabel("--")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(selectable_text)
            fields.addRow(caption + "：", value_label)
            self.detail_values[field_name] = value_label
        detail_layout.addLayout(fields)

        # 创建可滚动的完整 OCR 文字展示区。
        detail_layout.addWidget(QLabel("OCR 完整结果："))
        self.detail_ocr_text = QPlainTextEdit()
        self.detail_ocr_text.setReadOnly(True)
        detail_layout.addWidget(self.detail_ocr_text, 1)

        # 创建只供待复核记录显示的原因区域。
        self.review_reason_title = QLabel("复核原因：")
        self.review_reason_value = QLabel()
        self.review_reason_value.setWordWrap(True)
        self.review_reason_value.setTextInteractionFlags(selectable_text)
        detail_layout.addWidget(self.review_reason_title)
        detail_layout.addWidget(self.review_reason_value)

        # 在弹窗底部放置关闭入口。
        close_button = QPushButton("关闭")
        close_button.setProperty("buttonRole", "secondary")
        close_button.clicked.connect(self.detail_dialog.close)
        detail_layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

    def refresh_history(self) -> None:
        """进入历史页时更新机器选项和当前筛选结果。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器选项与历史列表已刷新，读取失败时显示提示
        """
        # 读取有历史记录的机器。
        try:
            machines = self.history_service.list_record_machines()
        except HistoryServiceError as error:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "历史记录读取失败", str(error))
            return

        # 更新机器选项并保留仍然存在的筛选值。
        selected_machine_id = self.machine_filter.currentData()
        self.machine_filter.blockSignals(True)
        self.machine_filter.clear()
        self.machine_filter.addItem("全部机器", None)
        for machine in machines:
            machine_id = machine["machine_id"]
            machine_name = machine["machine_name"]
            caption = (
                f"{machine_name}（{machine_id}#）"
                if machine_name != machine_id else f"{machine_id}#"
            )
            self.machine_filter.addItem(caption, machine_id)
        selected_index = self.machine_filter.findData(selected_machine_id)
        self.machine_filter.setCurrentIndex(max(selected_index, 0))
        self.machine_filter.blockSignals(False)

        # 读取当前状态和机器条件下的记录。
        self.reload_records()

    def select_status(self, needs_review: bool | None) -> None:
        """切换正常或待复核筛选并重新读取记录。

        Args:
            needs_review: None 表示全部，布尔值表示对应复核状态。

        Returns:
            返回示例：
                None  # 列表已按所选状态刷新
        """
        self.selected_review_status = needs_review
        self.reload_records()

    def reload_records(self) -> None:
        """按当前状态和机器条件填充历史记录表格。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 表格显示符合筛选条件的测量记录
        """
        # 按当前筛选条件读取测量结果。
        try:
            records = self.history_service.list_records(
                self.selected_review_status, self.machine_filter.currentData()
            )
        except HistoryServiceError as error:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "历史记录读取失败", str(error))
            return

        # 将每条记录填入六列表格。
        self.table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            ordered_lines = record["ordered_lines"]
            summary_lines = (line.replace("\n", " ") for line in ordered_lines[:2])
            summary = "；".join(summary_lines) or "--"
            if len(ordered_lines) > 2 or len(summary) > 60:
                summary = summary[:59] + "…"
            values = (
                format_history_time(record["finish_time"]),
                record["machine_name"],
                summary,
                format_history_frequency(record["final_frequency_hz"]),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                self.table.setItem(row_index, column, item)

            # 用不同底色标出正常和待复核状态。
            review_status = record["needs_review"]
            status_item = QTableWidgetItem("待复核" if review_status else "正常")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setForeground(QColor("#B96600" if review_status else "#138B3F"))
            status_item.setBackground(QColor("#FFF0D8" if review_status else "#DCF8E9"))
            self.table.setItem(row_index, 4, status_item)

            # 为当前记录建立只读详情入口。
            button = QPushButton("查看")
            button.setProperty("buttonRole", "text")
            session_id = record["session_id"]
            button.clicked.connect(
                lambda checked=False, cycle=session_id: self.show_record_detail(cycle)
            )
            self.table.setCellWidget(row_index, 5, button)

    def show_record_detail(self, session_id: str) -> None:
        """读取一条测量记录并打开只读详情弹窗。

        Args:
            session_id: 待查看的测量周期编号。

        Returns:
            返回示例：
                None  # 对应详情已显示，读取失败时显示提示
        """
        # 按周期编号读取完整记录。
        try:
            record = self.history_service.get_record(session_id)
        except HistoryServiceError as error:
            QMessageBox.warning(self, "历史详情读取失败", str(error))
            return
        if record is None:
            QMessageBox.warning(self, "历史详情读取失败", "该测量记录已不存在。")
            return

        # 填入机器、时间、状态、频率和证据目录。
        review_status = record["needs_review"]
        self.detail_values["machine"].setText(record["machine_name"])
        self.detail_values["session_id"].setText(record["session_id"])
        start_time = format_history_time(record["start_time"])
        finish_time = format_history_time(record["finish_time"])
        self.detail_values["start_time"].setText(start_time)
        self.detail_values["finish_time"].setText(finish_time)
        self.detail_values["status"].setText("待复核" if review_status else "正常")
        self.detail_values["frequency"].setText(
            format_history_frequency(record["final_frequency_hz"])
        )
        self.detail_values["evidence_directory"].setText(record["evidence_directory"])

        # 显示完整 OCR 文字和待复核原因。
        self.detail_ocr_text.setPlainText("\n".join(record["ordered_lines"]) or "--")
        self.review_reason_title.setVisible(review_status)
        self.review_reason_value.setVisible(review_status)
        reason_text = (record["review_reason"] or "--") if review_status else ""
        self.review_reason_value.setText(reason_text)
        self.detail_dialog.open()
