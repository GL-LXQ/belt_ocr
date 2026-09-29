"""实时监测页面、机器卡片和步骤进度组件。"""

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    InfoBar,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    SubtitleLabel,
    TitleLabel,
    setCustomStyleSheet,
)

from src.controller.controller import AppController
from ui.belt_animation import BeltAnimationWidget
from ui.theme import COLORS, create_icon


# 本轮处理阶段按界面展示顺序排列。
PROGRESS_STAGE_TITLES = {
    "session_start": "本轮启动",
    "image_capture": "图像采集",
    "frequency_collection": "频率采集",
    "character_recognition": "字符识别",
    "evidence_storage": "证据入库",
}

# 阶段状态转换为界面文字。
PROGRESS_STATUS_TITLES = {
    "running": "进行中",
    "success": "已完成",
    "failed": "失败",
}

# 设置机器卡片、详情卡和列数的布局尺寸。
MACHINE_CARD_MIN_WIDTH = 280
MACHINE_CARD_GAP = 16
MACHINE_DETAIL_WIDTH = 380
MIN_MACHINE_COLUMNS = 2
MAX_MACHINE_COLUMNS = 3


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
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setHorizontalSpacing(0)
        layout.setVerticalSpacing(8)
        self.dots = []
        self.connectors = []
        self.step_labels = []

        # 在五个等宽列中放置圆点和文字，连接线置于圆点下层。
        for index, title in enumerate(PROGRESS_STAGE_TITLES.values()):
            dot = QLabel()
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setFixedSize(22, 22)
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
                connector.setFixedHeight(1)
                # 连接线固定为轨道颜色，不随阶段状态变化。
                connector.setStyleSheet("background: #D9E1EA; border: none;")
                connector.lower()
                self.connectors.append(connector)

        self.update_steps({})

    def update_steps(self, progress_statuses: dict[str, str]):
        """根据各阶段状态更新五个进度节点。

        Args:
            progress_statuses: 以阶段标识为键、running、success 或 failed 为值的状态字典。

        Returns:
            返回示例：
                None  # 更新步骤圆点的颜色、图标和步骤文字
        """
        # 根据每个阶段的实际状态设置圆点颜色和图标。
        stage_names = tuple(PROGRESS_STAGE_TITLES)
        for index, dot in enumerate(self.dots):
            progress_status = progress_statuses.get(stage_names[index])
            color = {
                "running": "#2563EB",
                "success": COLORS["blue"],
                "failed": "#EF4444",
            }.get(progress_status, "#C5CFDA")
            icon_name = "check" if progress_status == "success" else "close"
            icon = create_icon(icon_name, "white").pixmap(QSize(16, 16))
            dot.setPixmap(
                icon if progress_status in ("success", "failed") else QPixmap()
            )
            active = progress_status in ("running", "failed")
            border = {
                "running": "#A6C9FF",
                "failed": "#FFD0D0",
            }.get(progress_status, "white")
            self.step_labels[index].setStyleSheet(
                "color: #34465F; font-weight: 500;" if active else ""
            )
            dot.setStyleSheet(
                f"background: {color}; color: white; "
                f"border: 1px solid {border}; border-radius: 11px;"
            )

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
            connector.setGeometry(left, left_dot.center().y(), right - left, 1)


def format_ocr_result_text(ordered_lines, normalized_lines) -> str:
    """按字符长度分类生成完整 OCR 展示文字。

    Args:
        ordered_lines: 保留原始格式的文字。
        normalized_lines: 逐条对应的去空格文字。

    Returns:
        "20  --\n8  --\n3  --\n2  --"  # 各类别的完整文字
    """
    # 按固定类别收集对应的原始文字。
    grouped_lines = {
        20: [],
        8: [],
        3: [],
        2: [],
    }
    for ordered_line, normalized_line in zip(ordered_lines, normalized_lines):
        character_count = len(normalized_line)
        if character_count in grouped_lines:
            grouped_lines[character_count].append(ordered_line)

    # 为每类首行添加类别标识，后续结果单独换行。
    result_lines = []
    for character_count, category_lines in grouped_lines.items():
        display_lines = category_lines or ["--"]
        result_lines.append(f"{character_count}  {display_lines[0]}")
        result_lines.extend(f"    {line}" for line in display_lines[1:])
    return "\n".join(result_lines)

class MachineCard(SimpleCardWidget):
    """展示一台机器的动画、状态、频率和 OCR 摘要。"""

    clicked = Signal(str)

    def paintEvent(self, event):
        """绘制 QSS 定义的卡片背景和边框。

        Args:
            event: 绘制事件。

        Returns:
            None  # 卡片表面已绘制
        """
        QFrame.paintEvent(self, event)

    def __init__(self, data: dict):
        """构建机器卡片并填入展示数据。

        Args:
            data: 卡片展示数据，包含标题、状态、频率和进度。

        Returns:
            返回示例：
                None  # 创建机器卡片
        """
        super().__init__()
        self.setObjectName("machineCard")
        self.machine_id = data["machine_id"]
        self.set_selected(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.progress_session_id = ""
        self.progress_statuses = {}
        self.setMinimumWidth(MACHINE_CARD_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(9)

        # 创建机器标题和状态徽标。
        heading = QHBoxLayout()
        self.title = SubtitleLabel()
        self.title.setObjectName("machineTitle")
        self.title.setWordWrap(True)
        self.badge = QLabel()
        self.badge.setObjectName("machineBadge")
        heading.addWidget(self.title)
        heading.addStretch()
        heading.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)


        # 显示相机序列号。
        self.camera_label = CaptionLabel(f"Camera · {data['camera_serial']}")
        layout.addWidget(self.camera_label)

        # 在卡片画面区域显示皮带机动画。
        self.belt_animation = BeltAnimationWidget()
        layout.addWidget(self.belt_animation)


        # 在同一行显示当前状态与实时频率。
        metrics = QHBoxLayout()
        metrics.setSpacing(16)
        state_panel = QVBoxLayout()
        state_panel.setSpacing(4)
        state_panel.addWidget(CaptionLabel("当前状态"))
        self.state_label = BodyLabel()
        self.state_label.setWordWrap(True)
        state_panel.addWidget(self.state_label)
        frequency_panel = QVBoxLayout()
        frequency_panel.setSpacing(4)
        frequency_panel.addWidget(CaptionLabel("实时频率"))
        self.frequency_label = BodyLabel()
        self.frequency_label.setObjectName("frequencyValue")
        frequency_panel.addWidget(self.frequency_label)
        metrics.addLayout(state_panel, 2)
        metrics.addLayout(frequency_panel, 1)
        layout.addLayout(metrics)


        # 在浅色信息块中显示单行 OCR 摘要。
        summary_panel = QFrame()
        summary_panel.setObjectName("ocrSummaryPanel")
        summary_layout = QVBoxLayout(summary_panel)
        summary_layout.setContentsMargins(10, 6, 10, 6)
        summary_layout.addWidget(CaptionLabel("OCR 摘要"))
        self.ocr_result_label = QLabel("--")
        self.ocr_result_label.setObjectName("ocrResult")
        self.ocr_result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.ocr_result_label.setFixedHeight(20)
        self.ocr_result_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.ocr_result_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        summary_layout.addWidget(self.ocr_result_label)
        layout.addWidget(summary_panel)
        self.clear_ocr_result()



        # 弱化相机信息和字段标题。
        for label in self.findChildren(CaptionLabel):
            label.setStyleSheet(f"color: {COLORS['muted']};")

        self.update_data(data)

    def set_selected(self, selected: bool):
        """刷新卡片选中边框。

        Args:
            selected: 是否选中当前卡片。

        Returns:
            None  # 选中属性和样式已刷新
        """
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mouseReleaseEvent(self, event):
        """点击卡片时发送机器编号。

        Args:
            event: 鼠标释放事件。

        Returns:
            None  # 左键点击发送选中信号
        """
        QFrame.mouseReleaseEvent(self, event)
        self.isPressed = False
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.machine_id)

    def update_data(self, data: dict):
        """将机器展示数据应用到已有控件。

        Args:
            data: 包含标题、状态、频率和进度的卡片展示数据。

        Returns:
            返回示例：
                None  # 更新机器卡片，不创建新控件
        """
        # 更新机器标题和状态文案。
        self.setProperty("tone", data["tone"])
        self.title.setText(data["title"])
        self.badge.setText(data["status"])
        self.state_label.setText(data["state"])
        self.frequency_label.setText(data["frequency"])

        # 刷新小面积状态徽标。
        self.badge.setProperty("tone", data["tone"])
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self.update()

    def set_frequency(self, frequency: float | None) -> None:
        """更新卡片实时频率文字。

        Args:
            frequency: 实际频率，None 表示暂时没有频率数据。

        Returns:
            返回示例：
                None  # 频率标签显示 --、0.0 Hz 或实际频率
        """
        frequency_text = "--" if frequency is None else f"{frequency:.1f} Hz"
        self.frequency_label.setText(frequency_text)

    def set_ocr_result(
        self, ordered_lines: tuple[str, ...], normalized_lines: tuple[str, ...]
    ) -> None:
        """显示第一条 OCR 文字摘要。

        Args:
            ordered_lines: 保留原始格式的最终文字。
            normalized_lines: 与原始文字逐条对应的去空格文字。

        Returns:
            返回示例：
                None  # 仅显示第一条结果或占位文字
        """
        self.ocr_result_label.setText(ordered_lines[0] if ordered_lines else "--")

    def clear_ocr_result(self) -> None:
        """将 OCR 摘要恢复为占位文字。

        Args:
            无。

        Returns:
            返回示例：
                None  # 摘要显示 --
        """
        self.ocr_result_label.setText("--")


class SummaryCard(SimpleCardWidget):
    """显示一项内存状态统计。"""

    def paintEvent(self, event):
        """绘制 QSS 定义的卡片背景和边框。

        Args:
            event: 绘制事件。

        Returns:
            None  # 卡片表面已绘制
        """
        QFrame.paintEvent(self, event)

    def __init__(self, title: str, description: str):
        """创建统计标题、数字和说明。

        Args:
            title: 统计标题。
            description: 统计说明。

        Returns:
            None  # 创建统计卡片
        """
        super().__init__()
        self.setObjectName("summaryCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(4)

        # 按顺序显示统计标题、数值和说明。
        layout.addWidget(CaptionLabel(title))
        self.value_label = QLabel("0")
        self.value_label.setObjectName("summaryValue")
        layout.addWidget(self.value_label)
        layout.addWidget(CaptionLabel(description))

        # 弱化统计标题和说明。
        for label in self.findChildren(CaptionLabel):
            label.setStyleSheet(f"color: {COLORS['muted']};")


class MachineDetailPanel(SimpleCardWidget):
    """显示选中机器的完整结果和进度。"""

    def paintEvent(self, event):
        """绘制 QSS 定义的卡片背景和边框。

        Args:
            event: 绘制事件。

        Returns:
            None  # 卡片表面已绘制
        """
        QFrame.paintEvent(self, event)

    def __init__(self):
        """创建固定宽度的机器详情区。

        Args:
            无。

        Returns:
            None  # 创建详情控件
        """
        super().__init__()
        self.setObjectName("machineDetailPanel")
        self.setFixedWidth(MACHINE_DETAIL_WIDTH)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(0)
        section_label = CaptionLabel("机器详情")
        section_label.setObjectName("detailSectionLabel")
        layout.addWidget(section_label)

        # 显示机器名称和状态徽标。
        layout.addSpacing(4)
        heading = QHBoxLayout()
        self.title = SubtitleLabel("未选择机器")
        self.title.setObjectName("detailMachineName")
        self.title.setWordWrap(True)
        self.badge = QLabel()
        self.badge.setObjectName("machineBadge")
        heading.addWidget(self.title, 1)
        heading.addWidget(self.badge)
        layout.addLayout(heading)

        # 在两列网格中展示设备序列号和当前状态。
        layout.addSpacing(10)
        self.attributes_layout = QGridLayout()
        self.attributes_layout.setContentsMargins(0, 0, 0, 0)
        self.attributes_layout.setHorizontalSpacing(24)
        self.attributes_layout.setVerticalSpacing(3)
        for column_index, title in enumerate(("相机序列号", "频率仪序列号")):
            label = CaptionLabel(title)
            label.setObjectName("detailMetaKey")
            self.attributes_layout.addWidget(label, 0, column_index)
        self.camera_serial_label = BodyLabel("--")
        self.camera_serial_label.setObjectName("detailMetaValue")
        self.camera_serial_label.setWordWrap(True)
        self.frequency_meter_serial_label = BodyLabel("--")
        self.frequency_meter_serial_label.setObjectName("detailMetaValue")
        self.frequency_meter_serial_label.setWordWrap(True)
        self.attributes_layout.addWidget(self.camera_serial_label, 1, 0)
        self.attributes_layout.addWidget(self.frequency_meter_serial_label, 1, 1)
        self.attributes_layout.setRowMinimumHeight(2, 7)
        for column_index, title in enumerate(("当前状态", "相机状态")):
            label = CaptionLabel(title)
            label.setObjectName("detailMetaKey")
            self.attributes_layout.addWidget(label, 3, column_index)
        self.state_label = BodyLabel("--")
        self.state_label.setObjectName("detailMetaValue")
        self.state_label.setWordWrap(True)
        self.attributes_layout.addWidget(self.state_label, 4, 0)
        self.camera_state_label = BodyLabel("--")
        self.camera_state_label.setObjectName("detailMetaValue")
        self.camera_state_label.setWordWrap(True)
        self.attributes_layout.addWidget(self.camera_state_label, 4, 1)
        self.attributes_layout.setColumnStretch(0, 1)
        self.attributes_layout.setColumnStretch(1, 1)
        layout.addLayout(self.attributes_layout)

        # 在浅灰分区卡中突出当前实时频率。
        layout.addSpacing(10)
        frequency_card = QFrame()
        frequency_card.setObjectName("detailFrequencyCard")
        frequency_layout = QVBoxLayout(frequency_card)
        frequency_layout.setContentsMargins(14, 8, 14, 8)
        frequency_layout.setSpacing(4)

        # 显示蓝色图标和频率标题。
        frequency_heading = QHBoxLayout()
        frequency_heading.setSpacing(6)
        frequency_icon = QLabel()
        frequency_icon.setPixmap(
            FluentIcon.SPEED_HIGH.icon(color=QColor(COLORS["blue"])).pixmap(16, 16)
        )
        frequency_heading.addWidget(frequency_icon)
        frequency_title = CaptionLabel("实时频率")
        frequency_title.setObjectName("detailCardTitle")
        frequency_heading.addWidget(frequency_title)
        frequency_heading.addStretch()
        frequency_layout.addLayout(frequency_heading)

        # 在标题下方显示当前频率值。
        self.frequency_label = QLabel("--")
        self.frequency_label.setObjectName("detailFrequencyValue")
        frequency_layout.addWidget(self.frequency_label)
        layout.addWidget(frequency_card)

        # 在独立的 OCR 分区卡中保留可复制的完整分类文字。
        layout.addSpacing(8)
        ocr_card = QFrame()
        ocr_card.setObjectName("detailOcrCard")
        ocr_card_layout = QVBoxLayout(ocr_card)
        ocr_card_layout.setContentsMargins(12, 8, 12, 8)
        ocr_card_layout.setSpacing(6)

        # 显示蓝色图标和 OCR 标题。
        ocr_heading = QHBoxLayout()
        ocr_heading.setSpacing(6)
        ocr_icon = QLabel()
        ocr_icon.setPixmap(
            FluentIcon.DOCUMENT.icon(color=QColor(COLORS["blue"])).pixmap(16, 16)
        )
        ocr_heading.addWidget(ocr_icon)
        ocr_title = CaptionLabel("OCR 识别结果")
        ocr_title.setObjectName("detailCardTitle")
        ocr_heading.addWidget(ocr_title)
        ocr_heading.addStretch()
        ocr_card_layout.addLayout(ocr_heading)

        # 在浅灰区域中显示可复制的完整 OCR 文本。
        ocr_panel = QFrame()
        ocr_panel.setObjectName("detailOcrPanel")
        ocr_layout = QVBoxLayout(ocr_panel)
        ocr_layout.setContentsMargins(12, 6, 12, 6)
        self.ocr_text = PlainTextEdit()
        self.ocr_text.setObjectName("detailOcrText")
        self.ocr_text.setReadOnly(True)
        self.ocr_text.setFrameShape(QFrame.Shape.NoFrame)
        self.ocr_text.setFixedHeight(110)
        ocr_style = (
            "PlainTextEdit {"
            f"background: transparent; color: {COLORS['text']};"
            "border: none; padding: 0; font-size: 13px;}"
        )
        setCustomStyleSheet(self.ocr_text, ocr_style, ocr_style)
        ocr_layout.addWidget(self.ocr_text)
        ocr_card_layout.addWidget(ocr_panel)
        layout.addWidget(ocr_card)

        # 在蓝色图标标题下显示本轮处理步骤。
        layout.addSpacing(10)
        progress_heading = QHBoxLayout()
        progress_heading.setSpacing(6)
        progress_icon = QLabel()
        progress_icon.setPixmap(
            FluentIcon.CHECKBOX.icon(color=QColor(COLORS["blue"])).pixmap(16, 16)
        )
        progress_heading.addWidget(progress_icon)
        progress_title = CaptionLabel("本轮处理")
        progress_title.setObjectName("detailSectionLabel")
        progress_heading.addWidget(progress_title)
        progress_heading.addStretch()
        layout.addLayout(progress_heading)
        layout.addSpacing(4)
        self.steps = StepProgress()
        layout.addWidget(self.steps)

        # 在详情底部并排显示三项固定占位统计。
        layout.addSpacing(10)
        stats_bar = QFrame()
        stats_bar.setObjectName("detailStatsBar")
        stats_layout = QHBoxLayout(stats_bar)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(0)
        self.stat_values = {}
        for index, (title, icon) in enumerate((
            ("运行时长", FluentIcon.DATE_TIME),
            ("今日识别数量", FluentIcon.DOCUMENT),
            ("今日待复核数量", FluentIcon.INFO),
        )):
            # 在相邻统计项之间显示竖向分隔线。
            if index:
                divider = QFrame()
                divider.setObjectName("detailStatDivider")
                divider.setFixedSize(1, 38)
                stats_layout.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)

            # 将图标和标题放在统计项首行。
            stat_item = QFrame()
            stat_item.setObjectName("detailStatItem")
            stat_layout = QVBoxLayout(stat_item)
            stat_layout.setContentsMargins(2, 0, 2, 0)
            stat_layout.setSpacing(4)
            stat_heading = QHBoxLayout()
            stat_heading.setSpacing(3)
            stat_icon = QLabel()
            stat_icon.setPixmap(icon.icon(color=QColor(COLORS["blue"])).pixmap(12, 12))
            stat_heading.addWidget(stat_icon)
            stat_title = QLabel(title)
            stat_title.setObjectName("detailStatTitle")
            stat_heading.addWidget(stat_title)
            stat_heading.addStretch()
            stat_layout.addLayout(stat_heading)

            # 在标题下方保存固定占位值。
            stat_value = QLabel("--")
            stat_value.setObjectName("detailStatValue")
            stat_layout.addWidget(stat_value)
            stats_layout.addWidget(stat_item, 1)
            self.stat_values[title] = stat_value
        layout.addWidget(stats_bar)

        # 设置详情文字的辅助色和数值字重。
        for label in self.findChildren(CaptionLabel):
            label.setStyleSheet(
                f"color: {COLORS['muted']}; font-size: 12px; font-weight: 500;"
            )
        self.title.setStyleSheet(
            f"color: {COLORS['text']}; font-size: 18px; font-weight: 600;"
        )
        for label in self.findChildren(BodyLabel):
            label.setStyleSheet(
                f"color: {COLORS['text']}; font-size: 14px; font-weight: 500;"
            )


class RealtimePage(QWidget):
    """组织机器卡片和监测状态展示。"""

    def __init__(self, controller: AppController, parent: QWidget | None = None):
        """初始化布局、机器卡片和本地交互。

        Args:
            controller: 界面业务控制器。
            parent: 所属主窗口。

        Returns:
            返回示例：
                None  # 创建实时监测页面，子控件随页面释放
        """
        super().__init__(parent)
        self.setObjectName("realtime")
        # 绘制实时页的统一背景。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.controller = controller
        self.closing_requested = False
        self.connection_states = {}
        self.ocr_results_by_machine_id = {}
        self.measurement_states_by_machine_id = {}
        self.selected_machine_id: str | None = None
        self.cards_by_machine_id = {}
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(24, 16, 24, 20)
        outer_layout.setSpacing(12)

        # 固定页面标题和监测操作区。
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(TitleLabel("实时监测"))
        titles.addWidget(BodyLabel("查看当前机器的检测状态"))
        header.addLayout(titles)
        header.addStretch()
        self.start_button = PrimaryPushButton(FluentIcon.PLAY, "启动监测")
        self.stop_button = PushButton(FluentIcon.PAUSE, "停止监测")
        self.refresh_button = PushButton(FluentIcon.SYNC, "刷新")
        for button in (self.start_button, self.stop_button, self.refresh_button):
            header.addWidget(button)
        self.stop_button.setEnabled(False)
        outer_layout.addLayout(header)

        # 显示四项内存状态总览。
        summary_layout = QHBoxLayout()
        summary_layout.setSpacing(16)
        self.summary_cards = []
        for title, description in (
            ("机器总数", "当前启用机器"),
            ("相机已连接", "相机连接正常"),
            ("测量中", "当前测量周期"),
            ("故障 / 失败", "连接或本轮测量异常"),
        ):
            summary_card = SummaryCard(title, description)
            self.summary_cards.append(summary_card)
            summary_layout.addWidget(summary_card, 1)
        outer_layout.addLayout(summary_layout)

        # 在总览卡片与机器列表分区之间保留明显的区块间距。
        outer_layout.addSpacing(10)

        # 显示机器列表分区标题和说明。
        section_layout = QVBoxLayout()
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(3)
        section_title = QLabel("我的机器")
        section_title.setObjectName("machineSectionTitle")

        # 设置机器列表分区的辅助说明。
        section_description = QLabel("实时查看各检测机器的连接、测量、频率与识别状态")
        section_description.setObjectName("machineSectionDescription")
        section_layout.addWidget(section_title)
        section_layout.addWidget(section_description)
        outer_layout.addLayout(section_layout)
        body_layout = QHBoxLayout()
        body_layout.setSpacing(MACHINE_CARD_GAP)
        outer_layout.addLayout(body_layout, 1)

        # 将机器卡片放入可滚动区域。
        self.scroll_area = ScrollArea()
        self.scroll_area.setObjectName("machineScrollArea")
        self.scroll_area.viewport().setObjectName("machineScrollViewport")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # 设置机器列表的浅灰滑块和透明轨道。
        vertical_scroll_bar = self.scroll_area.scrollDelagate.vScrollBar
        vertical_scroll_bar.setHandleColor("#D5DAE1", "#D5DAE1")
        vertical_scroll_bar.setGrooveColor("transparent", "transparent")
        vertical_scroll_bar.setArrowColor("transparent", "transparent")
        body_layout.addWidget(self.scroll_area, 1)

        # 详情内容较高时仅在右侧区域内部滚动。
        self.detail_scroll_area = ScrollArea()
        self.detail_scroll_area.setObjectName("detailScrollArea")
        self.detail_scroll_area.viewport().setObjectName("detailScrollViewport")
        self.detail_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll_area.setWidgetResizable(False)
        self.detail_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.detail_scroll_area.setFixedWidth(MACHINE_DETAIL_WIDTH)
        detail_scroll_bar = self.detail_scroll_area.scrollDelagate.vScrollBar
        detail_scroll_bar.setHandleColor("#D5DAE1", "#D5DAE1")
        detail_scroll_bar.setGrooveColor("transparent", "transparent")
        detail_scroll_bar.setArrowColor("transparent", "transparent")
        self.detail_panel = MachineDetailPanel()
        self.detail_scroll_area.setWidget(self.detail_panel)
        body_layout.addWidget(self.detail_scroll_area, 0)

        content = QWidget()
        content.setObjectName("monitorContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(MACHINE_CARD_GAP)
        self.scroll_area.setWidget(content)

        # 网格靠上排列，空余高度留在底部。
        cards_layout = QGridLayout()
        cards_layout.setHorizontalSpacing(MACHINE_CARD_GAP)
        cards_layout.setVerticalSpacing(MACHINE_CARD_GAP)
        cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards_layout = cards_layout
        self.machine_cards = []
        self.empty_hint = None
        self.card_column_count = 0
        layout.addLayout(cards_layout)
        layout.addStretch(1)
        self.scroll_area.viewport().installEventFilter(self)

        # 绑定页面操作按钮。
        self.start_button.clicked.connect(self.start_monitoring)
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.refresh_button.clicked.connect(self.reload_machines)

        # 连接 Controller 的实时状态通知。
        self.controller.camera_state_changed_signal.connect(
            self.update_connection_state
        )
        self.controller.measurement_progress_changed_signal.connect(
            self.update_measurement_progress
        )
        self.controller.cycle_closed_signal.connect(self.update_cycle_closed)
        self.controller.ocr_result_changed_signal.connect(self.update_ocr_result)
        self.controller.monitoring_finished_signal.connect(self.finish_monitoring)

        # 读取机器并建立卡片。
        self.reload_machines()

    def reload_machines(self):
        """重新读取机器表并按记录重建卡片。

        Args:
            无。

        Returns:
            返回示例：
                None  # 卡片跟随数据库内容，读取失败时提示并清空卡片区
        """
        # 读取已启用机器，失败时提示并把卡片区置空。
        result = self.controller.list_enabled_machines()
        if result.success:
            self.machines = result.data["machines"]
        else:
            InfoBar.error("机器读取失败", result.message, duration=-1, parent=self)
            self.machines = []
        self.populate_cards()
        # 按当前滚动区宽度排列卡片。
        self.reflow_cards()
        self.scroll_area.widget().updateGeometry()

    def populate_cards(self):
        """按当前机器记录重建机器卡片区。

        Args:
            无。

        Returns:
            返回示例：
                None  # 卡片已重建并恢复已缓存的显示状态
        """
        # 移除上一次创建的卡片。
        while self.cards_layout.count():
            widget = self.cards_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.machine_cards = []
        self.cards_by_machine_id = {}
        self.empty_hint = None
        self.card_column_count = 0

        # 没有机器时显示横跨整行的空态提示。
        if not self.machines:
            hint = QLabel("暂无机器，请先在机器管理页添加。")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #73849B;")
            self.empty_hint = hint
            self.cards_layout.addWidget(hint, 0, 0)
            self.selected_machine_id = None
            self.refresh_selected_machine_detail()
            self.update_dashboard_summary()
            return

        # 按机器编号创建卡片，并恢复最近一次连接结果。
        for machine in self.machines:
            card = MachineCard({
                "machine_id": str(machine["id"]),
                "camera_serial": machine["camera_serial"],
                "title": machine["machine_name"],
                "tone": "idle",
                "status": "未启动",
                "state": "未启动监测",
                "frequency": "--",
            })
            card.clicked.connect(self.select_machine)
            self.machine_cards.append(card)
            machine_id = str(machine["id"])
            self.cards_by_machine_id[machine_id] = card

            # 恢复当前周期身份和已缓存的文字。
            cached_result = self.ocr_results_by_machine_id.get(machine_id)
            if cached_result is not None:
                session_id, ordered_lines, normalized_lines = cached_result
                card.progress_session_id = session_id
                card.set_ocr_result(ordered_lines, normalized_lines)

            # 恢复当前周期的进度节点。
            measurement_state = self.measurement_states_by_machine_id.get(machine_id)
            if measurement_state is not None:
                card.progress_session_id = measurement_state["session_id"]
                card.progress_statuses = measurement_state["progress_statuses"].copy()

                # 运行中的周期恢复皮带和仍在执行的子动画。
                if measurement_state["machine_running"]:
                    card.belt_animation.start_machine()

                    # 没有失败进度时恢复仍在执行的子动画。
                    progress_statuses = card.progress_statuses
                    if "failed" not in progress_statuses.values():
                        if progress_statuses.get("image_capture") == "running":
                            card.belt_animation.start_capture()
                        if progress_statuses.get("frequency_collection") == "running":
                            card.belt_animation.set_frequency_listening(True)

            status, reason = self.connection_states.get(machine_id, ("未启动", ""))
            self.update_connection_state(machine_id, status, reason)

        # 恢复仍存在的选择，否则选中首台机器。
        selected_machine_id = self.selected_machine_id
        if selected_machine_id not in self.cards_by_machine_id:
            selected_machine_id = str(self.machines[0]["id"])
        self.select_machine(selected_machine_id)
        self.update_dashboard_summary()

    def update_dashboard_summary(self):
        """根据已有内存状态刷新总览数字。

        Args:
            无。

        Returns:
            None  # 四项统计数字已刷新
        """
        connected_count = sum(
            status == "相机已连接" for status, reason in self.connection_states.values()
        )
        running_count = sum(
            state["machine_running"]
            for state in self.measurement_states_by_machine_id.values()
        )

        # 合并连接故障和本轮失败的机器编号。
        failed_machine_ids = {
            machine_id
            for machine_id, (status, reason) in self.connection_states.items()
            if status in ("连接失败", "相机故障", "监测失败")
        }
        failed_machine_ids.update(
            machine_id
            for machine_id, state in self.measurement_states_by_machine_id.items()
            if "failed" in state["progress_statuses"].values()
        )
        values = (
            len(self.machines),
            connected_count,
            running_count,
            len(failed_machine_ids),
        )
        for summary_card, value in zip(self.summary_cards, values):
            summary_card.value_label.setText(str(value))

    def select_machine(self, machine_id: str):
        """选中指定机器并刷新详情。

        Args:
            machine_id: 当前卡片的机器编号。

        Returns:
            None  # 仅指定卡片选中，详情已同步
        """
        self.selected_machine_id = machine_id
        for current_machine_id, card in self.cards_by_machine_id.items():
            card.set_selected(current_machine_id == machine_id)
        self.refresh_selected_machine_detail()

    def refresh_selected_machine_detail(self):
        """将选中机器的已有展示状态映射到详情栏。

        Args:
            无。

        Returns:
            None  # 详情文字和步骤已刷新，无机器时显示占位
        """
        panel = self.detail_panel
        card = self.cards_by_machine_id.get(self.selected_machine_id)
        machine = next(
            (
                machine
                for machine in self.machines
                if str(machine["id"]) == self.selected_machine_id
            ),
            None,
        )
        panel.title.setText(card.title.text() if card else "未选择机器")
        panel.camera_serial_label.setText(machine["camera_serial"] if machine else "--")
        panel.frequency_meter_serial_label.setText(
            machine["frequency_meter_serial"] if machine else "--"
        )
        panel.state_label.setText(card.state_label.text() if card else "--")
        camera_status = self.connection_states.get(
            self.selected_machine_id, ("未启动", "")
        )[0]
        panel.camera_state_label.setText(camera_status if card else "--")
        panel.frequency_label.setText(card.frequency_label.text() if card else "--")

        # 同步主状态徽标和故障说明。
        panel.badge.setText(card.badge.text() if card else "未选择")
        panel.badge.setProperty("tone", card.property("tone") if card else "idle")
        panel.badge.style().unpolish(panel.badge)
        panel.badge.style().polish(panel.badge)
        panel.setToolTip(card.toolTip() if card else "")

        # 按当前卡片状态设置详情值的文字颜色。
        state_tone = card.property("tone") if card else "idle"
        state_color = {
            "running": "#138B3F",
            "waiting": "#A76200",
            "error": "#B42318",
        }.get(state_tone, COLORS["text"])
        panel.state_label.setStyleSheet(
            f"color: {state_color}; font-size: 14px; font-weight: 500;"
        )

        # 从已有缓存恢复全文和步骤。
        cached_result = self.ocr_results_by_machine_id.get(self.selected_machine_id)
        result_text = format_ocr_result_text(
            cached_result[1] if cached_result else (),
            cached_result[2] if cached_result else (),
        )
        if panel.ocr_text.toPlainText() != result_text:
            panel.ocr_text.setPlainText(result_text)
        measurement_state = self.measurement_states_by_machine_id.get(
            self.selected_machine_id
        )
        panel.steps.update_steps(
            measurement_state["progress_statuses"] if measurement_state else {}
        )

    def reflow_cards(self):
        """按机器列表宽度将已有卡片排列为两列或三列。

        Args:
            无。

        Returns:
            None  # 卡片实例已按两列或三列排列
        """
        # 用列表可视宽度判断能否容纳三列最小宽度的机器卡片。
        available_width = self.scroll_area.viewport().width()
        three_column_min_width = (
            MACHINE_CARD_MIN_WIDTH * MAX_MACHINE_COLUMNS
            + MACHINE_CARD_GAP * (MAX_MACHINE_COLUMNS - 1)
        )
        column_count = (
            MAX_MACHINE_COLUMNS if available_width >= three_column_min_width
            else MIN_MACHINE_COLUMNS
        )
        if not self.machine_cards:
            self.card_column_count = column_count
            return

        if column_count == self.card_column_count:
            return

        # 清除旧位置和列宽，再复用原有卡片。
        while self.cards_layout.count():
            self.cards_layout.takeAt(0)
        for column_index in range(MAX_MACHINE_COLUMNS):
            self.cards_layout.setColumnStretch(column_index, 0)
            self.cards_layout.setColumnMinimumWidth(column_index, 0)
        for column_index in range(column_count):
            self.cards_layout.setColumnStretch(column_index, 1)
        for card_index, card in enumerate(self.machine_cards):
            self.cards_layout.addWidget(
                card,
                card_index // column_count,
                card_index % column_count,
            )
        self.card_column_count = column_count
        self.scroll_area.widget().updateGeometry()

    def eventFilter(self, watched, event):
        """在机器列表可视宽度变化时重新排列已有卡片。

        Args:
            watched: 接收事件的控件。
            event: Qt 事件。

        Returns:
            bool  # 事件交给父类继续处理
        """
        if (
            watched is self.scroll_area.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self.reflow_cards()
        return super().eventFilter(watched, event)

    def start_monitoring(self):
        """读取机器卡片并启动一次后台监测。

        Args:
            无。

        Returns:
            None  # 后台启动，按钮等待监测结束后恢复
        """
        # 请求启动监测并显示启动失败提示。
        result = self.controller.start_monitoring()
        if not result.success:
            InfoBar.error("启动失败", result.message, duration=-1, parent=self)
            return

        # 重读机器清单并切换按钮状态。
        self.connection_states.clear()
        self.reload_machines()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        # 显示各机器正在连接。
        for machine_id in self.cards_by_machine_id:
            self.update_connection_state(machine_id, "连接中", "正在初始化监测服务")

    def stop_monitoring(self):
        """通知后台停止并等待后台自行完成资源释放。

        Args:
            无。

        Returns:
            None  # 停止通知已发送，界面继续处理事件
        """
        # 关闭重复停止入口，通知后台主流程退出。
        self.stop_button.setEnabled(False)
        self.controller.stop_monitoring()
        for machine_id in self.cards_by_machine_id:
            self.update_connection_state(machine_id, "停止中", "正在释放相机和后台资源")

    def update_connection_state(self, machine_id: str, status: str, reason: str):
        """保存机器连接结果并更新对应卡片。

        Args:
            machine_id: 数据库机器编号的字符串形式。
            status: 相机连接状态。
            reason: 连接失败原因或状态说明。

        Returns:
            None  # 已保存状态；当前页面存在该机器时更新卡片
        """
        # 保留状态，并跳过运行期间已从列表移除的机器。
        self.connection_states[machine_id] = (status, reason)
        self.update_dashboard_summary()
        card = self.cards_by_machine_id.get(machine_id)
        if card is None:
            return

        # 将连接结果转换为卡片文字和颜色。
        tone = "idle"
        if status in ("连接中", "停止中"):
            tone = "waiting"
        elif status in ("连接失败", "相机故障", "监测失败", "测量失败"):
            tone = "error"
        card.update_data({
            "title": card.title.text(),
            "tone": tone,
            "status": status,
            "state": "等待启停信号接入" if status == "相机已连接" else status,
            "frequency": "--",
        })
        card.setToolTip(reason)
        if machine_id == self.selected_machine_id:
            self.refresh_selected_machine_detail()

    def update_measurement_progress(
        self, machine_id: str, session_id: str, stage: str, status: str
    ):
        """将后台发送的本轮阶段状态更新到对应机器卡片。

        Args:
            machine_id: 数据库机器编号的字符串形式。
            session_id: 本轮测量周期编号。
            stage: 本次更新的处理阶段标识。
            status: 本次更新的 running、success 或 failed 状态。

        Returns:
            返回示例：
                None  # 当前周期的进度和动画状态已更新
        """
        # 正式受理新周期时切换页面缓存并清空上一轮文字。
        measurement_state = self.measurement_states_by_machine_id.get(machine_id)
        is_session_start = stage == "session_start" and status == "success"
        is_new_session = is_session_start and (
            measurement_state is None or measurement_state["session_id"] != session_id
        )
        if is_new_session:
            measurement_state = {
                "session_id": session_id,
                "progress_statuses": {},
                "machine_running": True,
            }
            self.measurement_states_by_machine_id[machine_id] = measurement_state
            self.ocr_results_by_machine_id[machine_id] = (session_id, (), ())
        elif measurement_state is None or measurement_state["session_id"] != session_id:
            return

        # 保存本轮进度，供卡片刷新时恢复。
        progress_statuses = measurement_state["progress_statuses"]
        progress_statuses[stage] = status
        progress_failed = "failed" in progress_statuses.values()
        self.update_dashboard_summary()

        # 找到对应机器的卡片。
        card = self.cards_by_machine_id.get(machine_id)
        if card is None:
            return

        # 新周期清空上一轮的进度和 OCR 展示。
        if is_new_session:
            card.progress_session_id = session_id
            card.progress_statuses = {}
            card.clear_ocr_result()
        card.progress_statuses = progress_statuses.copy()

        # 按当前周期的启动和运行进度开启动画。
        if is_new_session:
            card.belt_animation.start_machine()
        elif measurement_state["machine_running"] and not progress_failed:
            if stage == "image_capture" and status == "running":
                card.belt_animation.start_capture()
            elif stage == "frequency_collection" and status == "running":
                card.belt_animation.set_frequency_listening(True)

        # 失败进度结束本轮仍在运行的子动画。
        if status == "failed":
            card.belt_animation.stop_capture()
            card.belt_animation.set_frequency_listening(False)

        # 成功结算后关闭对应子动画。
        elif stage == "image_capture" and status == "success":
            card.belt_animation.stop_capture()
        elif stage == "frequency_collection" and status == "success":
            card.belt_animation.set_frequency_listening(False)

        # 相机故障时保留卡片主状态和故障原因。
        if self.connection_states.get(machine_id, ("", ""))[0] == "相机故障":
            if machine_id == self.selected_machine_id:
                self.refresh_selected_machine_detail()
            return

        # 更新卡片当前测量状态。
        stage_title = PROGRESS_STAGE_TITLES[stage]
        status_title = PROGRESS_STATUS_TITLES[status]
        card.update_data({
            "title": card.title.text(),
            "tone": "error" if status == "failed" else "running",
            "status": "测量失败" if status == "failed" else "测量中",
            "state": f"{stage_title}{status_title}",
            "frequency": card.frequency_label.text(),
        })

        if machine_id == self.selected_machine_id:
            self.refresh_selected_machine_detail()

    def update_cycle_closed(self, machine_id: str, session_id: str) -> None:
        """关闭对应机器当前周期的皮带动画。

        Args:
            machine_id: 机器编号。
            session_id: 进入关闭处理的周期编号。

        Returns:
            返回示例：
                None  # 当前周期动画已停止，文字结果保持不变
        """
        # 旧周期的关闭通知不影响新周期。
        measurement_state = self.measurement_states_by_machine_id.get(machine_id)
        if measurement_state is None or measurement_state["session_id"] != session_id:
            return

        # 将当前周期标记为停止。
        measurement_state["machine_running"] = False
        self.update_dashboard_summary()
        if machine_id == self.selected_machine_id:
            self.refresh_selected_machine_detail()

        # 收起当前卡片的皮带。
        card = self.cards_by_machine_id.get(machine_id)
        if card is not None:
            card.belt_animation.stop_machine()

    def update_ocr_result(
        self,
        machine_id: str,
        session_id: str,
        ordered_lines: tuple[str, ...],
        normalized_lines: tuple[str, ...],
    ) -> None:
        """保存当前周期的最终文字并更新对应卡片。

        Args:
            machine_id: 机器编号。
            session_id: 文字所属周期编号。
            ordered_lines: 保留原始格式的最终文字。
            normalized_lines: 与原始文字对应的去空格文字。

        Returns:
            返回示例：
                None  # 当前周期文字已保存并显示，旧周期文字被忽略
        """
        # 只接收已正式启动的当前周期文字。
        cached_result = self.ocr_results_by_machine_id.get(machine_id)
        if cached_result is None or cached_result[0] != session_id:
            return

        # 保留轻量文字缓存并更新仍在页面中的卡片。
        self.ocr_results_by_machine_id[machine_id] = (
            session_id, ordered_lines, normalized_lines
        )
        card = self.cards_by_machine_id.get(machine_id)
        if card is not None:
            card.set_ocr_result(ordered_lines, normalized_lines)

        if machine_id == self.selected_machine_id:
            self.refresh_selected_machine_detail()

    def finish_monitoring(self, failure_message: str):
        """显示最终停止结果并恢复启动入口。

        Args:
            failure_message: 后台监测结束时的故障提示。

        Returns:
            返回示例：
                None  # 全部卡片动画停止，操作按钮已恢复
        """
        # 标记全部周期停止，避免刷新后恢复子动画。
        for measurement_state in self.measurement_states_by_machine_id.values():
            measurement_state["machine_running"] = False

        # 更新全部卡片，保留具体机器的连接失败原因。
        for machine_id in self.cards_by_machine_id:
            # 停止当前机器的动画。
            self.cards_by_machine_id[machine_id].belt_animation.stop_machine()

            # 更新当前机器的连接结果。
            status, reason = self.connection_states.get(machine_id, ("未启动", ""))
            if status != "连接失败":
                status = "监测失败" if failure_message else "已停止"
                reason = failure_message or "相机连接已释放"
            self.update_connection_state(machine_id, status, reason)

        self.update_dashboard_summary()
        self.refresh_selected_machine_detail()

        # 恢复操作按钮，并展示没有对应卡片的初始化故障。
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if failure_message and not self.closing_requested:
            InfoBar.error("监测已停止", failure_message, duration=-1, parent=self)

