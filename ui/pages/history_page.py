"""展示已保存的测量历史并处理人工复核。"""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.controller.controller import AppController
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
    """组织测量历史筛选、表格、详情和人工复核。"""

    def __init__(self, controller: AppController) -> None:
        """建立历史记录页面并连接筛选交互。

        Args:
            controller: 界面业务控制器。

        Returns:
            返回示例：
                None  # 页面控件已建立，进入页面时再读取历史记录
        """
        super().__init__()
        self.setObjectName("history")
        self.controller = controller
        self.selected_review_status: str | None = None

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
        subtitle = QLabel("查看已保存的测量结果并处理待复核记录")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        layout.addLayout(header)

        # 建立状态按钮和机器筛选框。
        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.status_buttons: dict[str | None, QPushButton] = {}
        status_group = QButtonGroup(self)
        status_group.setExclusive(True)
        for review_status, caption in (
            (None, "全部"),
            ("normal", "正常"),
            ("pending", "待复核"),
            ("reviewed", "已复核"),
        ):
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

        # 创建共用的详情弹窗。
        self.build_detail_dialog()

    def build_detail_dialog(self) -> None:
        """创建历史记录的共用详情弹窗。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 详情字段、人工复核控件和关闭按钮已建立
        """
        # 创建弹窗和基本字段展示区。
        self.detail_dialog = QDialog(self)
        self.detail_dialog.setObjectName("historyDetail")
        self.detail_dialog.setWindowTitle("测量记录详情")
        self.detail_dialog.setWindowFlag(
            Qt.WindowType.WindowContextHelpButtonHint, False
        )
        self.detail_dialog.resize(640, 780)
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
            if field_name == "evidence_directory":
                value_label.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
                )
            fields.addRow(caption + "：", value_label)
            self.detail_values[field_name] = value_label
        detail_layout.addLayout(fields)

        # 创建可滚动的原始 OCR 文字展示区。
        detail_layout.addWidget(QLabel("原始 OCR 结果："))
        self.detail_ocr_text = QPlainTextEdit()
        self.detail_ocr_text.setReadOnly(True)
        self.detail_ocr_text.setFixedHeight(100)
        detail_layout.addWidget(self.detail_ocr_text)

        # 创建只供待复核记录显示的原因区域。
        self.review_reason_title = QLabel("复核原因：")
        self.review_reason_value = QLabel()
        self.review_reason_value.setWordWrap(True)
        self.review_reason_value.setTextInteractionFlags(selectable_text)
        detail_layout.addWidget(self.review_reason_title)
        detail_layout.addWidget(self.review_reason_value)

        # 创建已复核记录的时间和最终文字展示区。
        self.reviewed_at_title = QLabel("复核时间：")
        self.reviewed_at_value = QLabel()
        detail_layout.addWidget(self.reviewed_at_title)
        detail_layout.addWidget(self.reviewed_at_value)
        self.final_result_title = QLabel("人工最终结果：")
        self.final_result_text = QPlainTextEdit()
        self.final_result_text.setReadOnly(True)
        self.final_result_text.setFixedHeight(100)
        detail_layout.addWidget(self.final_result_title)
        detail_layout.addWidget(self.final_result_text)

        # 创建可滚动的证据图片缩略图区域。
        detail_layout.addWidget(QLabel("证据图片："))
        self.evidence_scroll = QScrollArea()
        self.evidence_scroll.setWidgetResizable(True)
        self.evidence_scroll.setFixedHeight(150)
        self.evidence_content = QWidget()
        self.evidence_grid = QGridLayout(self.evidence_content)
        self.evidence_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.evidence_scroll.setWidget(self.evidence_content)
        detail_layout.addWidget(self.evidence_scroll)

        # 创建待复核记录的文字编辑区和完成按钮。
        self.review_editor_title = QLabel("人工复核结果（每条一行）：")
        self.review_editor = QPlainTextEdit()
        self.review_editor.setFixedHeight(100)
        detail_layout.addWidget(self.review_editor_title)
        detail_layout.addWidget(self.review_editor)
        actions = QHBoxLayout()
        self.confirm_review_button = QPushButton("确认无误")
        self.confirm_review_button.clicked.connect(
            lambda: self.complete_record_review(False)
        )
        self.save_review_button = QPushButton("保存并完成复核")
        self.save_review_button.clicked.connect(
            lambda: self.complete_record_review(True)
        )
        actions.addWidget(self.confirm_review_button)
        actions.addWidget(self.save_review_button)
        actions.addStretch()

        # 在弹窗底部放置关闭入口。
        close_button = QPushButton("关闭")
        close_button.setProperty("buttonRole", "secondary")
        close_button.clicked.connect(self.detail_dialog.close)
        actions.addWidget(close_button)
        detail_layout.addLayout(actions)

    def refresh_history(self) -> None:
        """进入历史页时更新机器选项和当前筛选结果。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器选项与历史列表已刷新，读取失败时显示提示
        """
        # 读取有历史记录的机器。
        result = self.controller.list_record_machines()
        if not result.success:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "历史记录读取失败", result.message)
            return
        machines = result.data

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

    def select_status(self, review_status: str | None) -> None:
        """切换历史状态筛选并重新读取记录。

        Args:
            review_status: None 表示全部，字符串表示对应查询状态。

        Returns:
            返回示例：
                None  # 列表已按所选状态刷新
        """
        self.selected_review_status = review_status
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
        result = self.controller.list_measurement_records(
            self.selected_review_status, self.machine_filter.currentData()
        )
        if not result.success:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "历史记录读取失败", result.message)
            return
        records = result.data

        # 将每条记录填入六列表格。
        self.table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            final_lines = record["reviewed_lines"]
            if final_lines is None:
                final_lines = record["ordered_lines"]
            summary_lines = (line.replace("\n", " ") for line in final_lines[:2])
            summary = "；".join(summary_lines) or "--"
            if len(final_lines) > 2 or len(summary) > 60:
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

            # 用不同底色标出正常、待复核和已复核状态。
            if not record["needs_review"]:
                status_text, foreground, background = "正常", "#138B3F", "#DCF8E9"
            elif record["reviewed_at"] is None:
                status_text, foreground, background = "待复核", "#B96600", "#FFF0D8"
            else:
                status_text, foreground, background = "已复核", "#2462A8", "#E2EEFF"
            status_item = QTableWidgetItem(status_text)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setForeground(QColor(foreground))
            status_item.setBackground(QColor(background))
            self.table.setItem(row_index, 4, status_item)

            # 为当前记录建立详情入口。
            button = QPushButton("查看")
            button.setProperty("buttonRole", "text")
            session_id = record["session_id"]
            button.clicked.connect(
                lambda checked=False, cycle=session_id: self.show_record_detail(cycle)
            )
            self.table.setCellWidget(row_index, 5, button)

    def show_record_detail(self, session_id: str) -> None:
        """读取一条测量记录并打开对应状态的详情弹窗。

        Args:
            session_id: 待查看的测量周期编号。

        Returns:
            返回示例：
                None  # 对应详情已显示，读取失败时显示提示
        """
        # 按周期编号读取完整记录。
        result = self.controller.get_measurement_record(session_id)
        if not result.success:
            QMessageBox.warning(self, "历史详情读取失败", result.message)
            return
        record = result.data
        if record is None:
            QMessageBox.warning(self, "历史详情读取失败", "该测量记录已不存在。")
            return

        # 填入机器、时间、状态、频率和证据目录。
        needs_review = record["needs_review"]
        reviewed_at = record["reviewed_at"]
        pending_review = needs_review and reviewed_at is None
        completed_review = needs_review and reviewed_at is not None
        self.detail_values["machine"].setText(record["machine_name"])
        self.detail_values["session_id"].setText(record["session_id"])
        start_time = format_history_time(record["start_time"])
        finish_time = format_history_time(record["finish_time"])
        self.detail_values["start_time"].setText(start_time)
        self.detail_values["finish_time"].setText(finish_time)
        status_text = "已复核" if completed_review else "待复核" if pending_review else "正常"
        self.detail_values["status"].setText(status_text)
        self.detail_values["frequency"].setText(
            format_history_frequency(record["final_frequency_hz"])
        )
        self.detail_values["evidence_directory"].setText(record["evidence_directory"])

        # 显示原始 OCR 文字和需要复核的原因。
        self.detail_ocr_text.setPlainText("\n".join(record["ordered_lines"]) or "--")
        self.review_reason_title.setVisible(needs_review)
        self.review_reason_value.setVisible(needs_review)
        reason_text = (record["review_reason"] or "--") if needs_review else ""
        self.review_reason_value.setText(reason_text)

        # 为已复核记录显示复核时间和人工最终结果。
        final_lines = record["reviewed_lines"]
        if final_lines is None:
            final_lines = record["ordered_lines"]
        self.reviewed_at_title.setVisible(completed_review)
        self.reviewed_at_value.setVisible(completed_review)
        self.reviewed_at_value.setText(
            format_history_time(reviewed_at) if completed_review else ""
        )
        self.final_result_title.setVisible(completed_review)
        self.final_result_text.setVisible(completed_review)
        self.final_result_text.setPlainText("\n".join(final_lines) or "--")

        # 为待复核记录预填原始文字并显示操作入口。
        self.review_editor_title.setVisible(pending_review)
        self.review_editor.setVisible(pending_review)
        self.review_editor.setPlainText("\n".join(record["ordered_lines"]))
        self.confirm_review_button.setVisible(pending_review)
        self.save_review_button.setVisible(pending_review)

        # 读取本轮证据目录并显示可用图片。
        self.populate_evidence_images(record["evidence_directory"])
        self.detail_dialog.open()

    def complete_record_review(self, use_edited_text: bool) -> None:
        """提交当前详情中的人工复核并刷新历史列表。

        Args:
            use_edited_text: True 保存编辑文字，False 确认原始文字。

        Returns:
            返回示例：
                None  # 复核已保存并刷新列表，失败时显示提示
        """
        # 将当前详情的周期编号和可选编辑文字交给 Controller。
        session_id = self.detail_values["session_id"].text()
        edited_text = self.review_editor.toPlainText() if use_edited_text else None
        result = self.controller.complete_measurement_review(session_id, edited_text)
        if not result.success:
            QMessageBox.warning(self, "人工复核未完成", result.message)
            if result.data is True:
                self.show_record_detail(session_id)
            return

        # 关闭详情并刷新当前状态及机器筛选下的列表。
        self.detail_dialog.close()
        self.reload_records()

    def populate_evidence_images(self, evidence_directory: str) -> None:
        """读取本轮 JPG 证据并更新详情缩略图。

        Args:
            evidence_directory: 测量记录保存的本轮证据目录。

        Returns:
            返回示例：
                None  # 缩略图或图片占位文字已显示
        """
        # 清除上一条记录的缩略图和占位文字。
        while self.evidence_grid.count():
            layout_item = self.evidence_grid.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        # 读取记录目录当前层的 JPG 文件。
        directory = Path(evidence_directory) if evidence_directory else None
        image_paths = (
            sorted(directory.glob("*.jpg"))
            if directory is not None and directory.is_dir()
            else []
        )
        readable_image_count = 0
        for image_path in image_paths:
            image = QPixmap(str(image_path))
            if image.isNull():
                continue

            # 为可读取的图片创建可点击缩略图。
            thumbnail = QPushButton()
            thumbnail.setObjectName("evidenceThumbnail")
            thumbnail.setToolTip(image_path.name)
            thumbnail.setAccessibleName(image_path.name)
            thumbnail_image = image.scaled(
                QSize(140, 100),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            thumbnail.setIcon(QIcon(thumbnail_image))
            thumbnail.setIconSize(QSize(140, 100))
            thumbnail.setFixedSize(160, 120)
            thumbnail.clicked.connect(
                lambda checked=False, path=image_path: self.show_evidence_image(path)
            )
            self.evidence_grid.addWidget(
                thumbnail, readable_image_count // 3, readable_image_count % 3
            )
            readable_image_count += 1

        # 在没有可显示图片时给出对应提示。
        if readable_image_count == 0:
            message = "证据图片读取失败" if image_paths else "暂无证据图片"
            self.evidence_grid.addWidget(QLabel(message), 0, 0)

        self.evidence_scroll.verticalScrollBar().setValue(0)

    def show_evidence_image(self, image_path: Path) -> None:
        """打开一张证据图片的大图查看窗口。

        Args:
            image_path: 已选证据图片的完整路径。

        Returns:
            返回示例：
                None  # 大图窗口已关闭
        """
        # 创建适配当前屏幕大小的临时查看窗口。
        image_dialog = QDialog(self.detail_dialog)
        image_dialog.setWindowTitle(image_path.name)
        screen_size = image_dialog.screen().availableGeometry().size()
        image_dialog.resize(
            min(900, screen_size.width() - 80),
            min(650, screen_size.height() - 80),
        )
        image_layout = QVBoxLayout(image_dialog)
        image_label = QLabel()
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 将图片等比例缩放到查看窗口的可用区域。
        image = QPixmap(str(image_path))
        if image.isNull():
            image_label.setText("图片读取失败")
        else:
            available_size = QSize(
                image_dialog.width() - 40,
                image_dialog.height() - 90,
            )
            image_label.setPixmap(
                image.scaled(
                    available_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        image_layout.addWidget(image_label, 1)

        # 添加关闭按钮并显示大图窗口。
        close_button = QPushButton("关闭")
        close_button.clicked.connect(image_dialog.accept)
        image_layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        image_dialog.exec()
        image_dialog.deleteLater()
