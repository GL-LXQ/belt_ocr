"""实时监测页面、设备卡片和步骤进度组件。"""

from html import escape
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.service.machine_service import MachineService, MachineServiceError
from src.service.monitoring_service import MonitoringService
from ui.demo_data import LOG_ROWS
from ui.theme import create_icon


# 卡片区每行固定放置的设备数量。
CARDS_PER_ROW = 3


class CapturePreview(QLabel):
    """暂无实时画面时的灰色占位区域。"""

    def __init__(self):
        """创建随卡片宽度伸缩的画面占位块。

        Args:
            无。

        Returns:
            返回示例：
                None  # 创建灰色画面占位块
        """
        # 设置画面区域的标识、高度范围和伸缩策略。
        super().__init__()
        self.setObjectName("capturePreview")
        self.setMinimumHeight(140)
        self.setMaximumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)


class StepProgress(QFrame):
    """复用五步进度的圆点、连接线和文字。"""

    def __init__(self):
        """创建五步进度布局和可更新标签。

        Args:
            无。

        Returns:
            返回示例：
                None  # 创建步骤组件
        """
        super().__init__()
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setHorizontalSpacing(0)
        layout.setVerticalSpacing(8)
        self.dots = []
        self.connectors = []
        self.step_labels = []

        # 在五个等宽列中放置圆点和文字，连接线置于圆点下层。
        for index, title in enumerate(("等待启动", "采集图像", "准备字符识别", "识别中", "完成")):
            dot = QLabel()
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setFixedSize(25, 25)
            layout.addWidget(dot, 0, index, Qt.AlignmentFlag.AlignCenter)
            self.dots.append(dot)
            label = QLabel(title)
            label.setProperty("smallText", True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label, 1, index)
            self.step_labels.append(label)
            layout.setColumnStretch(index, 1)
            if index < 4:
                connector = QFrame(self)
                connector.setFixedHeight(2)
                connector.lower()
                self.connectors.append(connector)

    def update_steps(self, completed: int, tone: str):
        """更新已完成步骤与当前步骤颜色。

        Args:
            completed: 已完成步骤数量。
            tone: running、idle 或 waiting 展示状态。

        Returns:
            返回示例：
                None  # 更新步骤圆点及连接线
        """
        # 根据演示状态设置步骤圆点、勾选图标和当前步骤边框。
        accent = "#FF9818" if tone == "waiting" else "#18AE59"
        for index, dot in enumerate(self.dots):
            active = (tone == "running" and index == completed) or (tone == "waiting" and index == 2)
            color = "#2F7CF6" if active and tone == "running" else accent if index < completed else "#C5CFDA"
            border = ("#A6C9FF" if tone == "running" else "#FFD3A1") if active else "white"
            checkmark = create_icon("check", "white").pixmap(QSize(16, 16))
            dot.setPixmap(checkmark if index < completed or active else QPixmap())
            self.step_labels[index].setStyleSheet("color: #34465F; font-weight: bold;" if active else "")
            dot.setStyleSheet(
                f"background: {color}; color: white; border: 2px solid {border}; border-radius: 12px;"
            )

        # 将已完成步骤之间的连接线设置为状态颜色。
        for index, connector in enumerate(self.connectors):
            color = accent if index < completed else "#D9E1EA"
            connector.setStyleSheet(f"background: {color}; border: none;")

    def resizeEvent(self, event):
        """让连接线随相邻圆点位置伸缩。

        Args:
            event: 组件尺寸变化事件。

        Returns:
            返回示例：
                None  # 将连接线两端对齐到相邻圆点边缘
        """
        super().resizeEvent(event)
        self.layout().activate()
        # 按相邻圆点的实际位置设置连接线长度与垂直中心。
        for index, connector in enumerate(self.connectors):
            left_dot = self.dots[index].geometry()
            right_dot = self.dots[index + 1].geometry()
            left = left_dot.right() + 3
            right = right_dot.left() - 3
            connector.setGeometry(left, left_dot.center().y(), right - left, 2)


class MachineCard(QFrame):
    """展示一台设备的画面占位、状态、频率、进度和事件。"""

    def __init__(self, data: dict):
        """构建设备卡片并填入展示数据。

        Args:
            data: 卡片展示数据，包含标题、状态、频率、进度和事件。

        Returns:
            返回示例：
                None  # 创建设备卡片
        """
        super().__init__()
        self.setObjectName("machineCard")
        self.setMinimumWidth(326)
        # 高度按内容决定，可以被拉高但不会被压扁。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(5)

        # 创建机器标题和状态徽标。
        heading = QHBoxLayout()
        self.machine_icon = QLabel()
        self.machine_icon.setFixedSize(28, 28)
        heading.addWidget(self.machine_icon)
        self.title = QLabel()
        self.title.setObjectName("machineTitle")
        self.badge = QLabel()
        self.badge.setObjectName("machineBadge")
        heading.addWidget(self.title)
        heading.addStretch()
        heading.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)

        # 将画面标识叠放在灰色占位块顶部。
        self.preview = CapturePreview()
        preview_layout = QVBoxLayout(self.preview)
        preview_layout.setContentsMargins(8, 7, 8, 7)
        preview_caption = QHBoxLayout()
        caption = QLabel('<span style="color:#C5CFDA">●</span> 暂无画面')
        caption.setObjectName("previewCaption")
        preview_caption.addWidget(caption)
        preview_caption.addStretch()
        preview_layout.addLayout(preview_caption)
        preview_layout.addStretch()
        layout.addWidget(self.preview, 1)

        # 并排创建当前状态与实时频率信息面板。
        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        self.state_label = QLabel()
        self.frequency_label = QLabel()
        self.state_icon = QLabel()
        self.state_icon.setObjectName("stateIcon")
        self.state_icon.setFixedSize(34, 34)
        self.state_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frequency_icon = QLabel()
        self.frequency_icon.setFixedSize(24, 24)
        for title, value in (
            ("当前状态", self.state_label),
            ("实时频率", self.frequency_label),
        ):
            panel = QFrame()
            panel.setObjectName("metricPanel")
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(10, 8, 10, 8)
            panel_layout.setSpacing(4)
            panel_layout.addWidget(QLabel(title))
            value.setObjectName("metricValue")
            value_row = QHBoxLayout()
            value_row.setSpacing(8)
            value_row.addWidget(self.state_icon if title == "当前状态" else self.frequency_icon)
            value_row.addWidget(value, 1)
            panel_layout.addLayout(value_row)
            metrics.addWidget(panel, 1)
        layout.addLayout(metrics)

        # 创建本轮步骤进度区域。
        progress_panel = QFrame()
        progress_panel.setObjectName("metricPanel")
        progress_layout = QVBoxLayout(progress_panel)
        progress_layout.setContentsMargins(8, 8, 8, 10)
        progress_layout.setSpacing(6)
        progress_layout.addWidget(QLabel("本轮进度"))
        self.steps = StepProgress()
        progress_layout.addWidget(self.steps)
        layout.addWidget(progress_panel)

        # 创建最近事件标题、禁用入口和固定数量的事件行。
        events_heading = QHBoxLayout()
        events_heading.addWidget(QLabel("最近事件"))
        events_heading.addStretch()
        self.more_button = QPushButton("更多 ›")
        self.more_button.setObjectName("moreEvents")
        self.more_button.setEnabled(False)
        self.more_button.setToolTip("暂未接入")
        events_heading.addWidget(self.more_button)
        layout.addLayout(events_heading)
        self.event_labels = []
        for index in range(4):
            label = QLabel()
            label.setObjectName("eventLine")
            label.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(label)
            self.event_labels.append(label)
        self.update_data(data)

    def update_data(self, data: dict):
        """将设备展示数据应用到已有控件。

        Args:
            data: 包含标题、状态、频率、进度和事件的卡片展示数据。

        Returns:
            返回示例：
                None  # 更新设备卡片，不创建新控件
        """
        # 更新机器标题和状态文案。
        self.setProperty("tone", data["tone"])
        self.title.setText(data["title"])
        self.badge.setText(data["status"])
        self.state_label.setText(data["state"])
        self.frequency_label.setText(data["frequency"])

        # 复用主题 SVG 图标，设置状态圆形图标和频率波形。
        color = {"running": "normal", "idle": "muted", "waiting": "warning"}[data["tone"]]
        state_icon = {"running": "play", "idle": "pause", "waiting": "hourglass"}[data["tone"]]
        machine_icon = {"running": "alarm", "idle": "fan", "waiting": "warning_mark"}[data["tone"]]
        self.state_icon.setPixmap(create_icon(state_icon, "white").pixmap(QSize(22, 22)))
        self.state_icon.setAccessibleName(data["state"])
        self.frequency_icon.setPixmap(create_icon("pulse", color).pixmap(QSize(23, 23)))
        machine_color = "white" if data["tone"] == "waiting" else "blue" if data["tone"] == "idle" else color
        self.machine_icon.setPixmap(create_icon(machine_icon, machine_color).pixmap(QSize(28, 28)))
        self.machine_icon.setStyleSheet(
            "background: #FFA43A; border-radius: 14px;" if data["tone"] == "waiting" else ""
        )

        # 更新步骤和最近事件。
        self.steps.update_steps(data["completed_steps"], data["tone"])
        for label in self.event_labels:
            label.clear()
        for index, (label, (timestamp, message)) in enumerate(zip(self.event_labels, data["events"])):
            time_color = "#2F7CF6" if index % 2 == 0 else "#18AE59"
            event_color = "#FF9818" if data["tone"] == "waiting" and index % 2 == 0 else "#2F7CF6"
            label.setText(
                f'<span style="color:{time_color}">●</span> '
                f'<span style="color:#73849B">{escape(timestamp)}</span>　'
                f'<span style="color:{event_color}">●</span>　{escape(message)}'
            )
        for widget in (self, *self.findChildren(QWidget)):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.update()


class RealtimePage(QWidget):
    """组织设备卡片、系统日志和数据库读取。"""

    def __init__(self, machine_service: MachineService):
        """初始化布局、设备卡片和本地交互。

        Args:
            machine_service: 设备业务服务。

        Returns:
            返回示例：
                None  # 创建实时监测页面，子控件随页面释放
        """
        super().__init__()
        self.setObjectName("realtime")
        self.machine_service = machine_service
        self.monitoring_service = None
        self.closing_requested = False
        self.connection_states = {}
        self.cards_by_device_id = {}
        self.configuration_path = Path(__file__).resolve().parents[2] / "config.example.json"
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer_layout.addWidget(self.scroll_area)
        content = QWidget()
        content.setObjectName("monitorContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(14)
        self.scroll_area.setWidget(content)

        # 复用主题图标与页面标题样式，创建操作区域。
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(create_icon("pulse", "blue").pixmap(QSize(26, 26)))
        header.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("实时监测")
        title.setObjectName("pageTitle")
        subtitle = QLabel("实时查看皮带机的检测画面、状态和事件")
        subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.start_button = QPushButton("▶  启动监测")
        self.stop_button = QPushButton("■  停止监测")
        self.refresh_button = QPushButton("⟳  刷新")
        for button, name in (
            (self.start_button, "startAll"),
            (self.stop_button, "stopAll"),
            (self.refresh_button, "refreshDemo"),
        ):
            button.setObjectName(name)
            button.setFixedHeight(38)
            button.setMinimumWidth(96)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            header.addWidget(button)
        self.stop_button.setEnabled(False)
        layout.addLayout(header)

        # 创建卡片网格，每行固定放置几台设备。
        cards_layout = QGridLayout()
        cards_layout.setSpacing(12)
        for column in range(CARDS_PER_ROW):
            cards_layout.setColumnStretch(column, 1)
        self.cards_layout = cards_layout
        self.machine_cards = []
        self.empty_hint = None
        layout.addLayout(cards_layout, 1)

        # 创建日志工具栏和只读表格。
        log_panel = QFrame()
        log_panel.setObjectName("logPanel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 8, 12, 10)
        log_heading = QHBoxLayout()
        log_icon = QLabel()
        log_icon.setPixmap(create_icon("logs", "text").pixmap(QSize(19, 19)))
        log_heading.addWidget(log_icon)
        log_title = QLabel("系统日志")
        log_title.setObjectName("logTitle")
        log_heading.addWidget(log_title)
        log_heading.addStretch()
        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        self.clear_button = QPushButton("清空日志")
        self.clear_button.setObjectName("clearLogs")
        log_heading.addWidget(self.auto_scroll)
        log_heading.addWidget(self.clear_button)
        log_layout.addLayout(log_heading)
        self.log_table = QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(("时间", "级别", "来源", "内容"))
        self.log_table.verticalHeader().hide()
        self.log_table.verticalHeader().setDefaultSectionSize(24)
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.log_table.horizontalHeader().setStretchLastSection(True)
        self.log_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        self.log_table.setColumnWidth(0, 110)
        self.log_table.setColumnWidth(1, 90)
        self.log_table.setColumnWidth(2, 125)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.horizontalHeader().setFixedHeight(24)
        self.log_table.setFixedHeight(126)
        log_layout.addWidget(self.log_table)
        layout.addWidget(log_panel)

        # 绑定本地交互，填入演示日志并读取设备表。
        self.start_button.clicked.connect(self.start_monitoring)
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.refresh_button.clicked.connect(self.reload_devices)
        self.clear_button.clicked.connect(lambda: self.log_table.setRowCount(0))
        self.auto_scroll.toggled.connect(lambda checked: self.log_table.scrollToBottom() if checked else None)
        self.fill_demo_logs()
        self.reload_devices()

    def reload_devices(self):
        """重新读取设备表并按记录重建卡片。

        Args:
            无。

        Returns:
            返回示例：
                None  # 卡片跟随数据库内容，读取失败时提示并清空卡片区
        """
        # 读取已启用设备，失败时提示并把卡片区置空。
        try:
            self.devices = self.machine_service.list_enabled_machines()
        except MachineServiceError as error:
            QMessageBox.warning(self, "设备读取失败", str(error))
            self.devices = []
        self.populate_cards()
        # 通知滚动区按新的卡片行数重新计算内容高度。
        self.scroll_area.widget().updateGeometry()

    def populate_cards(self):
        """按当前设备记录重建设备卡片区。

        Args:
            无。

        Returns:
            返回示例：
                None  # 卡片数量与设备记录一致，每行固定排满后换行
        """
        # 移除上一次创建的卡片。
        while self.cards_layout.count():
            widget = self.cards_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.machine_cards = []
        self.cards_by_device_id = {}
        self.empty_hint = None

        # 没有设备时显示横跨整行的空态提示。
        if not self.devices:
            hint = QLabel("暂无设备，请先在设备管理页添加。")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #73849B;")
            self.empty_hint = hint
            self.cards_layout.addWidget(hint, 0, 0, 1, CARDS_PER_ROW)
            return

        # 按设备编号创建卡片，并恢复最近一次连接结果。
        for index, device in enumerate(self.devices):
            card = MachineCard({
                "title": device["machine_name"],
                "tone": "idle",
                "status": "未启动",
                "state": "未启动监测",
                "frequency": "--",
                "completed_steps": 0,
                "events": (),
            })
            self.cards_layout.addWidget(card, index // CARDS_PER_ROW, index % CARDS_PER_ROW)
            self.machine_cards.append(card)
            device_id = str(device["id"])
            self.cards_by_device_id[device_id] = card
            status, reason = self.connection_states.get(device_id, ("未启动", ""))
            self.update_connection_state(device_id, status, reason)

    def start_monitoring(self):
        """读取设备卡片并启动一次后台监测。

        Args:
            无。

        Returns:
            None  # 后台启动，按钮等待监测结束后恢复
        """
        # 重读设备清单，清除上一轮结果并切换按钮状态。
        self.connection_states.clear()
        self.reload_devices()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        for device_id in self.cards_by_device_id:
            self.update_connection_state(device_id, "连接中", "正在初始化监测服务")

        # 在线程中连接设备，通过 Qt 信号更新主线程中的卡片。
        # 创建后台线程，先接好连接进度和线程结束两个信号，再启动线程。
        self.monitoring_service = MonitoringService(self.configuration_path)
        self.monitoring_service.camera_state_changed_signal.connect(self.update_connection_state)
        self.monitoring_service.finished.connect(self.finish_monitoring)
        self.monitoring_service.start()

    def stop_monitoring(self):
        """通知后台停止并等待后台自行完成资源释放。

        Args:
            无。

        Returns:
            None  # 停止通知已发送，界面继续处理事件
        """
        # 关闭重复停止入口，通知后台主流程退出。
        self.stop_button.setEnabled(False)
        self.monitoring_service.stop_requested.set()
        for device_id in self.cards_by_device_id:
            self.update_connection_state(device_id, "停止中", "正在释放相机和后台资源")

    def update_connection_state(self, device_id: str, status: str, reason: str):
        """保存设备连接结果并更新对应卡片。

        Args:
            device_id: 数据库设备编号的字符串形式。
            status: 相机连接状态。
            reason: 连接失败原因或状态说明。

        Returns:
            None  # 已保存状态；当前页面存在该设备时更新卡片
        """
        # 保留状态，并跳过运行期间已从列表移除的设备。
        self.connection_states[device_id] = (status, reason)
        card = self.cards_by_device_id.get(device_id)
        if card is None:
            return

        # 将连接结果转换为卡片文字和颜色，频率及测量进度保持未接入。
        tone = "waiting" if status in ("连接中", "停止中", "连接失败", "监测失败") else "idle"
        card.update_data({
            "title": card.title.text(),
            "tone": tone,
            "status": status,
            "state": "等待启停信号接入" if status == "相机已连接" else status,
            "frequency": "--",
            "completed_steps": 0,
            "events": (("连接", reason),) if reason else (),
        })
        card.setToolTip(reason)

    def finish_monitoring(self):
        """显示最终停止结果并恢复启动入口。

        Args:
            无。

        Returns:
            None  # 卡片与按钮已反映后台退出结果
        """
        # 更新全部卡片，保留具体设备的连接失败原因。
        failure_message = self.monitoring_service.failure_message
        for device_id in self.cards_by_device_id:
            status, reason = self.connection_states.get(device_id, ("未启动", ""))
            if status != "连接失败":
                status = "监测失败" if failure_message else "已停止"
                reason = failure_message or "相机连接已释放"
            self.update_connection_state(device_id, status, reason)

        # 恢复操作按钮，并展示没有对应卡片的初始化故障。
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if failure_message and not self.closing_requested:
            QMessageBox.warning(self, "监测已停止", failure_message)
        self.monitoring_service.deleteLater()
        self.monitoring_service = None

    def fill_demo_logs(self):
        """清空日志表格并按时间顺序填入固定演示日志。

        Args:
            无。

        Returns:
            返回示例：
                None  # 日志表格显示演示日志
        """
        # 清空旧日志，并按时间顺序填入固定日志。
        self.log_table.setRowCount(0)
        for values in LOG_ROWS:
            self.append_log(values)

    def append_log(self, values: tuple):
        """追加一条展示日志并按选项滚动。

        Args:
            values: 按时间、级别、来源、内容排列的四项文字。

        Returns:
            返回示例：
                None  # 追加日志行并更新滚动位置
        """
        # 在表格末尾添加日志字段并设置级别颜色。
        row = self.log_table.rowCount()
        self.log_table.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 1:
                item.setForeground(QColor("#16A653"))
            self.log_table.setItem(row, column, item)

        # 根据自动滚动选项显示最新行。
        if self.auto_scroll.isChecked():
            self.log_table.scrollToBottom()

