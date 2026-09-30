"""展示已保存的测量历史并处理人工复核。"""

from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, QPoint, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CalendarPicker,
    CaptionLabel,
    CheckBox,
    ComboBox,
    Flyout,
    FlyoutAnimationType,
    FlyoutView,
    InfoBar,
    MaskDialogBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SimpleCardWidget,
    SubtitleLabel,
    TableWidget,
    TitleLabel,
    TransparentPushButton,
)

from src.controller.controller import AppController
from ui.theme import COLORS


MACHINE_COLUMN = 0
TIME_COLUMN = 1


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

    def __init__(
        self, controller: AppController, parent: QWidget | None = None
    ) -> None:
        """建立历史记录页面并连接筛选交互。

        Args:
            controller: 界面业务控制器。
            parent: 所属主窗口。

        Returns:
            返回示例：
                None  # 页面控件已建立，进入页面时再读取历史记录
        """
        super().__init__(parent)
        self.setObjectName("history")
        self.controller = controller

        # 保存已应用的筛选条件和可选机器。
        self.selected_review_status: str | None = None
        self.selected_machine_id: str | None = None
        self.record_machines = []
        self.selected_start_date: date | None = None
        self.selected_end_date: date | None = None

        # 初始化历史记录分页。
        self.current_page = 1
        self.page_size = 20

        # 创建页面标题和说明。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 4, 24, 20)
        layout.setSpacing(20)
        title = TitleLabel("历史记录")
        title.setObjectName("pageTitle")
        subtitle = BodyLabel("查看已保存的测量结果并处理待复核记录")
        subtitle.setObjectName("pageSubtitle")
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        layout.addLayout(heading)

        # 在筛选卡片中建立状态筛选。
        filter_card = SimpleCardWidget()
        filter_card.setObjectName("historyFilterCard")
        filter_layout = QVBoxLayout(filter_card)
        filter_layout.setContentsMargins(20, 16, 20, 16)
        filter_layout.setSpacing(12)
        filters = QHBoxLayout()
        filters.setSpacing(12)
        self.status_filter = SegmentedWidget(self)
        status_values = {
            "all": None,
            "normal": "normal",
            "pending": "pending",
            "reviewed": "reviewed",
        }
        self.status_buttons = {}
        for route_key, caption in (
            ("all", "全部"),
            ("normal", "正常"),
            ("pending", "待复核"),
            ("reviewed", "已复核"),
        ):
            review_status = status_values[route_key]
            segment = self.status_filter.addItem(
                route_key,
                caption,
                onClick=lambda checked=False, status=review_status: (
                    self.select_status(status)
                ),
            )
            self.status_buttons[review_status] = segment
            self.status_buttons[route_key] = segment
        self.status_filter.setCurrentItem("all")
        filters.addWidget(self.status_filter)
        filters.addStretch()
        filter_layout.addLayout(filters)

        layout.addWidget(filter_card)

        # 建立六列只读历史记录表格。
        content = SimpleCardWidget()
        content.setObjectName("historyRecordsCard")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        self.table = TableWidget()
        self.table.setColumnCount(6)
        self.table.setObjectName("historyTable")
        self.table.setHorizontalHeaderLabels((
            "机器 ▾", "时间 ▾", "OCR 结果摘要", "最终频率", "状态", "操作"
        ))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        table_header = self.table.horizontalHeader()
        table_header.setSectionsClickable(True)
        table_header.sectionClicked.connect(self.handle_header_clicked)
        self.update_machine_filter_header()
        self.update_time_filter_header()
        table_header.setFixedHeight(40)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 140), (1, 170), (3, 110), (4, 100), (5, 80)):
            self.table.setColumnWidth(column, width)
        content_layout.addWidget(self.table)

        # 在表格下方显示筛选后的总数和翻页入口。
        pagination_layout = QHBoxLayout()
        self.record_count_label = CaptionLabel("0 条")
        pagination_layout.addWidget(self.record_count_label)
        pagination_layout.addStretch()
        self.previous_page_button = TransparentPushButton("‹")
        self.previous_page_button.setFixedWidth(34)
        self.previous_page_button.setAccessibleName("上一页")
        self.previous_page_button.setEnabled(False)
        self.previous_page_button.clicked.connect(self.show_previous_page)
        pagination_layout.addWidget(self.previous_page_button)
        self.page_label = CaptionLabel("1 / 1")
        pagination_layout.addWidget(self.page_label)
        self.next_page_button = TransparentPushButton("›")
        self.next_page_button.setFixedWidth(34)
        self.next_page_button.setAccessibleName("下一页")
        self.next_page_button.setEnabled(False)
        self.next_page_button.clicked.connect(self.show_next_page)
        pagination_layout.addWidget(self.next_page_button)
        content_layout.addLayout(pagination_layout)
        layout.addWidget(content, 1)

        # 保存当前详情记录的有效证据目录。
        self.current_evidence_directory: Path | None = None

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
        # 创建遮罩弹窗的主体。
        dialog_parent = self.parentWidget() or self
        self.detail_dialog = MaskDialogBase(dialog_parent)
        self.detail_dialog.setObjectName("historyDetail")
        self.detail_dialog.widget.setObjectName("historyDetailContent")
        self.detail_dialog.widget.setFixedSize(
            min(800, dialog_parent.width() - 140),
            min(740, dialog_parent.height() - 100),
        )
        detail_layout = QVBoxLayout(self.detail_dialog.widget)
        detail_layout.setContentsMargins(24, 24, 24, 24)
        detail_layout.setSpacing(18)
        self.detail_values: dict[str, QLabel] = {}
        selectable_text = Qt.TextInteractionFlag.TextSelectableByMouse

        # 在标题右侧显示当前记录状态。
        detail_heading = QHBoxLayout()
        detail_heading.addWidget(SubtitleLabel("记录详情"))
        detail_heading.addStretch()
        status_badge = QLabel("--")
        status_badge.setObjectName("historyDetailStatusBadge")
        detail_heading.addWidget(status_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.detail_values["status"] = status_badge
        detail_layout.addLayout(detail_heading)

        # 创建统一滚动的详情正文。
        self.detail_scroll = ScrollArea()
        self.detail_scroll.setObjectName("historyDetailScroll")
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_body = QWidget()
        detail_body.setObjectName("historyDetailBody")
        body_layout = QVBoxLayout(detail_body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(16)

        # 创建顶部测量摘要卡。
        self.summary_card = SimpleCardWidget()
        self.summary_card.setObjectName("historyDetailSummaryCard")
        self.summary_card.setBorderRadius(12)
        summary_layout = QHBoxLayout(self.summary_card)
        summary_layout.setContentsMargins(18, 16, 18, 16)
        summary_layout.setSpacing(24)

        # 在摘要卡左侧显示机器名称和完成时间。
        machine_layout = QVBoxLayout()
        machine_layout.setSpacing(4)
        machine_name = SubtitleLabel("--")
        machine_name.setObjectName("historyDetailMachineName")
        machine_name.setWordWrap(True)
        machine_name.setTextInteractionFlags(selectable_text)
        self.detail_values["machine"] = machine_name
        machine_layout.addWidget(machine_name)
        self.detail_completed_at_label = CaptionLabel("--")
        self.detail_completed_at_label.setObjectName("historyDetailFinishedAt")
        machine_layout.addWidget(self.detail_completed_at_label)
        summary_layout.addLayout(machine_layout, 1)

        # 在摘要卡右侧显示最终频率。
        frequency_layout = QVBoxLayout()
        frequency_layout.setSpacing(4)
        frequency_caption = CaptionLabel("最终频率")
        frequency_caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        frequency_layout.addWidget(frequency_caption)
        frequency_value = QLabel("--")
        frequency_value.setObjectName("historyDetailFrequencyValue")
        frequency_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        frequency_value.setTextInteractionFlags(selectable_text)
        frequency_layout.addWidget(frequency_value)
        self.detail_values["frequency"] = frequency_value
        summary_layout.addLayout(frequency_layout)
        body_layout.addWidget(self.summary_card)

        # 创建识别结果的并排卡片区域。
        result_layout = QHBoxLayout()
        result_layout.setSpacing(12)

        # 在左侧卡片展示原始 OCR 文字。
        self.original_result_card = SimpleCardWidget()
        self.original_result_card.setObjectName("historyOriginalResultCard")
        self.original_result_card.setBorderRadius(12)
        original_layout = QVBoxLayout(self.original_result_card)
        original_layout.setContentsMargins(18, 16, 18, 16)
        original_layout.setSpacing(12)
        original_layout.addWidget(BodyLabel("原始 OCR"))
        self.detail_ocr_text = QLabel("--")
        self.detail_ocr_text.setObjectName("historyDetailOcrText")
        self.detail_ocr_text.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_ocr_text.setWordWrap(True)
        self.detail_ocr_text.setTextInteractionFlags(selectable_text)
        self.detail_ocr_text.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        original_layout.addWidget(self.detail_ocr_text, 1)
        result_layout.addWidget(self.original_result_card, 1)

        # 在右侧卡片标题旁显示确认标记。
        self.final_result_card = SimpleCardWidget()
        self.final_result_card.setObjectName("historyFinalResultCard")
        self.final_result_card.setBorderRadius(12)
        final_layout = QVBoxLayout(self.final_result_card)
        final_layout.setContentsMargins(18, 16, 18, 16)
        final_layout.setSpacing(12)
        final_heading = QHBoxLayout()
        final_heading.addWidget(BodyLabel("人工最终结果"))
        final_heading.addStretch()
        final_badge = CaptionLabel("✓ 已确认")
        final_badge.setObjectName("historyFinalResultBadge")
        final_heading.addWidget(final_badge)
        final_layout.addLayout(final_heading)

        # 在右侧卡片展示人工最终结果。
        self.final_result_text = QLabel("--")
        self.final_result_text.setObjectName("historyFinalResultText")
        self.final_result_text.setTextFormat(Qt.TextFormat.PlainText)
        self.final_result_text.setWordWrap(True)
        self.final_result_text.setTextInteractionFlags(selectable_text)
        self.final_result_text.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        final_layout.addWidget(self.final_result_text, 1)
        result_layout.addWidget(self.final_result_card, 1)

        # 将只读结果的宽度限制在卡片内。
        for result_text in (self.detail_ocr_text, self.final_result_text):
            result_size_policy = result_text.sizePolicy()
            result_size_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            result_text.setSizePolicy(result_size_policy)
        body_layout.addLayout(result_layout)

        # 在浅暖色提示卡中展示复核原因。
        self.review_reason_card = SimpleCardWidget()
        self.review_reason_card.setObjectName("historyReviewReasonCard")
        self.review_reason_card.setBorderRadius(12)
        reason_layout = QVBoxLayout(self.review_reason_card)
        reason_layout.setContentsMargins(18, 16, 18, 16)
        reason_layout.setSpacing(10)
        self.review_reason_title = BodyLabel("复核原因")
        self.review_reason_title.setObjectName("historyReviewReasonTitle")
        reason_layout.addWidget(self.review_reason_title)
        self.review_reason_value = QLabel()
        self.review_reason_value.setObjectName("historyReviewReasonValue")
        self.review_reason_value.setWordWrap(True)
        self.review_reason_value.setTextInteractionFlags(selectable_text)
        reason_layout.addWidget(self.review_reason_value)
        body_layout.addWidget(self.review_reason_card)

        # 创建待复核记录的人工编辑区。
        self.review_editor_section = QWidget()
        self.review_editor_section.setObjectName("historyReviewEditorSection")
        editor_layout = QVBoxLayout(self.review_editor_section)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(12)
        self.review_editor_title = SubtitleLabel("人工复核")
        editor_layout.addWidget(self.review_editor_title)
        editor_layout.addWidget(CaptionLabel("可修正 OCR 结果，每行一条"))
        self.review_editor = PlainTextEdit(self.review_editor_section)
        self.review_editor.setFixedHeight(125)
        editor_layout.addWidget(self.review_editor)

        # 在编辑区右下方保留两种复核操作。
        review_actions = QHBoxLayout()
        review_actions.addStretch()
        self.confirm_review_button = PushButton("确认无误")
        self.confirm_review_button.clicked.connect(
            lambda: self.complete_record_review(False)
        )
        self.save_review_button = PrimaryPushButton("保存并完成复核")
        self.save_review_button.clicked.connect(
            lambda: self.complete_record_review(True)
        )
        review_actions.addWidget(self.confirm_review_button)
        review_actions.addWidget(self.save_review_button)
        editor_layout.addLayout(review_actions)
        body_layout.addWidget(self.review_editor_section)

        # 创建证据图片的四列网格。
        self.evidence_content = QWidget()
        self.evidence_content.setObjectName("historyEvidenceContent")
        self.evidence_grid = QGridLayout(self.evidence_content)
        self.evidence_grid.setContentsMargins(0, 0, 0, 0)
        self.evidence_grid.setHorizontalSpacing(12)
        self.evidence_grid.setVerticalSpacing(12)
        self.evidence_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        body_layout.addWidget(self.evidence_content)

        # 在证据图片下方创建操作行。
        self.evidence_actions = QWidget()
        evidence_actions_layout = QHBoxLayout(self.evidence_actions)
        evidence_actions_layout.setContentsMargins(0, 0, 0, 0)
        evidence_actions_layout.setSpacing(12)
        self.evidence_actions.hide()

        # 在操作行左侧放置更多图片提示。
        self.evidence_more_label = CaptionLabel("仅展示前 4 张，更多图片请打开文件夹查看")
        self.evidence_more_label.hide()
        evidence_actions_layout.addWidget(self.evidence_more_label)
        evidence_actions_layout.addStretch()

        # 在操作行右侧连接当前证据目录的打开入口。
        self.open_evidence_directory_button = TransparentPushButton("打开证据文件夹")
        self.open_evidence_directory_button.clicked.connect(self.open_evidence_directory)
        evidence_actions_layout.addWidget(self.open_evidence_directory_button)
        body_layout.addWidget(self.evidence_actions)

        # 在详情底部创建记录信息卡。
        self.record_meta_card = SimpleCardWidget()
        self.record_meta_card.setObjectName("historyRecordMetaCard")
        self.record_meta_card.setBorderRadius(12)
        record_meta_layout = QVBoxLayout(self.record_meta_card)
        record_meta_layout.setContentsMargins(18, 16, 18, 16)
        record_meta_layout.setSpacing(12)
        record_fields = QGridLayout()
        record_fields.setContentsMargins(0, 0, 0, 0)
        record_fields.setHorizontalSpacing(12)
        record_fields.setVerticalSpacing(12)
        record_fields.setColumnStretch(1, 1)
        record_fields.setColumnStretch(3, 1)

        # 将记录字段排成两列，证据目录横跨整行。
        for field_name, caption, row, column, span in (
            ("session_id", "Session ID", 0, 0, 1),
            ("start_time", "开始时间", 1, 0, 1),
            ("finish_time", "结束时间", 1, 2, 1),
            ("evidence_directory", "证据目录", 2, 0, 3),
        ):
            field_label = QLabel(caption)
            field_label.setObjectName("historyRecordMetaKey")
            value_label = QLabel("--")
            value_label.setObjectName("historyRecordMetaValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(selectable_text)
            if field_name == "evidence_directory":
                value_label.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
                )
            record_fields.addWidget(field_label, row, column)
            record_fields.addWidget(value_label, row, column + 1, 1, span)
            self.detail_values[field_name] = value_label

        # 在首行右侧显示已复核记录的复核时间。
        self.reviewed_at_title = QLabel("复核时间")
        self.reviewed_at_title.setObjectName("historyRecordMetaKey")
        self.reviewed_at_value = QLabel()
        self.reviewed_at_value.setObjectName("historyRecordMetaValue")
        self.reviewed_at_value.setWordWrap(True)
        self.reviewed_at_value.setTextInteractionFlags(selectable_text)
        record_fields.addWidget(self.reviewed_at_title, 0, 2)
        record_fields.addWidget(self.reviewed_at_value, 0, 3)
        record_meta_layout.addLayout(record_fields)
        body_layout.addWidget(self.record_meta_card)
        body_layout.addStretch()
        self.detail_scroll.setWidget(detail_body)
        detail_layout.addWidget(self.detail_scroll, 1)

        # 在弹窗底部放置关闭入口。
        actions = QHBoxLayout()
        actions.addStretch()
        close_button = PushButton("关闭")
        close_button.setProperty("buttonRole", "secondary")
        close_button.clicked.connect(self.detail_dialog.close)
        actions.addWidget(close_button)
        detail_layout.addLayout(actions)
        self.detail_dialog.hide()

    def refresh_history(self) -> None:
        """进入历史页时恢复不限时间并更新机器和记录。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器选项与历史列表已刷新，读取失败时显示提示
        """
        # 恢复不限时间的筛选状态。
        self.selected_start_date = None
        self.selected_end_date = None
        self.update_time_filter_header()

        # 读取有历史记录的机器。
        result = self.controller.list_record_machines()
        if not result.success:
            # 清空记录和分页显示。
            self.table.setRowCount(0)
            self.current_page = 1
            self.record_count_label.setText("0 条")
            self.page_label.setText("1 / 1")
            self.previous_page_button.setEnabled(False)
            self.next_page_button.setEnabled(False)

            # 显示机器选项读取错误。
            InfoBar.error("历史记录读取失败", result.message, duration=-1, parent=self)
            return
        self.record_machines = result.data["machines"]

        # 保留仍然存在的机器筛选，清除已失效的机器条件。
        machine_ids = {machine["machine_id"] for machine in self.record_machines}
        if self.selected_machine_id not in machine_ids:
            self.selected_machine_id = None
        self.update_machine_filter_header()

        # 读取当前状态和机器条件下的记录。
        self.current_page = 1
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
        self.current_page = 1
        self.reload_records()

    def apply_machine_filter(self, machine_id: str | None) -> None:
        """应用机器筛选或恢复全部机器并读取第一页记录。

        Args:
            machine_id: 所选机器编号，None 表示全部机器。

        Returns:
            返回示例：
                None  # 机器表头和第一页记录已按所选机器刷新
        """
        # 保存已应用的机器条件并回到第一页。
        self.selected_machine_id = machine_id
        self.current_page = 1

        # 刷新机器表头并读取记录。
        self.update_machine_filter_header()
        self.reload_records()

    def handle_header_clicked(self, column: int) -> None:
        """点击机器或时间表头时打开对应筛选弹层。

        Args:
            column: 点击的表格列索引。

        Returns:
            返回示例：
                None  # 机器或时间列打开弹层，其他列保持不变
        """
        if column == MACHINE_COLUMN:
            self.show_machine_filter_flyout()
        elif column == TIME_COLUMN:
            self.show_time_filter_flyout()

    def update_machine_filter_header(self) -> None:
        """刷新机器表头的所选机器提示和文字颜色。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器表头已显示当前应用条件，表头文字保持不变
        """
        # 全部机器使用默认提示和文字颜色。
        header_item = self.table.horizontalHeaderItem(MACHINE_COLUMN)
        if self.selected_machine_id is None:
            header_item.setToolTip("点击筛选机器")
            header_item.setData(Qt.ItemDataRole.ForegroundRole, None)
        else:
            # 读取所选机器名称。
            machine_name = next(
                machine["machine_name"]
                for machine in self.record_machines
                if machine["machine_id"] == self.selected_machine_id
            )

            # 更新已应用机器条件的表头提示。
            header_item.setToolTip(machine_name)
            header_item.setForeground(QColor(COLORS["blue"]))

    def show_machine_filter_flyout(self) -> None:
        """建立临时机器选项并在机器表头下方显示筛选弹层。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 弹层已打开，确定或重置后应用条件并关闭
        """
        # 创建机器筛选弹层正文。
        view = FlyoutView(title="机器筛选", content="", isClosable=False)
        content = QWidget()
        content.setObjectName("historyMachineFilterFlyout")
        content.setFixedWidth(320)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 10, 16, 12)
        content_layout.setSpacing(12)

        # 用当前机器列表填充临时下拉框。
        machine_combo_box = ComboBox(content)
        machine_combo_box.addItem("全部机器", userData=None)
        for machine in self.record_machines:
            machine_id = machine["machine_id"]
            machine_name = machine["machine_name"]
            caption = (
                f"{machine_name}（{machine_id}#）"
                if machine_name != machine_id else f"{machine_id}#"
            )
            machine_combo_box.addItem(caption, userData=machine_id)

        # 恢复已应用的机器选项。
        selected_index = machine_combo_box.findData(self.selected_machine_id)
        machine_combo_box.setCurrentIndex(selected_index)
        content_layout.addWidget(machine_combo_box)

        # 将重置和确定按钮放在右下方。
        actions = QHBoxLayout()
        actions.addStretch()
        reset_button = TransparentPushButton("重置")
        apply_button = PrimaryPushButton("确定")
        actions.addWidget(reset_button)
        actions.addWidget(apply_button)
        content_layout.addLayout(actions)
        view.addWidget(content)

        # 根据机器列的实际位置展开弹层。
        header = self.table.horizontalHeader()
        column_position = header.sectionViewportPosition(MACHINE_COLUMN)
        target_position = header.viewport().mapToGlobal(
            QPoint(column_position, header.height())
        )
        flyout = Flyout.make(
            view,
            target=target_position,
            parent=self.window(),
            aniType=FlyoutAnimationType.DROP_DOWN,
        )

        # 点击确定或重置后应用机器条件并关闭弹层。
        apply_button.clicked.connect(
            lambda: self.apply_machine_filter(machine_combo_box.currentData())
        )
        reset_button.clicked.connect(lambda: self.apply_machine_filter(None))
        apply_button.clicked.connect(flyout.close)
        reset_button.clicked.connect(flyout.close)

    def update_time_filter_header(self) -> None:
        """刷新时间表头的日期范围提示和文字颜色。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 时间表头已显示当前应用条件
        """
        header_item = self.table.horizontalHeaderItem(TIME_COLUMN)
        if self.selected_start_date is None:
            header_item.setToolTip("点击筛选时间范围")
            header_item.setData(Qt.ItemDataRole.ForegroundRole, None)
        else:
            header_item.setToolTip(
                f"{self.selected_start_date:%Y-%m-%d} ～ "
                f"{self.selected_end_date:%Y-%m-%d}"
            )
            header_item.setForeground(QColor(COLORS["blue"]))

    def apply_time_filter(self, start_date: date | None, end_date: date | None) -> None:
        """应用日期范围或清除时间条件并读取第一页。

        Args:
            start_date: 本地开始日期，None 表示不限时间。
            end_date: 本地结束日期，与开始日期同时设置或清除。

        Returns:
            返回示例：
                None  # 已应用日期条件，表头和第一页记录已刷新
        """
        # 保存已应用的日期条件。
        self.selected_start_date = start_date
        self.selected_end_date = end_date
        self.current_page = 1

        # 刷新筛选提示并读取记录。
        self.update_time_filter_header()
        self.reload_records()

    def show_time_filter_flyout(self) -> None:
        """建立临时日期输入并在时间表头下方显示筛选弹层。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 弹层已打开，确定或重置后应用条件并关闭
        """
        # 创建弹层正文和不限时间选项。
        view = FlyoutView(title="时间筛选", content="", isClosable=False)
        content = QWidget()
        content.setObjectName("historyTimeFilterFlyout")
        content.setFixedWidth(320)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 10, 16, 12)
        content_layout.setSpacing(12)
        unlimited_checkbox = CheckBox("不限时间")
        unlimited_checkbox.setChecked(self.selected_start_date is None)
        content_layout.addWidget(unlimited_checkbox)

        # 恢复已应用的日期或显示今天。
        current_date = QDate.currentDate()
        start_date_edit = CalendarPicker(content)
        end_date_edit = CalendarPicker(content)
        for date_edit, selected_date in (
            (start_date_edit, self.selected_start_date),
            (end_date_edit, self.selected_end_date),
        ):
            date_edit.setDate(QDate(selected_date) if selected_date else current_date)
            date_edit.setDateFormat("yyyy-MM-dd")
            date_edit.setResetEnabled(False)
            date_edit.setEnabled(not unlimited_checkbox.isChecked())

        # 横向排列两个日期输入框。
        date_layout = QHBoxLayout()
        date_layout.setSpacing(8)
        date_layout.addWidget(start_date_edit)
        date_layout.addWidget(CaptionLabel("—"))
        date_layout.addWidget(end_date_edit)
        content_layout.addLayout(date_layout)

        # 将重置和确定按钮放在右下方。
        actions = QHBoxLayout()
        actions.addStretch()
        reset_button = TransparentPushButton("重置")
        apply_button = PrimaryPushButton("确定")
        actions.addWidget(reset_button)
        actions.addWidget(apply_button)
        content_layout.addLayout(actions)
        view.addWidget(content)

        def synchronize_date_range(changed_date: QDate, changed_start: bool) -> None:
            """在临时输入框中同步交叉的日期范围。

            Args:
                changed_date: 本次选择的日期。
                changed_start: 是否修改了开始日期。

            Returns:
                返回示例：
                    None  # 交叉时另一日期已同步，已应用条件保持不变
            """
            if start_date_edit.getDate() > end_date_edit.getDate():
                target_edit = end_date_edit if changed_start else start_date_edit
                target_edit.setDate(changed_date)

        # 仅更新弹层内的可用状态和日期顺序。
        unlimited_checkbox.toggled.connect(
            lambda checked: start_date_edit.setEnabled(not checked)
        )
        unlimited_checkbox.toggled.connect(
            lambda checked: end_date_edit.setEnabled(not checked)
        )
        start_date_edit.dateChanged.connect(
            lambda selected_date: synchronize_date_range(selected_date, True)
        )
        end_date_edit.dateChanged.connect(
            lambda selected_date: synchronize_date_range(selected_date, False)
        )

        # 根据时间列的实际位置展开弹层。
        header = self.table.horizontalHeader()
        column_position = header.sectionViewportPosition(TIME_COLUMN)
        target_position = header.viewport().mapToGlobal(
            QPoint(column_position, header.height())
        )
        flyout = Flyout.make(
            view,
            target=target_position,
            parent=self.window(),
            aniType=FlyoutAnimationType.DROP_DOWN,
        )

        # 点击确定或重置后应用条件并关闭弹层。
        apply_button.clicked.connect(
            lambda: self.apply_time_filter(
                None
                if unlimited_checkbox.isChecked()
                else start_date_edit.getDate().toPython(),
                None
                if unlimited_checkbox.isChecked()
                else end_date_edit.getDate().toPython(),
            )
        )
        reset_button.clicked.connect(lambda: self.apply_time_filter(None, None))
        apply_button.clicked.connect(flyout.close)
        reset_button.clicked.connect(flyout.close)

    def show_previous_page(self) -> None:
        """在上一页可用时读取上一页记录。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 上一页记录已显示，或页码保持不变
        """
        if self.previous_page_button.isEnabled():
            self.current_page -= 1
            self.reload_records()

    def show_next_page(self) -> None:
        """在下一页可用时读取下一页记录。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 下一页记录已显示，或页码保持不变
        """
        if self.next_page_button.isEnabled():
            self.current_page += 1
            self.reload_records()

    def reload_records(self) -> None:
        """按状态、机器和日期条件填充历史记录表格。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 表格显示符合筛选条件的测量记录
        """
        # 整理当前选择的本地日期范围。
        start_date = self.selected_start_date
        end_date = self.selected_end_date

        # 按当前筛选条件读取测量结果。
        result = self.controller.list_measurement_records(
            self.selected_review_status,
            self.selected_machine_id,
            self.current_page,
            self.page_size,
            start_date=start_date,
            end_date=end_date,
        )
        if not result.success:
            # 清空记录和分页显示。
            self.table.setRowCount(0)
            self.current_page = 1
            self.record_count_label.setText("0 条")
            self.page_label.setText("1 / 1")
            self.previous_page_button.setEnabled(False)
            self.next_page_button.setEnabled(False)

            # 显示测量记录读取错误。
            InfoBar.error("历史记录读取失败", result.message, duration=-1, parent=self)
            return
        records = result.data["records"]
        total = result.data["total"]
        total_pages = result.data["total_pages"]

        # 当前页消失时读取最后一个有效页。
        if self.current_page > total_pages:
            self.current_page = total_pages
            self.reload_records()
            return

        # 更新页码、总数和翻页按钮。
        self.record_count_label.setText(f"{total} 条")
        self.page_label.setText(f"{self.current_page} / {total_pages}")
        self.previous_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)

        # 将每条记录填入六列表格。
        self.table.setRowCount(len(records))
        highlighted_font = QFont(self.table.font())
        highlighted_font.setWeight(QFont.Weight.Medium)
        for row_index, record in enumerate(records):
            final_lines = record["reviewed_lines"]
            if final_lines is None:
                final_lines = record["ordered_lines"]
            summary_lines = (line.replace("\n", " ") for line in final_lines[:2])
            summary = "；".join(summary_lines) or "--"
            if len(final_lines) > 2 or len(summary) > 60:
                summary = summary[:59] + "…"
            values = (
                record["machine_name"],
                format_history_time(record["finish_time"]),
                summary,
                format_history_frequency(record["final_frequency_hz"]),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 2):
                    item.setFont(highlighted_font)
                elif column == 1:
                    item.setForeground(QColor("#667085"))
                elif column == 3:
                    item.setForeground(QColor("#344054"))
                self.table.setItem(row_index, column, item)

            # 在状态列居中放置对应颜色的徽标。
            if not record["needs_review"]:
                status_text, status_tone = "正常", "normal"
            elif record["reviewed_at"] is None:
                status_text, status_tone = "待复核", "pending"
            else:
                status_text, status_tone = "已复核", "reviewed"
            status_wrapper = QWidget()
            status_wrapper.setObjectName("historyStatusWrapper")
            status_layout = QHBoxLayout(status_wrapper)
            status_layout.setContentsMargins(0, 0, 0, 0)
            status_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status_badge = QLabel(status_text)
            status_badge.setObjectName("historyStatusBadge")
            status_badge.setProperty("tone", status_tone)
            status_layout.addWidget(status_badge)
            self.table.setCellWidget(row_index, 4, status_wrapper)

            # 为当前记录建立详情入口。
            button = TransparentPushButton("查看  ›")
            button.setObjectName("historyViewButton")
            session_id = record["session_id"]
            button.clicked.connect(
                lambda checked=False, cycle=session_id: self.show_record_detail(cycle)
            )
            self.table.setCellWidget(row_index, 5, button)

        # 在页面完成显示后将状态徽标和查看按钮对齐到单元格。
        QTimer.singleShot(0, self.table.doItemsLayout)

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
            InfoBar.error("历史详情读取失败", result.message, duration=-1, parent=self)
            return
        record = result.data["record"]
        if record is None:
            InfoBar.error("历史详情读取失败", "该测量记录已不存在。", duration=-1, parent=self)
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
        self.detail_completed_at_label.setText(finish_time)
        if completed_review:
            status_text, status_tone = "已复核", "reviewed"
        elif pending_review:
            status_text, status_tone = "待复核", "pending"
        else:
            status_text, status_tone = "正常", "normal"
        status_badge = self.detail_values["status"]
        status_badge.setText(status_text)
        status_badge.setProperty("tone", status_tone)
        status_badge.style().unpolish(status_badge)
        status_badge.style().polish(status_badge)
        self.detail_values["frequency"].setText(
            format_history_frequency(record["final_frequency_hz"])
        )
        self.detail_values["evidence_directory"].setText(record["evidence_directory"])

        # 显示原始 OCR 文字和需要复核的原因。
        self.detail_ocr_text.setText("\n".join(record["ordered_lines"]) or "--")
        self.review_reason_card.setVisible(needs_review)
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
        self.final_result_card.setVisible(completed_review)
        self.final_result_text.setText("\n".join(final_lines) or "--")

        # 为待复核记录预填原始文字并显示操作入口。
        self.review_editor_section.setVisible(pending_review)
        self.review_editor.setPlainText("\n".join(record["ordered_lines"]))

        # 读取本轮证据目录并显示可用图片。
        self.populate_evidence_images(record["evidence_directory"])

        # 根据当前窗口尺寸调整弹窗。
        dialog_parent = self.detail_dialog.parentWidget()
        self.detail_dialog.widget.setFixedSize(
            min(800, dialog_parent.width() - 140),
            min(740, dialog_parent.height() - 100),
        )

        # 将详情正文恢复到顶部。
        self.detail_scroll.verticalScrollBar().setValue(0)

        # 启用复核操作。
        self.confirm_review_button.setEnabled(True)
        self.save_review_button.setEnabled(True)

        # 打开详情弹窗。
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
            InfoBar.error(
                "人工复核未完成",
                result.message,
                duration=-1,
                parent=self.detail_dialog.widget,
            )
            if result.data is True:
                self.show_record_detail(session_id)
            return

        # 关闭详情并刷新当前状态及机器筛选下的列表。
        self.confirm_review_button.setEnabled(False)
        self.save_review_button.setEnabled(False)
        self.detail_dialog.close()
        self.reload_records()

    def populate_evidence_images(self, evidence_directory: str) -> None:
        """读取本轮 JPG 证据并更新详情预览和文件夹入口。

        Args:
            evidence_directory: 测量记录保存的本轮证据目录。

        Returns:
            返回示例：
                None  # 证据预览、更多图片提示和文件夹入口已更新
        """
        # 清除上一条记录的缩略图和占位文字。
        while self.evidence_grid.count():
            layout_item = self.evidence_grid.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        # 保存当前记录的有效证据目录。
        directory = Path(evidence_directory) if evidence_directory else None
        if directory is not None and directory.is_dir():
            self.current_evidence_directory = directory
        else:
            self.current_evidence_directory = None

        # 根据证据目录更新文件夹入口。
        self.evidence_actions.setVisible(self.current_evidence_directory is not None)
        self.open_evidence_directory_button.setEnabled(
            self.current_evidence_directory is not None
        )

        # 清除上一条记录的更多图片提示。
        self.evidence_more_label.hide()

        # 读取记录目录当前层按文件名排序的 JPG 文件。
        image_paths = (
            sorted(directory.glob("*.jpg"))
            if self.current_evidence_directory is not None
            else []
        )

        # 跳过损坏的 JPG 图片。
        readable_image_count = 0
        has_more_images = False
        for image_path in image_paths:
            image = QPixmap(str(image_path))
            if image.isNull():
                continue

            # 检测到第五张可读取图片后停止读取。
            if readable_image_count >= 4:
                has_more_images = True
                break

            # 为可读取的图片创建可点击缩略图。
            thumbnail = PushButton()
            thumbnail.setObjectName("evidenceThumbnail")
            thumbnail.setToolTip(image_path.name)
            thumbnail.setAccessibleName(image_path.name)

            # 按现有尺寸缩放缩略图。
            thumbnail_image = image.scaled(
                QSize(140, 100),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

            # 设置缩略图图标和按钮大小。
            thumbnail.setIcon(QIcon(thumbnail_image))
            thumbnail.setIconSize(QSize(140, 100))
            thumbnail.setFixedSize(160, 120)

            # 将缩略图点击连接到现有大图查看入口。
            thumbnail.clicked.connect(
                lambda checked=False, path=image_path: self.show_evidence_image(path)
            )

            # 将缩略图加入证据预览的第一行。
            self.evidence_grid.addWidget(thumbnail, 0, readable_image_count)
            readable_image_count += 1

        # 在可读取图片超过四张时显示更多图片提示。
        self.evidence_more_label.setVisible(has_more_images)

        # 在没有可显示图片时给出对应提示。
        if readable_image_count == 0:
            message = "证据图片读取失败" if image_paths else "暂无证据图片"
            self.evidence_grid.addWidget(QLabel(message), 0, 0)

    def open_evidence_directory(self) -> None:
        """使用系统文件管理器打开当前记录的证据目录。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 已请求打开当前证据目录，无有效目录时不执行操作
        """
        # 打开当前记录保存的有效证据目录。
        directory = self.current_evidence_directory
        if directory is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def show_evidence_image(self, image_path: Path) -> None:
        """打开一张证据图片的大图查看窗口。

        Args:
            image_path: 已选证据图片的完整路径。

        Returns:
            返回示例：
                None  # 大图窗口已关闭
        """
        # 创建适配当前屏幕大小的遮罩预览。
        dialog_parent = self.detail_dialog.parentWidget() or self
        image_dialog = MaskDialogBase(dialog_parent)
        image_dialog.widget.setObjectName("historyEvidencePreviewContent")
        image_dialog.setWindowTitle(image_path.name)
        screen_size = image_dialog.screen().availableGeometry().size()
        image_dialog.widget.setFixedSize(
            min(900, screen_size.width() - 80, dialog_parent.width() - 80),
            min(650, screen_size.height() - 80, dialog_parent.height() - 80),
        )
        image_layout = QVBoxLayout(image_dialog.widget)
        image_layout.setContentsMargins(20, 16, 20, 16)
        image_layout.setSpacing(8)

        # 在图片上方显示文件名和关闭按钮。
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel(image_path.name))
        header.addStretch()
        close_button = PushButton("关闭")
        close_button.clicked.connect(image_dialog.accept)
        header.addWidget(close_button)
        image_layout.addLayout(header)
        image_label = QLabel()
        image_label.setObjectName("historyEvidenceImage")
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 将图片等比例缩放到查看窗口的可用区域。
        image = QPixmap(str(image_path))
        if image.isNull():
            image_label.setText("图片读取失败")
        else:
            available_size = QSize(
                image_dialog.widget.width() - 40,
                image_dialog.widget.height() - 90,
            )
            image_label.setPixmap(
                image.scaled(
                    available_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        image_layout.addWidget(image_label, 1)

        # 显示并释放大图窗口。
        image_dialog.exec()
        image_dialog.deleteLater()
