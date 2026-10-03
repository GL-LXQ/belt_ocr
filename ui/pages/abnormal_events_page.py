"""查询异常事件并查看已保存的原始内容。"""

from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    CaptionLabel,
    ComboBox,
    FluentIcon,
    FlyoutAnimationType,
    FlyoutView,
    InfoBar,
    MaskDialogBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    SubtitleLabel,
    TableWidget,
    TitleLabel,
    TransparentPushButton,
    setCustomStyleSheet,
)
from qfluentwidgets.components.widgets.flyout import FlyoutAnimationManager

from src.controller.controller import AppController
from ui.date_range_picker import DateRangeFilterPanel, DateRangePicker
from ui.theme import COLORS


def format_event_time(created_at: float) -> str:
    """将异常事件写入时间转换为本地显示时间。

    Args:
        created_at: 异常事件写入数据库时的 Unix 时间戳。

    Returns:
        返回示例：
            "2026-09-27 16:09:30"  # 本地记录时间
    """
    return datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")


class AbnormalEventsPage(QWidget):
    """组织异常事件机器与日期筛选、列表和只读详情。"""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        """创建异常事件页面并绑定查询操作。

        Args:
            controller: 界面业务控制器。
            parent: 所属主窗口。

        Returns:
            返回示例：
                None  # 页面控件已建立，进入页面时再读取记录
        """
        super().__init__(parent)
        self.setObjectName("abnormal_events")
        self.controller = controller
        self.machine_names_by_id: dict[str, str] = {}
        self.current_payload_json = ""

        # 保存已应用的本地日期范围和当前草稿面板。
        self.selected_start_date: date | None = None
        self.selected_end_date: date | None = None
        self.time_filter_panel: DateRangeFilterPanel | None = None

        # 创建页面标题。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 4, 24, 20)
        layout.setSpacing(18)
        title = TitleLabel("异常事件")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # 在一行筛选栏中放置机器和日期入口。
        filter_card = SimpleCardWidget()
        filter_card.setObjectName("abnormalFilterCard")
        filter_card.setBorderRadius(16)
        filter_layout = QHBoxLayout(filter_card)
        filter_layout.setContentsMargins(20, 16, 20, 16)
        filter_layout.setSpacing(12)
        self.machine_filter = ComboBox()
        self.machine_filter.setObjectName("abnormalMachineFilter")
        self.machine_filter.setAccessibleName("机器筛选")
        self.machine_filter.addItem("全部机器", userData=None)
        self.machine_filter.setFixedSize(200, 36)
        self.machine_filter.currentIndexChanged.connect(self.reload_events)
        filter_layout.addWidget(self.machine_filter)

        # 使用历史页的单层日期面板编辑记录时间范围。
        self.time_filter_button = PushButton(FluentIcon.CALENDAR, "时间范围")
        self.time_filter_button.setObjectName("abnormalTimeFilter")
        self.time_filter_button.setAccessibleName("时间筛选")
        self.time_filter_button.setFixedSize(270, 36)
        self.time_filter_button.clicked.connect(self.show_time_filter_flyout)
        self.update_time_filter_button()
        filter_layout.addWidget(self.time_filter_button)
        filter_layout.addStretch()

        # 刷新时继续保留已选择的机器和日期范围。
        self.refresh_button = TransparentPushButton(FluentIcon.SYNC, "刷新")
        self.refresh_button.setObjectName("abnormalRefreshButton")
        self.refresh_button.setFixedSize(88, 36)
        self.refresh_button.clicked.connect(self.refresh_events)
        filter_layout.addWidget(self.refresh_button)
        layout.addWidget(filter_card)

        # 创建五列只读异常事件表格。
        content = SimpleCardWidget()
        content.setObjectName("abnormalEventsCard")
        content.setBorderRadius(16)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 12, 16, 12)
        content_layout.setSpacing(12)
        self.table = TableWidget()
        self.table.setObjectName("abnormalEventsTable")
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(("机器", "记录时间", "异常原因", "摘要", "详情"))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(58)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        table_header = self.table.horizontalHeader()
        table_header.setSectionsClickable(False)
        table_header.setFixedHeight(44)
        table_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column_index, width in ((0, 160), (1, 180), (2, 240), (4, 80)):
            self.table.setColumnWidth(column_index, width)
        content_layout.addWidget(self.table, 1)

        # 在空表格中显示提示，保留原来的列名。
        self.empty_label = BodyLabel("暂无异常事件", self.table.viewport())
        self.empty_label.setObjectName("abnormalEmptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.table.viewport())
        empty_layout.addWidget(self.empty_label, alignment=Qt.AlignmentFlag.AlignCenter)

        # 在表格底部显示当前结果数量。
        self.event_count_label = CaptionLabel("0 条事件")
        self.event_count_label.setObjectName("abnormalEventCount")
        count_layout = QHBoxLayout()
        count_layout.setContentsMargins(8, 0, 4, 0)
        count_layout.addWidget(self.event_count_label)
        count_layout.addStretch()
        content_layout.addLayout(count_layout)
        layout.addWidget(content, 1)

        # 为页面控件追加统一的浅色样式。
        stylesheet_path = Path(__file__).parents[1] / "styles" / "abnormal_events_page.qss"
        self.abnormal_stylesheet = stylesheet_path.read_text(encoding="utf-8")
        for name, color in COLORS.items():
            self.abnormal_stylesheet = self.abnormal_stylesheet.replace(f"@{name}", color)
        for widget in (
            title, filter_card, content, self.machine_filter, self.time_filter_button,
            self.refresh_button, self.table, self.empty_label, self.event_count_label,
        ):
            setCustomStyleSheet(widget, self.abnormal_stylesheet, self.abnormal_stylesheet)

        # 建立复用的只读详情对话框。
        self.build_detail_dialog()

    def build_detail_dialog(self) -> None:
        """创建以异常原因和记录信息为主的详情对话框。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 详情字段、原始数据折叠区和复制按钮已建立
        """
        # 在遮罩弹窗中建立标题和可滚动正文。
        dialog_parent = self.parentWidget() or self
        self.detail_dialog = MaskDialogBase(dialog_parent)
        self.detail_dialog.setObjectName("abnormalEventDetail")
        self.detail_dialog.widget.setObjectName("abnormalEventDetailContent")
        detail_layout = QVBoxLayout(self.detail_dialog.widget)
        detail_layout.setContentsMargins(24, 22, 24, 20)
        detail_layout.setSpacing(18)
        detail_layout.addWidget(SubtitleLabel("事件详情"))
        self.detail_scroll = ScrollArea()
        self.detail_scroll.setObjectName("abnormalDetailScroll")
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_body = QWidget()
        detail_body.setObjectName("abnormalDetailBody")
        body_layout = QVBoxLayout(detail_body)
        body_layout.setContentsMargins(0, 0, 6, 0)
        body_layout.setSpacing(16)
        self.detail_values: dict[str, QLabel] = {}
        selectable_text = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard

        # 将原始异常原因放在详情首位。
        reason_label = QLabel("--")
        reason_label.setObjectName("abnormalDetailReason")
        reason_label.setTextFormat(Qt.TextFormat.PlainText)
        reason_label.setWordWrap(True)
        reason_label.setTextInteractionFlags(selectable_text)
        self.detail_values["reason"] = reason_label
        body_layout.addWidget(reason_label)

        # 并排展示机器名称和记录时间。
        summary_card = SimpleCardWidget()
        summary_card.setObjectName("abnormalDetailSummaryCard")
        summary_card.setBorderRadius(12)
        summary_layout = QHBoxLayout(summary_card)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setSpacing(24)
        for field_name, caption in (("machine_name", "机器"), ("created_at", "记录时间")):
            field_layout = QVBoxLayout()
            field_layout.setSpacing(6)
            field_label = CaptionLabel(caption)
            field_label.setObjectName("abnormalDetailKey")
            value_label = QLabel("--")
            value_label.setObjectName("abnormalDetailValue")
            value_label.setTextFormat(Qt.TextFormat.PlainText)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(selectable_text)
            field_layout.addWidget(field_label)
            field_layout.addWidget(value_label)
            summary_layout.addLayout(field_layout, 1)
            self.detail_values[field_name] = value_label
        body_layout.addWidget(summary_card)

        # 将可复制的定位编号集中放在原因信息下方。
        identity_layout = QGridLayout()
        identity_layout.setHorizontalSpacing(14)
        identity_layout.setVerticalSpacing(12)
        identity_layout.setColumnStretch(1, 1)
        for row_index, (field_name, caption) in enumerate((
            ("abnormal_event_id", "事件 ID"),
            ("machine_id", "机器 ID"),
            ("session_id", "Session ID"),
        )):
            field_label = CaptionLabel(caption)
            field_label.setObjectName("abnormalDetailKey")
            value_label = QLabel("--")
            value_label.setObjectName("abnormalDetailIdentity")
            value_label.setTextFormat(Qt.TextFormat.PlainText)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(selectable_text)
            value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            identity_layout.addWidget(field_label, row_index, 0)
            identity_layout.addWidget(value_label, row_index, 1)
            self.detail_values[field_name] = value_label
        self.copy_session_button = TransparentPushButton(FluentIcon.COPY, "复制")
        self.copy_session_button.setObjectName("abnormalCopySession")
        self.copy_session_button.setAccessibleName("复制 Session ID")
        self.copy_session_button.clicked.connect(self.copy_session_id)
        identity_layout.addWidget(self.copy_session_button, 2, 2)
        body_layout.addLayout(identity_layout)

        # 默认折叠完整原始数据，展开后再显示复制入口。
        self.payload_toggle_button = TransparentPushButton(FluentIcon.CHEVRON_RIGHT_MED, "原始数据 JSON")
        self.payload_toggle_button.setObjectName("abnormalPayloadToggle")
        self.payload_toggle_button.setCheckable(True)
        self.payload_toggle_button.toggled.connect(self.toggle_payload)
        body_layout.addWidget(self.payload_toggle_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.payload_container = QWidget()
        self.payload_container.setObjectName("abnormalPayloadContainer")
        payload_layout = QVBoxLayout(self.payload_container)
        payload_layout.setContentsMargins(0, 0, 0, 0)
        payload_layout.setSpacing(8)
        self.detail_payload = PlainTextEdit()
        self.detail_payload.setObjectName("abnormalEventPayload")
        self.detail_payload.setReadOnly(True)
        self.detail_payload.setMinimumHeight(170)
        self.detail_payload.setMaximumHeight(240)
        payload_layout.addWidget(self.detail_payload)
        self.copy_payload_button = TransparentPushButton(FluentIcon.COPY, "复制原始 JSON")
        self.copy_payload_button.setObjectName("abnormalCopyPayload")
        self.copy_payload_button.clicked.connect(self.copy_payload_json)
        payload_layout.addWidget(self.copy_payload_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.payload_container.hide()
        body_layout.addWidget(self.payload_container)
        body_layout.addStretch()
        self.detail_scroll.setWidget(detail_body)
        detail_layout.addWidget(self.detail_scroll, 1)

        # 在底部保留唯一的关闭操作。
        close_button = PushButton("关闭")
        close_button.setObjectName("abnormalDetailClose")
        close_button.setFixedSize(88, 36)
        close_button.clicked.connect(self.detail_dialog.close)
        detail_layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.detail_dialog.widget.setStyleSheet(self.abnormal_stylesheet)
        for widget in (
            summary_card, self.payload_toggle_button, self.detail_payload,
            self.copy_session_button, self.copy_payload_button, close_button,
        ):
            setCustomStyleSheet(widget, self.abnormal_stylesheet, self.abnormal_stylesheet)
        self.detail_dialog.hide()

    def update_time_filter_button(self) -> None:
        """在日期按钮中显示已应用的记录时间范围。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 按钮显示不限时间、单日或完整日期范围
        """
        # 将已应用的日期范围转换为按钮文字。
        if self.selected_start_date is None:
            caption = "时间范围"
        elif self.selected_start_date == self.selected_end_date:
            caption = f"{self.selected_start_date:%Y-%m-%d}"
        else:
            caption = f"{self.selected_start_date:%Y-%m-%d} ～ {self.selected_end_date:%Y-%m-%d}"

        # 同步按钮文字与本地记录日期提示。
        self.time_filter_button.setText(f"{caption}  ▾")
        self.time_filter_button.setToolTip(f"按本地记录日期筛选：{caption}")

    def apply_time_filter(self, start_date: date | None, end_date: date | None) -> None:
        """应用日期草稿或清除时间条件并保留当前机器选择。

        Args:
            start_date: 本地开始日期，None 表示不限时间。
            end_date: 本地结束日期，与开始日期同时设置或清除。

        Returns:
            返回示例：
                None  # 日期条件和按钮已更新，列表只刷新一次
        """
        # 保存已应用的日期条件。
        self.selected_start_date = start_date
        self.selected_end_date = end_date

        # 刷新日期提示并按机器和日期组合查询。
        self.update_time_filter_button()
        self.reload_events()

    def show_time_filter_flyout(self) -> None:
        """使用现有单层日历面板编辑临时日期范围。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 确定或清除时应用条件，取消和关闭时丢弃草稿
        """
        # 再次点击按钮时只关闭当前日期草稿面板。
        if self.time_filter_panel is not None and self.time_filter_panel.isVisible():
            self.time_filter_panel.close()
            return

        # 创建与历史页相同的单层日期筛选内容。
        view = FlyoutView(title="时间筛选", content="", isClosable=False)
        content = QWidget()
        content.setFixedWidth(346)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 10, 16, 12)
        content_layout.setSpacing(12)

        # 从已应用的日期恢复草稿，未筛选时使用今天。
        current_date = QDate.currentDate()
        date_range = DateRangePicker(
            QDate(self.selected_start_date) if self.selected_start_date else current_date,
            QDate(self.selected_end_date) if self.selected_end_date else current_date,
            content,
        )
        content_layout.addWidget(date_range)

        # 确定和清除日期应用查询，取消只关闭面板。
        actions = QHBoxLayout()
        clear_button = TransparentPushButton("清除时间")
        cancel_button = TransparentPushButton("取消")
        apply_button = PrimaryPushButton("确定")
        actions.addWidget(clear_button)
        actions.addStretch()
        actions.addWidget(cancel_button)
        actions.addWidget(apply_button)
        content_layout.addLayout(actions)
        view.addWidget(content)

        # 复用非 Popup 浮层及其 Esc 和外部点击处理。
        panel = DateRangeFilterPanel(view, self.time_filter_button, self.window())
        self.time_filter_panel = panel

        def clear_panel_reference() -> None:
            """只清理对应已关闭面板的引用。

            Args:
                无外部参数。

            Returns:
                返回示例：
                    None  # 旧面板的迟到通知不会清理新面板
            """
            if self.time_filter_panel is panel:
                self.time_filter_panel = None

        panel.closed.connect(clear_panel_reference)

        # 沿用现有面板定位与下拉动画。
        panel.show()
        animation = FlyoutAnimationManager.make(FlyoutAnimationType.DROP_DOWN, panel)
        panel.exec(animation.position(self.time_filter_button), FlyoutAnimationType.DROP_DOWN)

        # 将应用、清除和取消分别连接到已有查询和关闭入口。
        apply_button.clicked.connect(
            lambda: self.apply_time_filter(date_range.start_date.toPython(), date_range.end_date.toPython())
        )
        clear_button.clicked.connect(lambda: self.apply_time_filter(None, None))
        cancel_button.clicked.connect(panel.close)
        apply_button.clicked.connect(panel.close)
        clear_button.clicked.connect(panel.close)

    def hideEvent(self, event) -> None:
        """页面离开时关闭未应用的日期草稿面板。

        Args:
            event: Qt 隐藏事件。

        Returns:
            返回示例：
                None  # 面板和应用事件过滤器已关闭，已应用条件保留
        """
        if self.time_filter_panel is not None:
            self.time_filter_panel.close()
        super().hideEvent(event)

    def refresh_events(self) -> None:
        """更新机器选项与名称并按当前条件重新查询异常事件。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器选项和异常列表已更新
        """
        # 读取异常记录中出现过的机器编号。
        result = self.controller.list_abnormal_event_machine_ids()
        if not result.success:
            self.show_load_error(result.message)
            return
        machine_ids = result.data["machine_ids"]

        # 读取现有机器名称，无法读取时继续显示原始编号。
        machines_result = self.controller.list_machines()
        self.machine_names_by_id = {}
        if machines_result.success:
            self.machine_names_by_id = {
                str(machine["id"]): machine["machine_name"]
                for machine in machines_result.data["machines"]
            }

        # 更新机器选项并保留仍可使用的当前选择。
        selected_machine_id = self.machine_filter.currentData()
        self.machine_filter.blockSignals(True)
        self.machine_filter.clear()
        self.machine_filter.addItem("全部机器", userData=None)
        for machine_id in machine_ids:
            caption = self.machine_names_by_id.get(machine_id, machine_id)
            self.machine_filter.addItem(caption, userData=machine_id)
        selected_index = self.machine_filter.findData(selected_machine_id)
        self.machine_filter.setCurrentIndex(max(selected_index, 0))
        self.machine_filter.blockSignals(False)

        # 使用当前机器和日期条件刷新列表。
        self.reload_events()

    def reload_events(self) -> None:
        """按机器和本地记录日期填充异常事件列表。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 表格显示符合机器和日期条件的异常事件
        """
        # 通过现有接口读取机器和日期条件下的全部周期。
        result = self.controller.list_abnormal_events(
            self.machine_filter.currentData(),
            None,
            start_date=self.selected_start_date,
            end_date=self.selected_end_date,
        )
        if not result.success:
            self.show_load_error(result.message)
            return
        events = result.data["events"]

        # 显示真实的结果数量并区分空列表。
        self.table.setRowCount(len(events))
        self.event_count_label.setText(f"{len(events)} 条事件")
        self.empty_label.setText("该机器暂无异常事件" if self.machine_filter.currentData() else "暂无异常事件")
        if self.selected_start_date is not None:
            self.empty_label.setText("当前筛选条件下暂无异常事件")
        self.empty_label.setVisible(not events)

        # 将机器名称、记录时间、原始原因和服务摘要填入表格。
        for row_index, event in enumerate(events):
            machine_id = event["machine_id"]
            values = (
                self.machine_names_by_id.get(machine_id, machine_id or "--"),
                format_event_time(event["created_at"]),
                event["reason"],
                event["payload_summary"],
            )
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column_index == 2:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row_index, column_index, item)

            # 每行详情只按事件主键读取，避免同一周期的多条异常混淆。
            button = TransparentPushButton("查看")
            button.setObjectName("abnormalViewButton")
            button.setAccessibleName(f"查看异常事件 {event['abnormal_event_id']}")
            button.clicked.connect(
                lambda checked=False, event_id=event["abnormal_event_id"]: self.show_event_detail(event_id)
            )
            setCustomStyleSheet(button, self.abnormal_stylesheet, self.abnormal_stylesheet)
            self.table.setCellWidget(row_index, 4, button)

        # 在页面完成显示后对齐单元格中的详情按钮。
        QTimer.singleShot(0, self.table.doItemsLayout)

    def show_load_error(self, message: str) -> None:
        """清空旧结果并显示本次读取失败提示。

        Args:
            message: 现有控制器返回的失败说明。

        Returns:
            返回示例：
                None  # 旧列表已清空，页面显示读取失败
        """
        self.table.setRowCount(0)
        self.event_count_label.setText("-- 条事件")
        self.empty_label.setText("异常事件读取失败，请重试刷新")
        self.empty_label.show()
        InfoBar.error("异常事件读取失败", message, duration=-1, parent=self)

    def show_event_detail(self, abnormal_event_id: int) -> None:
        """按异常事件主键读取记录并显示详情。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            返回示例：
                None  # 对应详情已显示，读取失败时显示提示
        """
        # 查询当前异常记录并处理已删除或读取失败的情况。
        result = self.controller.get_abnormal_event(abnormal_event_id)
        if not result.success:
            InfoBar.error("异常详情读取失败", result.message, duration=-1, parent=self)
            return
        event = result.data["event"]
        if event is None:
            InfoBar.error("异常详情读取失败", "该异常记录已不存在。", duration=-1, parent=self)
            return

        # 显示现有异常原因、机器名称和记录时间。
        machine_id = event["machine_id"]
        self.detail_values["reason"].setText(event["reason"])
        self.detail_values["machine_name"].setText(self.machine_names_by_id.get(machine_id, machine_id or "--"))
        self.detail_values["created_at"].setText(format_event_time(event["created_at"]))
        self.detail_values["abnormal_event_id"].setText(str(event["abnormal_event_id"]))
        self.detail_values["machine_id"].setText(machine_id or "--")
        self.detail_values["session_id"].setText(event["session_id"] or "--")
        self.copy_session_button.setEnabled(bool(event["session_id"]))

        # 保存原始 JSON，复制时不改变空白、转义或换行。
        self.current_payload_json = event["payload_json"]
        self.detail_payload.setPlainText(self.current_payload_json)
        self.detail_payload.verticalScrollBar().setValue(0)
        self.payload_toggle_button.setChecked(False)
        self.toggle_payload(False)
        self.detail_scroll.verticalScrollBar().setValue(0)
        self.detail_dialog.open()

    def toggle_payload(self, expanded: bool) -> None:
        """展开或折叠原始数据并调整详情高度。

        Args:
            expanded: 是否展开原始 JSON 内容。

        Returns:
            返回示例：
                None  # 原始数据区域和详情高度已同步
        """
        self.payload_container.setVisible(expanded)
        self.payload_toggle_button.setIcon(FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED)
        dialog_parent = self.detail_dialog.parentWidget()
        self.detail_dialog.widget.setFixedSize(
            min(720, dialog_parent.width() - 80),
            min(660 if expanded else 440, dialog_parent.height() - 80),
        )

    def copy_session_id(self) -> None:
        """将当前详情中的周期编号复制到剪贴板。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # Session ID 已复制
        """
        QApplication.clipboard().setText(self.detail_values["session_id"].text())

    def copy_payload_json(self) -> None:
        """将当前异常记录的完整原始 JSON 复制到剪贴板。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 原始 JSON 已逐字复制
        """
        QApplication.clipboard().setText(self.current_payload_json)
