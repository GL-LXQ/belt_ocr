"""按测量分组的图片管理 UI，真实数据读取留待下一阶段接入。"""

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QRect, QSize, Qt
from PySide6.QtGui import QAction, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    FlyoutAnimationType,
    FlyoutView,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
    TitleLabel,
    TransparentPushButton,
    setCustomStyleSheet,
)
from qfluentwidgets.components.widgets.flyout import FlyoutAnimationManager

from ui.date_range_picker import DateRangePicker
from ui.image_evidence_viewer import EvidenceViewer
from ui.image_management_preview import (
    EVIDENCE_MESSAGES,
    REVIEW_CAPTIONS,
    PreviewMeasurement,
    build_preview_measurements,
    draw_preview_image,
)
from ui.pages.history_page import HistoryTimeFilterPanel
from ui.theme import COLORS


class EvidenceThumbnail(QLabel):
    """完整展示卡片首图或异常占位，不裁切图片。"""

    def __init__(self, record: PreviewMeasurement, parent: QWidget | None = None) -> None:
        """保存当前首图并建立目录或坏图提示。

        Args:
            record: 当前测量的演示展示数据。
            parent: 所属测量卡片。

        Returns:
            None  # 首图或带文件名的异常占位已准备
        """
        super().__init__(parent)
        self.setObjectName("evidenceCardThumbnail")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.source_pixmap = QPixmap()
        image = record.images[0] if record.images else None
        if image and image.readable:
            self.source_pixmap = draw_preview_image(image)
        else:
            state = "corrupt" if image else record.evidence_state
            title = EVIDENCE_MESSAGES[state][0]
            self.setText(f"{title}\n{image.filename}" if image else title)
        self.setToolTip(image.filename if image else EVIDENCE_MESSAGES[record.evidence_state][0])

    def paintEvent(self, event) -> None:
        """在留白区域中等比例绘制整张图片。

        Args:
            event: Qt 绘制事件。

        Returns:
            None  # 图片保持原始长宽比，异常文字沿用 QLabel 绘制
        """
        super().paintEvent(event)
        if self.source_pixmap.isNull():
            return
        available = self.size() - QSize(20, 38)
        target = QRect(0, 0, *self.source_pixmap.size().scaled(available, Qt.AspectRatioMode.KeepAspectRatio).toTuple())
        target.moveCenter(self.rect().adjusted(0, 0, 0, -20).center())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, self.source_pixmap)
        painter.end()


class MeasurementEvidenceCard(QPushButton):
    """以一次测量为单位展示首图、摘要和状态。"""

    def __init__(self, record: PreviewMeasurement, parent: QWidget | None = None) -> None:
        """建立可用鼠标或键盘打开的测量卡片。

        Args:
            record: 卡片对应的内存演示测量。
            parent: 所属记录网格。

        Returns:
            None  # 图片、最多两行文字和测量摘要已建立
        """
        super().__init__(parent)
        self.record = record
        self.setObjectName("imageMeasurementCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(0)
        self.setFixedHeight(304)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"演示测量 {record.session_id}，{record.machine_name}，打开证据查看器")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 14)
        layout.setSpacing(9)

        # 在图片留白区叠放现存数量，未读取目录时不显示零张。
        stage = QWidget()
        stage.setFixedHeight(158)
        stage_layout = QGridLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        self.thumbnail = EvidenceThumbnail(record)
        stage_layout.addWidget(self.thumbnail, 0, 0)
        self.image_count_label = QLabel(record.image_count_caption)
        self.image_count_label.setObjectName("imageCountBadge")
        stage_layout.addWidget(self.image_count_label, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(stage)

        # 机器名称与复核状态共享一行。
        metadata = QHBoxLayout()
        metadata.setSpacing(6)
        machine_label = QLabel(record.machine_name)
        machine_label.setObjectName("imageMachineName")
        machine_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_badge = QLabel(REVIEW_CAPTIONS[record.review_status])
        self.status_badge.setObjectName("imageReviewBadge")
        self.status_badge.setProperty("reviewStatus", record.review_status)
        metadata.addWidget(machine_label, 1)
        metadata.addWidget(self.status_badge)
        layout.addLayout(metadata)

        # 摘要优先显示人工最终文字，完整内容保留在查看器中。
        self.summary_label = QLabel()
        self.summary_label.setObjectName("imageTextSummary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setFixedHeight(40)
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.summary_label.setToolTip("\n".join(record.effective_lines) or "暂无识别文字")
        layout.addWidget(self.summary_label)
        self.update_summary()

        # 时间仅标记测量完成时刻，频率放在次要位置。
        details = QHBoxLayout()
        time_label = QLabel(f"完成 {record.finished_at:%Y-%m-%d %H:%M:%S}")
        time_label.setToolTip(f"测量完成时间：{record.finished_at:%Y-%m-%d %H:%M:%S}")
        frequency = "--" if record.frequency is None else f"{record.frequency:.1f} Hz"
        frequency_label = QLabel(frequency)
        for label in (time_label, frequency_label):
            label.setObjectName("imageMutedText")
        details.addWidget(time_label)
        details.addStretch()
        details.addWidget(frequency_label)
        layout.addLayout(details)

        # 子控件不截获点击，整张卡片保留原生按钮键盘行为。
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def update_summary(self) -> None:
        """按卡片宽度缩略最多两行有效文字。

        Args:
            无。

        Returns:
            None  # 摘要不超过两行，额外内容以省略号提示
        """
        lines = list(self.record.effective_lines[:2]) or ["暂无识别文字"]
        if len(self.record.effective_lines) > 2:
            lines[-1] += " …"
        width = max(1, self.width() - 28)
        metrics = self.summary_label.fontMetrics()
        summary = "\n".join(metrics.elidedText(line, Qt.TextElideMode.ElideRight, width) for line in lines)
        self.summary_label.setText(summary)

    def resizeEvent(self, event) -> None:
        """在卡片宽度改变后重新缩略摘要。

        Args:
            event: Qt 尺寸变化事件。

        Returns:
            None  # 摘要文字保持在卡片边界内
        """
        super().resizeEvent(event)
        self.update_summary()


class ImageManagementPage(QWidget):
    """提供显式演示入口的分组证据浏览页面。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """建立图片管理界面，不查询业务库或读取证据目录。

        Args:
            parent: 所属主窗口。

        Returns:
            None  # 页面默认显示待接入状态，演示仅由明确入口开启
        """
        super().__init__(parent)
        self.setObjectName("images")
        self.preview_enabled = False
        self.preview_records: tuple[PreviewMeasurement, ...] = ()
        self.cards: list[MeasurementEvidenceCard] = []
        self.current_page = 1
        self.page_size = 12
        self.total_pages = 1
        self.grid_columns = 0
        self.time_filter_panel: HistoryTimeFilterPanel | None = None

        # 保存已提交条件，输入中的文字不影响其他筛选操作。
        self.selected_text_query = ""
        self.selected_match_mode = "contains"
        self.selected_text_length: int | None = None
        self.selected_machine_id: str | None = None
        self.selected_review_status: str | None = None
        self.selected_start_date: date | None = None
        self.selected_end_date: date | None = None

        # 按标题、预览提示、筛选和分页结果建立主流程。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 4, 24, 20)
        layout.setSpacing(16)
        self.build_heading(layout)
        self.build_filters(layout)
        self.build_results(layout)

        # 应用独立页面样式，查看器沿用同一套视觉。
        stylesheet_path = Path(__file__).parents[1] / "styles" / "image_management_page.qss"
        self.image_stylesheet = stylesheet_path.read_text(encoding="utf-8")
        for name, color in COLORS.items():
            self.image_stylesheet = self.image_stylesheet.replace(f"@{name}", color)
        self.setStyleSheet(self.image_stylesheet)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (PushButton, LineEdit, SegmentedWidget, SimpleCardWidget, QLabel)):
                setCustomStyleSheet(widget, self.image_stylesheet, self.image_stylesheet)
        self.viewer = EvidenceViewer(self.window(), self.image_stylesheet)
        self.set_preview_enabled(False)

    def build_heading(self, layout: QVBoxLayout) -> None:
        """建立页面标题、刷新按钮和显式演示开关。

        Args:
            layout: 页面主布局。

        Returns:
            None  # 标题和演示入口已加入页面
        """
        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(6)
        title = TitleLabel("图片管理")
        title.setObjectName("pageTitle")
        subtitle = BodyLabel("按测量归组查看证据图片，完整结果与复核仍在历史记录中")
        subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        heading.addLayout(titles, 1)
        self.refresh_button = PushButton(FluentIcon.SYNC, "刷新")
        self.refresh_button.setFixedSize(88, 36)
        self.refresh_button.clicked.connect(self.render_records)
        heading.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)

        # 演示说明始终可见，虚构数据不会冒充业务记录。
        preview_bar = QFrame()
        preview_bar.setObjectName("imagePreviewBar")
        preview_layout = QHBoxLayout(preview_bar)
        preview_layout.setContentsMargins(16, 10, 12, 10)
        preview_layout.setSpacing(12)
        self.preview_label = QLabel()
        self.preview_label.setObjectName("imagePreviewNotice")
        self.preview_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_label, 1)
        self.scenario_combo = ComboBox()
        self.scenario_combo.setObjectName("imagePreviewScenario")
        self.scenario_combo.setAccessibleName("切换 UI 演示场景")
        self.scenario_combo.setFixedWidth(188)
        for key, caption in (
            ("all", "演示：分组与分页"),
            ("empty", "演示：暂无测量"),
            ("no_results", "演示：筛选无结果"),
            ("missing_directory", "演示：目录不存在"),
            ("no_jpg", "演示：目录无 JPG"),
            ("access_denied", "演示：读取被拒绝"),
            ("corrupt", "演示：图片损坏"),
        ):
            self.scenario_combo.addItem(caption, userData=key)
        self.scenario_combo.currentIndexChanged.connect(self.change_preview_scenario)
        preview_layout.addWidget(self.scenario_combo)
        self.preview_button = PushButton("打开演示预览")
        self.preview_button.clicked.connect(lambda: self.set_preview_enabled(not self.preview_enabled))
        preview_layout.addWidget(self.preview_button)
        layout.addWidget(preview_bar)

    def build_filters(self, layout: QVBoxLayout) -> None:
        """建立两行文字、机器、完成日期和复核状态筛选。

        Args:
            layout: 页面主布局。

        Returns:
            None  # 查询草稿与已应用筛选的操作入口已建立
        """
        self.filter_card = SimpleCardWidget()
        self.filter_card.setObjectName("imageFilterCard")
        self.filter_card.setBorderRadius(16)
        filter_layout = QVBoxLayout(self.filter_card)
        filter_layout.setContentsMargins(20, 16, 20, 16)
        filter_layout.setSpacing(12)
        query_row = QHBoxLayout()
        query_row.setSpacing(10)
        self.text_query_edit = LineEdit()
        self.text_query_edit.setObjectName("imageTextQuery")
        self.text_query_edit.setPlaceholderText("输入完整皮带文字或片段")
        self.text_query_edit.setAccessibleName("皮带文字")
        self.text_query_edit.setFixedHeight(38)
        self.text_query_edit.setMinimumWidth(400)
        self.text_query_edit.setMaximumWidth(438)
        search_action = QAction(FluentIcon.SEARCH.icon(), "查询皮带文字", self.text_query_edit)
        search_action.triggered.connect(self.apply_text_search)
        self.text_query_edit.addAction(search_action, QLineEdit.ActionPosition.LeadingPosition)
        self.text_query_edit.returnPressed.connect(self.apply_text_search)

        # 位数限制针对完整有效文字行，匹配方式区分包含与精确。
        self.text_length_combo = ComboBox()
        self.text_length_combo.setObjectName("imageTextLength")
        self.text_length_combo.setAccessibleName("完整文字位数")
        self.text_length_combo.setFixedSize(124, 38)
        for length in (None, 20, 8, 3, 2):
            self.text_length_combo.addItem("全部位数" if length is None else f"{length} 位", userData=length)
        self.match_mode_combo = ComboBox()
        self.match_mode_combo.setObjectName("imageMatchMode")
        self.match_mode_combo.setAccessibleName("文字匹配方式")
        self.match_mode_combo.setFixedSize(104, 38)
        self.match_mode_combo.addItem("包含", userData="contains")
        self.match_mode_combo.addItem("精确", userData="exact")
        self.search_button = PrimaryPushButton("查询")
        self.search_button.setObjectName("imageSearchButton")
        self.search_button.setFixedSize(88, 38)
        self.search_button.clicked.connect(self.apply_text_search)
        query_row.addWidget(self.text_query_edit, 1)
        for widget in (self.text_length_combo, self.match_mode_combo, self.search_button):
            query_row.addWidget(widget)
        query_row.addStretch()
        filter_layout.addLayout(query_row)

        # 第二行只保留定位记录需要的次要筛选。
        detail_row = QHBoxLayout()
        detail_row.setSpacing(12)
        self.machine_combo = ComboBox()
        self.machine_combo.setObjectName("imageMachineFilter")
        self.machine_combo.setAccessibleName("机器筛选")
        self.machine_combo.setFixedSize(176, 36)
        self.machine_combo.addItem("全部机器", userData=None)
        self.machine_combo.currentIndexChanged.connect(self.apply_machine_filter)
        self.time_filter_button = PushButton("完成日期  ▾")
        self.time_filter_button.setObjectName("imageTimeFilter")
        self.time_filter_button.setAccessibleName("测量完成日期筛选")
        self.time_filter_button.setFixedSize(250, 36)
        self.time_filter_button.clicked.connect(self.show_time_filter)
        self.status_filter = SegmentedWidget()
        self.status_filter.setObjectName("imageStatusFilter")
        self.status_filter.setFixedSize(336, 36)
        self.status_filter.setIndicatorColor(Qt.GlobalColor.transparent, Qt.GlobalColor.transparent)
        for status, caption in (("all", "全部"), *REVIEW_CAPTIONS.items()):
            self.status_filter.addItem(
                status,
                caption,
                onClick=lambda checked=False, value=status: self.apply_status_filter(None if value == "all" else value),
            )
        self.status_filter.setCurrentItem("all")
        self.reset_button = TransparentPushButton("重置筛选")
        self.reset_button.setObjectName("imageResetFilters")
        self.reset_button.setFixedSize(88, 36)
        self.reset_button.clicked.connect(self.reset_filters)
        for widget in (self.machine_combo, self.time_filter_button, self.status_filter):
            detail_row.addWidget(widget)
        detail_row.addStretch()
        detail_row.addWidget(self.reset_button)
        filter_layout.addLayout(detail_row)
        layout.addWidget(self.filter_card)

    def build_results(self, layout: QVBoxLayout) -> None:
        """建立只保留当前页卡片的网格、空状态和底部分页。

        Args:
            layout: 页面主布局。

        Returns:
            None  # 滚动结果和按测量计数的分页已建立
        """
        self.results_scroll = QScrollArea()
        self.results_scroll.setObjectName("imageResultsScroll")
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_scroll.viewport().installEventFilter(self)
        self.results_body = QWidget()
        self.results_body.setObjectName("imageResultsBody")
        results_layout = QVBoxLayout(self.results_body)
        results_layout.setContentsMargins(0, 0, 4, 0)
        results_layout.setSpacing(0)
        self.grid_body = QWidget()
        self.grid = QGridLayout(self.grid_body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(16)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        results_layout.addWidget(self.grid_body)

        # 空白、无结果和未接入状态使用同一区域呈现。
        self.empty_state = QFrame()
        self.empty_state.setObjectName("imageEmptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(32, 40, 32, 40)
        empty_layout.setSpacing(12)
        empty_layout.addStretch()
        icon_label = QLabel()
        icon_label.setPixmap(FluentIcon.PHOTO.icon().pixmap(36, 36))
        empty_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = QLabel()
        self.empty_title.setObjectName("imageEmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_body = QLabel()
        self.empty_body.setObjectName("imageMutedText")
        self.empty_body.setWordWrap(True)
        self.empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_body)
        self.empty_action = PushButton("打开演示预览")
        self.empty_action.clicked.connect(self.handle_empty_action)
        empty_layout.addWidget(self.empty_action, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch()
        results_layout.addWidget(self.empty_state, 1)
        results_layout.addStretch()
        self.results_scroll.setWidget(self.results_body)
        layout.addWidget(self.results_scroll, 1)

        # 计数单位为测量记录，图片总数不参与翻页。
        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.record_count_label = CaptionLabel()
        self.record_count_label.setObjectName("imageRecordCount")
        footer.addWidget(self.record_count_label)
        footer.addStretch()
        self.previous_page_button = TransparentPushButton("‹")
        self.previous_page_button.setAccessibleName("上一页测量")
        self.previous_page_button.setFixedWidth(34)
        self.previous_page_button.clicked.connect(lambda: self.change_page(self.current_page - 1))
        self.page_label = CaptionLabel("1 / 1")
        self.page_label.setObjectName("imagePageNumber")
        self.next_page_button = TransparentPushButton("›")
        self.next_page_button.setAccessibleName("下一页测量")
        self.next_page_button.setFixedWidth(34)
        self.next_page_button.clicked.connect(lambda: self.change_page(self.current_page + 1))
        footer.addWidget(self.previous_page_button)
        footer.addWidget(self.page_label)
        footer.addWidget(self.next_page_button)
        layout.addLayout(footer)

    def set_preview_enabled(self, enabled: bool) -> None:
        """明确进入或退出与业务数据隔离的演示模式。

        Args:
            enabled: 是否开启内存演示。

        Returns:
            None  # 演示记录、控件可用性和页面说明已同步
        """
        self.preview_enabled = enabled
        self.preview_records = build_preview_measurements() if enabled else ()
        self.viewer.close()
        if self.time_filter_panel is not None:
            self.time_filter_panel.close()

        # 用明确文案区分真实数据待接入和当前演示状态。
        self.preview_label.setText(
            "演示数据 · UI 预览｜虚构测量与绘制占位图，仅存于内存。"
            if enabled else "UI 阶段｜真实图片读取与记录跳转待接入，可先打开演示预览。"
        )
        self.preview_button.setText("退出演示" if enabled else "打开演示预览")
        self.scenario_combo.setVisible(enabled)
        self.filter_card.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.refresh_button.setToolTip("仅刷新当前演示场景，保留已应用筛选" if enabled else "真实数据读取待接入")

        # 机器选项仅来自演示记录，不读取机器表。
        self.machine_combo.blockSignals(True)
        self.machine_combo.clear()
        self.machine_combo.addItem("全部机器", userData=None)
        machines = {record.machine_id: record.machine_name for record in self.preview_records}
        for machine_id, machine_name in machines.items():
            self.machine_combo.addItem(machine_name, userData=machine_id)
        self.machine_combo.blockSignals(False)
        self.reset_filters()

    def reset_filters(self) -> None:
        """清除查询草稿及全部已应用条件并恢复完整演示场景。

        Args:
            无。

        Returns:
            None  # 默认筛选和第一页记录已恢复
        """
        if self.time_filter_panel is not None:
            self.time_filter_panel.close()
        self.text_query_edit.clear()
        self.match_mode_combo.setCurrentIndex(0)
        self.text_length_combo.setCurrentIndex(0)
        self.selected_text_query = ""
        self.selected_match_mode = "contains"
        self.selected_text_length = None
        self.selected_machine_id = None
        self.selected_review_status = None
        self.selected_start_date = None
        self.selected_end_date = None

        # 阻止控件回填期间重复重建卡片。
        self.machine_combo.blockSignals(True)
        self.machine_combo.setCurrentIndex(0)
        self.machine_combo.blockSignals(False)
        self.scenario_combo.blockSignals(True)
        self.scenario_combo.setCurrentIndex(0)
        self.scenario_combo.blockSignals(False)
        self.status_filter.setCurrentItem("all")
        self.update_date_caption()
        self.current_page = 1
        self.render_records()

    def apply_text_search(self) -> None:
        """提交文字草稿并从第一页筛选演示记录。

        Args:
            无。

        Returns:
            None  # 空白被移除且转为大写，其他筛选沿用已应用值
        """
        self.selected_text_query = "".join(self.text_query_edit.text().split()).upper()
        self.selected_match_mode = self.match_mode_combo.currentData()
        self.selected_text_length = self.text_length_combo.currentData()
        self.current_page = 1
        self.render_records()

    def apply_machine_filter(self) -> None:
        """应用当前机器并从第一页展示演示记录。

        Args:
            无。

        Returns:
            None  # 已应用文字草稿不变，机器条件已更新
        """
        self.selected_machine_id = self.machine_combo.currentData()
        self.current_page = 1
        self.render_records()

    def apply_status_filter(self, review_status: str | None) -> None:
        """应用复核状态并从第一页展示演示记录。

        Args:
            review_status: None 表示全部，其他值为已有复核状态。

        Returns:
            None  # 已应用文字条件不变，状态条件已更新
        """
        self.selected_review_status = review_status
        self.current_page = 1
        self.render_records()

    def apply_time_filter(self, start_date: date | None, end_date: date | None) -> None:
        """应用本地测量完成日期范围。

        Args:
            start_date: 开始日期，None 表示清除日期条件。
            end_date: 结束日期，与开始日期同时设置或清除。

        Returns:
            None  # 按完成日期筛选第一页演示测量
        """
        self.selected_start_date = start_date
        self.selected_end_date = end_date
        self.update_date_caption()
        self.current_page = 1
        self.render_records()

    def update_date_caption(self) -> None:
        """显示当前已应用的完成日期条件。

        Args:
            无。

        Returns:
            None  # 按钮显示不限日期、单日或日期范围
        """
        if self.selected_start_date is None:
            caption = "完成日期"
        elif self.selected_start_date == self.selected_end_date:
            caption = f"{self.selected_start_date:%Y-%m-%d}"
        else:
            caption = f"{self.selected_start_date:%Y-%m-%d} ～ {self.selected_end_date:%Y-%m-%d}"
        self.time_filter_button.setText(f"{caption}  ▾")
        self.time_filter_button.setToolTip(f"按测量完成日期筛选：{caption}")
        self.time_filter_button.setProperty("active", self.selected_start_date is not None)
        self.time_filter_button.style().unpolish(self.time_filter_button)
        self.time_filter_button.style().polish(self.time_filter_button)

    def show_time_filter(self) -> None:
        """复用单层日期面板，关闭时丢弃未提交日期草稿。

        Args:
            无。

        Returns:
            None  # 再次点击关闭面板，仅确定和清除会应用条件
        """
        if self.time_filter_panel is not None and self.time_filter_panel.isVisible():
            self.time_filter_panel.close()
            return
        view = FlyoutView(title="测量完成日期", content="", isClosable=False)
        content = QWidget()
        content.setFixedWidth(346)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(12)
        today = QDate.currentDate()
        picker = DateRangePicker(
            QDate(self.selected_start_date) if self.selected_start_date else today,
            QDate(self.selected_end_date) if self.selected_end_date else today,
            content,
        )
        layout.addWidget(picker)

        # 使用现有面板的 Esc、外部点击和触发按钮开关行为。
        actions = QHBoxLayout()
        clear_button = TransparentPushButton("清除时间")
        cancel_button = TransparentPushButton("取消")
        apply_button = PrimaryPushButton("确定")
        actions.addWidget(clear_button)
        actions.addStretch()
        actions.addWidget(cancel_button)
        actions.addWidget(apply_button)
        layout.addLayout(actions)
        view.addWidget(content)
        panel = HistoryTimeFilterPanel(view, self.time_filter_button, self.window())
        self.time_filter_panel = panel

        def clear_panel_reference() -> None:
            """关闭时仅清除当前日期面板引用。

            Args:
                无。

            Returns:
                None  # 新面板不受旧面板延迟关闭影响
            """
            if self.time_filter_panel is panel:
                self.time_filter_panel = None

        panel.closed.connect(clear_panel_reference)
        apply_button.clicked.connect(
            lambda: self.apply_time_filter(picker.start_date.toPython(), picker.end_date.toPython())
        )
        clear_button.clicked.connect(lambda: self.apply_time_filter(None, None))
        for button in (clear_button, cancel_button, apply_button):
            button.clicked.connect(panel.close)
        panel.show()
        animation = FlyoutAnimationManager.make(FlyoutAnimationType.DROP_DOWN, panel)
        panel.exec(animation.position(self.time_filter_button), FlyoutAnimationType.DROP_DOWN)

    def change_preview_scenario(self) -> None:
        """切换演示场景并清除上一场景的筛选条件。

        Args:
            无。

        Returns:
            None  # 新场景从第一页呈现，不与旧筛选混淆
        """
        scenario_index = self.scenario_combo.currentIndex()
        self.reset_filters()
        self.scenario_combo.blockSignals(True)
        self.scenario_combo.setCurrentIndex(scenario_index)
        self.scenario_combo.blockSignals(False)
        self.viewer.close()
        self.render_records()

    def filter_preview_records(self) -> list[PreviewMeasurement]:
        """仅在内存中组合演示场景与已提交筛选条件。

        Args:
            无。

        Returns:
            [PreviewMeasurement(...)]  # 符合当前条件的虚构测量，顺序保持不变
        """
        scenario = self.scenario_combo.currentData()
        if scenario in ("empty", "no_results"):
            return []
        matching_records = []
        for record in self.preview_records:
            # 先筛选测量场景、机器、复核状态和完成日期。
            if scenario != "all" and record.evidence_state != scenario:
                continue
            if self.selected_machine_id is not None and record.machine_id != self.selected_machine_id:
                continue
            if self.selected_review_status is not None and record.review_status != self.selected_review_status:
                continue
            if self.selected_start_date is not None:
                if not self.selected_start_date <= record.finished_at.date() <= self.selected_end_date:
                    continue

            # 位数和文字必须命中同一条完整有效文字行。
            if self.selected_text_query or self.selected_text_length is not None:
                matched_line = False
                for line in record.effective_lines:
                    if self.selected_text_length is not None and len(line) != self.selected_text_length:
                        continue
                    query = self.selected_text_query
                    if not query or (line == query if self.selected_match_mode == "exact" else query in line):
                        matched_line = True
                        break
                if not matched_line:
                    continue
            matching_records.append(record)
        return matching_records

    def render_records(self) -> None:
        """按当前条件分页并仅建立当前页的测量卡片。

        Args:
            无。

        Returns:
            None  # 当前页、空状态和按测量计数的分页已同步
        """
        records = self.filter_preview_records() if self.preview_enabled else []
        total = len(records)
        self.total_pages = max(1, (total + self.page_size - 1) // self.page_size)
        self.current_page = min(self.current_page, self.total_pages)
        page_start = (self.current_page - 1) * self.page_size

        # 删除上一页组件，不保存跨页图片缓存。
        for card in self.cards:
            self.grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        for record in records[page_start:page_start + self.page_size]:
            card = MeasurementEvidenceCard(record)
            card.clicked.connect(lambda checked=False, measurement=record: self.viewer.open_record(measurement))
            self.cards.append(card)
        self.grid_columns = 0
        self.reflow_cards()

        # 更新空状态及恢复入口。
        self.grid_body.setVisible(bool(self.cards))
        self.empty_state.setVisible(not self.cards)
        self.empty_action.setVisible(not self.cards)
        if not self.preview_enabled:
            self.empty_title.setText("图片管理 UI 已就绪")
            self.empty_body.setText("打开演示预览，查看测量分组、证据查看器与异常状态。\n真实图片读取将在视觉确认后接入。")
            self.empty_action.setText("打开演示预览")
        elif self.scenario_combo.currentData() == "empty":
            self.empty_title.setText("暂无测量记录 · 演示")
            self.empty_body.setText("测量完成并保存后，将按每次测量展示证据图片。")
            self.empty_action.setText("返回分组演示")
        else:
            self.empty_title.setText("没有符合条件的测量 · 演示")
            self.empty_body.setText("试试其他文字、位数、机器或完成日期，也可以重置筛选。")
            self.empty_action.setText("重置筛选")

        # 记录数不表示图片数，更不表示原始证据完整。
        count = f"演示：共 {total} 次测量" if self.preview_enabled else "真实测量数据待接入"
        self.record_count_label.setText(count)
        self.page_label.setText(f"{self.current_page} / {self.total_pages}")
        self.previous_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < self.total_pages)
        self.results_scroll.verticalScrollBar().setValue(0)

    def reflow_cards(self) -> None:
        """按可用宽度在三列和四列之间重排当前页卡片。

        Args:
            无。

        Returns:
            None  # 卡片均分列宽，尺寸变化不重建图片
        """
        columns = 4 if self.results_scroll.viewport().width() >= 1240 else 3
        if columns == self.grid_columns:
            return
        for card in self.cards:
            self.grid.removeWidget(card)
        for column in range(4):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        self.grid_columns = columns

    def change_page(self, page_number: int) -> None:
        """在有效页码内切换演示测量记录。

        Args:
            page_number: 目标页码，从一开始。

        Returns:
            None  # 有效目标页显示对应测量，越界请求不改变内容
        """
        if 1 <= page_number <= self.total_pages and page_number != self.current_page:
            self.current_page = page_number
            self.render_records()

    def handle_empty_action(self) -> None:
        """从空状态开启演示或恢复全部演示记录。

        Args:
            无。

        Returns:
            None  # 演示入口和无结果恢复入口已执行
        """
        if self.preview_enabled:
            self.reset_filters()
        else:
            self.set_preview_enabled(True)

    def eventFilter(self, watched, event) -> bool:
        """在滚动视口宽度变化后重排当前页卡片。

        Args:
            watched: 接收事件的视口。
            event: Qt 事件。

        Returns:
            False  # 尺寸事件继续交给原控件处理
        """
        if watched is self.results_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.reflow_cards()
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:
        """离开页面时关闭未提交日期草稿和证据查看器。

        Args:
            event: Qt 隐藏事件。

        Returns:
            None  # 浮层已关闭，已应用筛选和演示选择仍保留
        """
        if self.time_filter_panel is not None:
            self.time_filter_panel.close()
        self.viewer.close()
        super().hideEvent(event)
