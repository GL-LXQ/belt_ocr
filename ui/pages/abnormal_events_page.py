"""查询异常事件并查看已保存的原始内容。"""

from datetime import datetime

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.service.abnormal_event_service import (
    AbnormalEventService,
    AbnormalEventServiceError,
)
from ui.theme import create_icon


def format_event_time(created_at: float) -> str:
    """将异常事件时间戳转换为本地显示时间。

    Args:
        created_at: 异常事件的 Unix 时间戳。

    Returns:
        返回示例：
            "2026-09-27 16:09:30"  # 本地发生时间
    """
    return datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")


class AbnormalEventsPage(QWidget):
    """组织异常事件筛选、列表和只读详情。"""

    def __init__(self, abnormal_event_service: AbnormalEventService) -> None:
        """创建异常事件页面并绑定查询操作。

        Args:
            abnormal_event_service: 异常事件读取服务。

        Returns:
            返回示例：
                None  # 页面控件已建立，进入页面时再读取记录
        """
        super().__init__()
        self.setObjectName("abnormal_events")
        self.abnormal_event_service = abnormal_event_service

        # 创建页面标题和说明。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(create_icon("abnormal_events", "blue").pixmap(QSize(24, 24)))
        header.addWidget(icon)
        heading = QVBoxLayout()
        title = QLabel("异常事件")
        title.setObjectName("pageTitle")
        subtitle = QLabel("查看测量运行失败的原因和原始事件信息")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        layout.addLayout(header)

        # 创建机器筛选、完整 Session ID 搜索和刷新按钮。
        filters = QHBoxLayout()
        filters.setSpacing(8)
        filters.addWidget(QLabel("机器："))
        self.machine_filter = QComboBox()
        self.machine_filter.setObjectName("abnormalMachineFilter")
        self.machine_filter.addItem("全部机器", None)
        self.machine_filter.setMinimumWidth(150)
        self.machine_filter.currentIndexChanged.connect(self.reload_events)
        filters.addWidget(self.machine_filter)
        filters.addSpacing(16)
        filters.addWidget(QLabel("Session ID："))
        self.session_search = QLineEdit()
        self.session_search.setObjectName("abnormalSessionSearch")
        self.session_search.setPlaceholderText("输入完整 Session ID")
        self.session_search.setMinimumWidth(300)
        self.session_search.returnPressed.connect(self.reload_events)
        filters.addWidget(self.session_search)
        search_button = QPushButton("搜索")
        search_button.clicked.connect(self.reload_events)
        filters.addWidget(search_button)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_events)
        filters.addWidget(refresh_button)
        filters.addStretch()
        layout.addLayout(filters)

        # 创建六列只读异常事件表格。
        content = QFrame()
        content.setObjectName("pageContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("abnormalEventsTable")
        self.table.setHorizontalHeaderLabels((
            "发生时间", "机器", "Session ID", "异常原因", "详情摘要", "查看"
        ))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table_header = self.table.horizontalHeader()
        table_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column_index, width in ((0, 170), (1, 80), (2, 270), (3, 190), (5, 80)):
            self.table.setColumnWidth(column_index, width)
        content_layout.addWidget(self.table)
        layout.addWidget(content, 1)

        # 建立复用的只读详情对话框。
        self.build_detail_dialog()

    def build_detail_dialog(self) -> None:
        """创建异常事件详情对话框。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 详情字段和关闭按钮已建立
        """
        # 创建发生时间、机器、周期和原因字段。
        self.detail_dialog = QDialog(self)
        self.detail_dialog.setObjectName("abnormalEventDetail")
        self.detail_dialog.setWindowTitle("异常事件详情")
        self.detail_dialog.setWindowFlag(
            Qt.WindowType.WindowContextHelpButtonHint, False
        )
        self.detail_dialog.resize(660, 480)
        detail_layout = QVBoxLayout(self.detail_dialog)
        fields = QFormLayout()
        self.detail_values: dict[str, QLabel] = {}
        selectable_text = Qt.TextInteractionFlag.TextSelectableByMouse
        for field_name, caption in (
            ("created_at", "发生时间"),
            ("machine_id", "机器 ID"),
            ("session_id", "Session ID"),
            ("reason_label", "异常原因"),
            ("reason", "原因码"),
        ):
            value_label = QLabel("--")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(selectable_text)
            fields.addRow(caption + "：", value_label)
            self.detail_values[field_name] = value_label
        detail_layout.addLayout(fields)

        # 创建完整 payload 的只读展示区。
        detail_layout.addWidget(QLabel("完整 payload："))
        self.detail_payload = QPlainTextEdit()
        self.detail_payload.setObjectName("abnormalEventPayload")
        self.detail_payload.setReadOnly(True)
        detail_layout.addWidget(self.detail_payload, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.detail_dialog.close)
        detail_layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

    def refresh_events(self) -> None:
        """更新机器选项并按当前条件重新查询异常事件。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器选项和异常列表已更新
        """
        # 读取异常记录中出现过的机器编号。
        try:
            machine_ids = self.abnormal_event_service.list_machine_ids()
        except AbnormalEventServiceError as error:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "异常事件读取失败", str(error))
            return

        # 更新机器选项并保留仍可使用的当前选择。
        selected_machine_id = self.machine_filter.currentData()
        self.machine_filter.blockSignals(True)
        self.machine_filter.clear()
        self.machine_filter.addItem("全部机器", None)
        for machine_id in machine_ids:
            self.machine_filter.addItem(f"机器 {machine_id}", machine_id)
        selected_index = self.machine_filter.findData(selected_machine_id)
        self.machine_filter.setCurrentIndex(max(selected_index, 0))
        self.machine_filter.blockSignals(False)

        # 使用当前机器和 Session ID 条件刷新列表。
        self.reload_events()

    def reload_events(self) -> None:
        """按机器和完整 Session ID 填充异常事件列表。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 表格显示符合筛选条件的异常事件
        """
        # 读取符合当前筛选条件的异常记录。
        session_id = self.session_search.text().strip() or None
        try:
            events = self.abnormal_event_service.list_events(
                self.machine_filter.currentData(), session_id
            )
        except AbnormalEventServiceError as error:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "异常事件读取失败", str(error))
            return

        # 将发生时间、身份、中文原因和摘要填入表格。
        self.table.setRowCount(len(events))
        for row_index, event in enumerate(events):
            values = (
                format_event_time(event["created_at"]),
                event["machine_id"] or "--",
                event["session_id"] or "--",
                event["reason_label"],
                event["payload_summary"],
            )
            for column_index, value in enumerate(values):
                self.table.setItem(row_index, column_index, QTableWidgetItem(value))

            # 为当前异常记录建立按主键读取的详情入口。
            button = QPushButton("查看")
            button.setProperty("buttonRole", "text")
            abnormal_event_id = event["abnormal_event_id"]
            button.clicked.connect(
                lambda checked=False, event_id=abnormal_event_id: (
                    self.show_event_detail(event_id)
                )
            )
            self.table.setCellWidget(row_index, 5, button)

    def show_event_detail(self, abnormal_event_id: int) -> None:
        """按异常事件主键读取记录并显示完整详情。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            返回示例：
                None  # 对应详情已显示，读取失败时显示提示
        """
        # 查询当前异常记录并处理已删除或读取失败的情况。
        try:
            event = self.abnormal_event_service.get_event(abnormal_event_id)
        except AbnormalEventServiceError as error:
            QMessageBox.warning(self, "异常详情读取失败", str(error))
            return
        if event is None:
            QMessageBox.warning(self, "异常详情读取失败", "该异常记录已不存在。")
            return

        # 显示中文原因、原始原因码和完整原始内容。
        self.detail_values["created_at"].setText(format_event_time(event["created_at"]))
        self.detail_values["machine_id"].setText(event["machine_id"] or "--")
        self.detail_values["session_id"].setText(event["session_id"] or "--")
        self.detail_values["reason_label"].setText(event["reason_label"])
        self.detail_values["reason"].setText(event["reason"])
        self.detail_payload.setPlainText(event["payload_json"])
        self.detail_dialog.open()
