"""实时监测页面、机器卡片和步骤进度组件。"""

import logging

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
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


logger = logging.getLogger(__name__)


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

# 后端机器整体状态转换为界面文字。
MACHINE_STATUS_TITLES = {
    "offline": "离线",
    "online": "在线",
    "fault": "故障",
}

# 后端机器整体状态映射到现有胶囊样式。
MACHINE_STATUS_TONES = {
    "offline": "idle",
    "online": "running",
    "fault": "error",
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
            data: 卡片展示数据，包含机器编号、标题、当前流程和频率。

        Returns:
            返回示例：
                None  # 创建机器卡片
        """
        super().__init__()
        self.setObjectName("machineCard")
        self.machine_id = data["machine_id"]
        self.set_selected(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(MACHINE_CARD_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(9)

        # 创建机器标题和状态徽标。
        heading = QHBoxLayout()
        heading.setSpacing(8)
        self.title = SubtitleLabel()
        self.title.setObjectName("machineTitle")
        self.title.setWordWrap(True)
        self.badge = QLabel()
        self.badge.setObjectName("machineBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        heading.addWidget(self.title)
        heading.addStretch()
        heading.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(heading)


        # 在机器名称下方创建相机连接状态行。
        camera_status_row = QHBoxLayout()
        camera_status_row.setContentsMargins(0, 0, 0, 0)
        camera_status_row.setSpacing(6)

        # 创建相机连接状态色点。
        self.camera_status_dot = QLabel()
        self.camera_status_dot.setObjectName("cameraStatusDot")
        self.camera_status_dot.setFixedSize(7, 7)

        # 显示相机连接状态文字。
        self.camera_status_label = CaptionLabel("未启动")
        self.camera_status_label.setObjectName("cameraStatusText")

        # 将色点和文字加入状态行。
        camera_status_row.addWidget(
            self.camera_status_dot,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        camera_status_row.addWidget(self.camera_status_label)
        camera_status_row.addStretch()
        layout.addLayout(camera_status_row)

        # 在卡片画面区域显示皮带机动画。
        self.belt_animation = BeltAnimationWidget()
        layout.addWidget(self.belt_animation)


        # 在同一行显示当前流程与实时频率。
        metrics = QHBoxLayout()
        metrics.setSpacing(16)
        state_panel = QVBoxLayout()
        state_panel.setSpacing(4)
        state_panel.addWidget(CaptionLabel("当前流程"))
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
        summary_layout.addWidget(CaptionLabel("本轮识别"))
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



        # 弱化相机状态文字和字段标题。
        for label in self.findChildren(CaptionLabel):
            label.setStyleSheet(f"color: {COLORS['muted']};")

        # 初始化机器整体状态为离线。
        self.set_machine_status("offline")

        # 填入机器标题、当前流程和频率。
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

    def set_machine_status(self, status: str) -> None:
        """更新机器卡片右上角的整体状态胶囊。

        Args:
            status: 后端发送的 online、offline 或 fault 状态标识。

        Returns:
            返回示例：
                None  # 整体状态文字和现有胶囊样式已刷新
        """
        # 将整体状态转换为文字和样式标识。
        title = MACHINE_STATUS_TITLES.get(status, "离线")
        tone = MACHINE_STATUS_TONES.get(status, "idle")
        self.setProperty("tone", tone)

        # 刷新整体状态胶囊。
        self.badge.setText(title)
        self.badge.setProperty("tone", tone)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

        # 刷新卡片样式。
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def update_data(self, data: dict):
        """更新机器标题、当前流程和频率展示。

        Args:
            data: 包含 title、state 和 frequency 的卡片展示数据。

        Returns:
            返回示例：
                None  # 更新机器卡片，不创建新控件
        """
        # 更新机器标题、当前流程和频率。
        self.title.setText(data["title"])
        self.state_label.setText(data["state"])
        self.frequency_label.setText(data["frequency"])
        self.update()

    def set_camera_status(self, status: str) -> None:
        """更新机器卡片的相机连接状态。

        Args:
            status: 已有的相机连接状态文字。

        Returns:
            返回示例：
                None  # 相机状态文字和色点已刷新
        """
        # 更新相机连接状态文字。
        self.camera_status_label.setText(status)

        # 将连接状态映射到色点样式。
        tone = "idle"
        if status == "相机已连接":
            tone = "normal"
        elif status in ("连接中", "停止中"):
            tone = "waiting"
        elif status in ("连接失败", "相机故障", "监测失败"):
            tone = "error"

        # 刷新相机状态色点。
        self.camera_status_dot.setProperty("tone", tone)
        self.camera_status_dot.style().unpolish(self.camera_status_dot)
        self.camera_status_dot.style().polish(self.camera_status_dot)

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


class DeviceOverviewCard(SimpleCardWidget):
    """展示启用机器的总数、在线数和故障数。"""

    def paintEvent(self, event):
        """绘制 QSS 定义的卡片背景和边框。

        Args:
            event: 绘制事件。

        Returns:
            None  # 卡片表面已绘制
        """
        QFrame.paintEvent(self, event)

    def __init__(self) -> None:
        """创建设备总览的标题区和三列指标。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 设备总览卡片已创建，三项指标初始为零
        """
        # 设置总览卡片的固定高度。
        super().__init__()
        self.setObjectName("summaryCard")
        self.setFixedHeight(128)

        # 设置卡片内容的留白和间距。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        # 在浅蓝色底座中显示 Fluent 设备图标。
        icon_container = QLabel()
        icon_container.setObjectName("summaryIconContainer")
        icon_container.setFixedSize(32, 32)
        icon_container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = FluentIcon.IOT.icon(color=QColor(COLORS["blue"]))
        icon_container.setPixmap(icon.pixmap(17, 17))

        # 创建设备总览标题和副标题。
        title = QLabel("设备总览")
        title.setObjectName("summaryTitle")
        description = QLabel("当前启用设备的运行状态")
        description.setObjectName("summaryDescription")

        # 纵向排列标题和说明。
        titles_layout = QVBoxLayout()
        titles_layout.setSpacing(0)
        titles_layout.addWidget(title)
        titles_layout.addWidget(description)

        # 横向排列标题区的图标和文字。
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)
        header_layout.addWidget(icon_container)
        header_layout.addLayout(titles_layout, 1)
        layout.addLayout(header_layout)

        # 创建三项指标的数值控件。
        self.total_value = QLabel("0")
        self.online_value = QLabel("0")
        self.fault_value = QLabel("0")
        metrics_layout = QHBoxLayout()
        metrics_layout.setSpacing(0)

        # 将三项指标均分到横向三列。
        for metric_index, (value_label, metric_title) in enumerate((
            (self.total_value, "总机器"),
            (self.online_value, "在线机器"),
            (self.fault_value, "故障机器"),
        )):
            # 在相邻指标之间插入浅灰色竖线。
            if metric_index > 0:
                separator = QFrame()
                separator.setObjectName("summaryMetricSeparator")
                separator.setFixedSize(1, 44)
                metrics_layout.addWidget(separator, 0, Qt.AlignmentFlag.AlignVCenter)

            # 设置当前指标的数值和名称。
            value_label.setObjectName("summaryMetricValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            metric_label = QLabel(metric_title)
            metric_label.setObjectName("summaryMetricLabel")
            metric_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

            # 纵向排列当前指标的数值和名称。
            metric_layout = QVBoxLayout()
            metric_layout.setSpacing(0)
            metric_layout.addWidget(value_label)
            metric_layout.addWidget(metric_label)
            metrics_layout.addLayout(metric_layout, 1)
        layout.addLayout(metrics_layout, 1)

        # 为顶部总览卡片添加轻量阴影。
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(16, 24, 40, 18))
        self.setGraphicsEffect(shadow)

    def set_values(self, total_count: int, online_count: int, fault_count: int) -> None:
        """更新设备总览的三项数量和故障数字颜色。

        Args:
            total_count: 当前启用机器总数。
            online_count: 后端整体状态为 online 的机器数。
            fault_count: 后端整体状态为 fault 的机器数。

        Returns:
            返回示例：
                None  # 三项数量已显示，非零故障数使用红色
        """
        # 更新设备总数、在线数和故障数。
        self.total_value.setText(str(total_count))
        self.online_value.setText(str(online_count))
        self.fault_value.setText(str(fault_count))

        # 仅将非零故障数字设置为故障颜色。
        self.fault_value.setProperty("tone", "fault" if fault_count > 0 else "normal")
        self.fault_value.style().unpolish(self.fault_value)
        self.fault_value.style().polish(self.fault_value)
        self.fault_value.update()


class TodayDetectionCard(SimpleCardWidget):
    """展示今日正式入库的识别数量和待复核数量。"""

    def paintEvent(self, event):
        """绘制 QSS 定义的卡片背景和边框。

        Args:
            event: 绘制事件。

        Returns:
            返回示例：
                None  # 卡片表面已绘制
        """
        QFrame.paintEvent(self, event)

    def __init__(self) -> None:
        """创建今日检测的标题区和两列指标。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 今日检测卡片已创建，两项指标初始为占位符
        """
        # 设置总览卡片的固定高度。
        super().__init__()
        self.setObjectName("summaryCard")
        self.setFixedHeight(128)

        # 设置卡片内容的留白和间距。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        # 在浅蓝色底座中显示 Fluent 文档图标。
        icon_container = QLabel()
        icon_container.setObjectName("summaryIconContainer")
        icon_container.setFixedSize(32, 32)
        icon_container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = FluentIcon.DOCUMENT.icon(color=QColor(COLORS["blue"]))
        icon_container.setPixmap(icon.pixmap(17, 17))

        # 创建今日检测标题和副标题。
        title = QLabel("今日检测")
        title.setObjectName("summaryTitle")
        description = QLabel("今日检测结果与待处理情况")
        description.setObjectName("summaryDescription")

        # 纵向排列标题和说明。
        titles_layout = QVBoxLayout()
        titles_layout.setSpacing(0)
        titles_layout.addWidget(title)
        titles_layout.addWidget(description)

        # 横向排列标题区的图标和文字。
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)
        header_layout.addWidget(icon_container)
        header_layout.addLayout(titles_layout, 1)
        layout.addLayout(header_layout)

        # 创建两项指标的数值控件。
        self.recognition_value = QLabel("--")
        self.pending_review_value = QLabel("--")
        metrics_layout = QHBoxLayout()
        metrics_layout.setSpacing(0)

        # 将两项指标均分到横向两列。
        for metric_index, (value_label, metric_title) in enumerate((
            (self.recognition_value, "今日识别"),
            (self.pending_review_value, "待复核"),
        )):
            # 在相邻指标之间插入浅灰色竖线。
            if metric_index > 0:
                separator = QFrame()
                separator.setObjectName("summaryMetricSeparator")
                separator.setFixedSize(1, 44)
                metrics_layout.addWidget(separator, 0, Qt.AlignmentFlag.AlignVCenter)

            # 设置当前指标的数值和名称。
            value_label.setObjectName("summaryMetricValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            metric_label = QLabel(metric_title)
            metric_label.setObjectName("summaryMetricLabel")
            metric_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

            # 纵向排列当前指标的数值和名称。
            metric_layout = QVBoxLayout()
            metric_layout.setSpacing(0)
            metric_layout.addWidget(value_label)
            metric_layout.addWidget(metric_label)
            metrics_layout.addLayout(metric_layout, 1)
        layout.addLayout(metrics_layout, 1)

        # 为顶部总览卡片添加轻量阴影。
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(16, 24, 40, 18))
        self.setGraphicsEffect(shadow)

    def set_values(
        self,
        recognition_count: int | None,
        pending_review_count: int | None,
    ) -> None:
        """更新今日检测数量和待复核数字颜色。

        Args:
            recognition_count: 今日已入库记录数，读取失败时为 None。
            pending_review_count: 今日待复核记录数，读取失败时为 None。

        Returns:
            返回示例：
                None  # 两项数量或占位符已显示，非零待复核数使用橙色
        """
        # 更新今日识别和待复核数量，读取失败时显示占位符。
        self.recognition_value.setText(
            "--" if recognition_count is None else str(recognition_count)
        )
        self.pending_review_value.setText(
            "--" if pending_review_count is None else str(pending_review_count)
        )

        # 仅将非零待复核数字设置为提醒颜色。
        tone = "normal"
        if pending_review_count is not None and pending_review_count > 0:
            tone = "warning"
        self.pending_review_value.setProperty("tone", tone)
        self.pending_review_value.style().unpolish(self.pending_review_value)
        self.pending_review_value.style().polish(self.pending_review_value)
        self.pending_review_value.update()


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
        self.setMinimumHeight(600)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(0)
        section_label = CaptionLabel("机器详情")
        section_label.setObjectName("detailSectionLabel")
        layout.addWidget(section_label)

        # 显示机器名称和状态徽标。
        layout.addSpacing(5)
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
        layout.addSpacing(18)
        self.attributes_layout = QGridLayout()
        self.attributes_layout.setContentsMargins(0, 0, 0, 0)
        self.attributes_layout.setHorizontalSpacing(24)
        self.attributes_layout.setVerticalSpacing(5)
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
        self.attributes_layout.setRowMinimumHeight(2, 14)
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
        layout.addSpacing(18)
        frequency_card = QFrame()
        frequency_card.setObjectName("detailFrequencyCard")
        frequency_layout = QVBoxLayout(frequency_card)
        frequency_layout.setContentsMargins(16, 12, 16, 12)
        frequency_layout.setSpacing(6)

        # 显示实时频率标题。
        frequency_title = CaptionLabel("实时频率")
        frequency_title.setObjectName("detailCardTitle")
        frequency_layout.addWidget(frequency_title)

        # 在标题下方显示当前频率值。
        self.frequency_label = QLabel("--")
        self.frequency_label.setObjectName("detailFrequencyValue")
        frequency_layout.addWidget(self.frequency_label)
        layout.addWidget(frequency_card)

        # 组织 OCR 标题和可复制的完整分类文字。
        layout.addSpacing(18)
        ocr_card = QFrame()
        ocr_card.setObjectName("detailOcrCard")
        ocr_card_layout = QVBoxLayout(ocr_card)
        ocr_card_layout.setContentsMargins(0, 0, 0, 0)
        ocr_card_layout.setSpacing(8)

        # 显示 OCR 识别结果标题。
        ocr_title = CaptionLabel("OCR 识别结果")
        ocr_title.setObjectName("detailCardTitle")
        ocr_card_layout.addWidget(ocr_title)

        # 在浅灰区域中显示可复制的完整 OCR 文本。
        ocr_panel = QFrame()
        ocr_panel.setObjectName("detailOcrPanel")
        ocr_layout = QVBoxLayout(ocr_panel)
        ocr_layout.setContentsMargins(14, 10, 14, 10)
        self.ocr_text = PlainTextEdit()
        self.ocr_text.setObjectName("detailOcrText")
        self.ocr_text.setReadOnly(True)
        self.ocr_text.setFrameShape(QFrame.Shape.NoFrame)
        self.ocr_text.setFixedHeight(110)

        # 隐藏文本框的焦点装饰并统一默认、悬浮和聚焦背景。
        self.ocr_text.layer.hide()
        ocr_style = (
            "PlainTextEdit#detailOcrText,"
            "PlainTextEdit#detailOcrText:hover,"
            "PlainTextEdit#detailOcrText:focus {"
            f"background: transparent; color: {COLORS['text']};"
            "border: none; border-radius: 0; padding: 0; font-size: 13px;}"
        )
        setCustomStyleSheet(self.ocr_text, ocr_style, ocr_style)
        ocr_layout.addWidget(self.ocr_text)
        ocr_card_layout.addWidget(ocr_panel)
        layout.addWidget(ocr_card)

        # 显示本轮处理标题和步骤。
        layout.addSpacing(18)
        progress_title = CaptionLabel("本轮处理")
        progress_title.setObjectName("detailSectionLabel")
        layout.addWidget(progress_title)
        layout.addSpacing(8)
        self.steps = StepProgress()
        layout.addWidget(self.steps)

        # 在详情底部并排显示三项固定占位统计。
        layout.addSpacing(18)
        stats_bar = QFrame()
        stats_bar.setObjectName("detailStatsBar")
        stats_layout = QHBoxLayout(stats_bar)
        stats_layout.setContentsMargins(10, 10, 10, 10)
        stats_layout.setSpacing(0)
        self.stat_values = {}
        for index, title in enumerate((
            "运行时长",
            "今日识别数量",
            "今日待复核数量",
        )):
            # 在相邻统计项之间显示竖向分隔线。
            if index:
                divider = QFrame()
                divider.setObjectName("detailStatDivider")
                divider.setFixedSize(1, 36)
                stats_layout.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)

            # 设置统计项的内部间距。
            stat_item = QFrame()
            stat_item.setObjectName("detailStatItem")
            stat_layout = QVBoxLayout(stat_item)
            stat_layout.setContentsMargins(6, 0, 6, 0)
            stat_layout.setSpacing(5)

            # 居中显示统计项标题。
            stat_title = QLabel(title)
            stat_title.setObjectName("detailStatTitle")
            stat_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stat_layout.addWidget(stat_title)

            # 在标题下方保存固定占位值。
            stat_value = QLabel("--")
            stat_value.setObjectName("detailStatValue")
            stat_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        self.machine_statuses_by_machine_id = {}
        self.ocr_results_by_machine_id = {}
        self.measurement_states_by_machine_id = {}
        self.selected_machine_id: str | None = None
        self.cards_by_machine_id = {}
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(24, 16, 24, 20)
        outer_layout.setSpacing(12)

        # 固定页面标题和说明。
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(TitleLabel("实时监测"))
        titles.addWidget(BodyLabel("查看当前机器的检测状态"))
        header.addLayout(titles)
        header.addStretch()
        outer_layout.addLayout(header)

        # 按 55 / 45 的比例显示设备总览和今日检测。
        summary_layout = QHBoxLayout()
        summary_layout.setSpacing(16)
        self.device_overview_card = DeviceOverviewCard()
        self.today_detection_card = TodayDetectionCard()
        summary_layout.addWidget(self.device_overview_card, 11)
        summary_layout.addWidget(self.today_detection_card, 9)
        outer_layout.addLayout(summary_layout)

        # 在总览卡片与机器列表分区之间保留明显的区块间距。
        outer_layout.addSpacing(10)

        # 用两行网格组织机器分区，使详情区与机器卡片顶部对齐。
        machine_section_layout = QGridLayout()
        machine_section_layout.setContentsMargins(0, 0, 0, 0)
        machine_section_layout.setHorizontalSpacing(MACHINE_CARD_GAP)
        machine_section_layout.setVerticalSpacing(12)
        outer_layout.addLayout(machine_section_layout, 1)

        # 创建机器列表列的标题行。
        machine_header_layout = QHBoxLayout()
        machine_header_layout.setContentsMargins(0, 0, 0, 0)
        machine_header_layout.setSpacing(8)

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
        machine_header_layout.addLayout(section_layout)
        machine_header_layout.addStretch()

        # 创建机器工作区的三个操作按钮。
        self.refresh_button = PushButton(FluentIcon.SYNC, "刷新")
        self.stop_button = PushButton(FluentIcon.PAUSE, "停止监测")
        self.start_button = PrimaryPushButton(FluentIcon.PLAY, "启动监测")
        self.stop_button.setEnabled(False)

        # 从左到右排列刷新、停止监测和启动监测。
        machine_header_layout.addWidget(self.refresh_button)
        machine_header_layout.addWidget(self.stop_button)
        machine_header_layout.addWidget(self.start_button)

        # 将机器标题行放入网格第一行的机器列表列。
        machine_section_layout.addLayout(machine_header_layout, 0, 0)

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
        machine_section_layout.addWidget(self.scroll_area, 1, 0)

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
        machine_section_layout.addWidget(self.detail_scroll_area, 1, 1)

        # 将剩余宽度分配给机器列表。
        machine_section_layout.setColumnStretch(0, 1)
        machine_section_layout.setColumnStretch(1, 0)

        # 将剩余高度分配给第二行。
        machine_section_layout.setRowStretch(0, 0)
        machine_section_layout.setRowStretch(1, 1)

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
        self.controller.machine_status_changed_signal.connect(
            self.update_machine_status
        )
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

        # 读取正式入库的今日检测统计。
        self.refresh_today_detection_summary()

    def showEvent(self, event) -> None:
        """页面重新显示时同步今日识别和待复核数量。

        Args:
            event: Qt 页面显示事件。

        Returns:
            返回示例：
                None  # 页面已显示，今日检测统计已重新读取
        """
        super().showEvent(event)
        self.refresh_today_detection_summary()

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
                "title": machine["machine_name"],
                "state": "未启动监测",
                "frequency": "--",
            })
            card.clicked.connect(self.select_machine)
            self.machine_cards.append(card)
            machine_id = str(machine["id"])
            self.cards_by_machine_id[machine_id] = card

            # 恢复后端最近发送的机器整体状态。
            card.set_machine_status(
                self.machine_statuses_by_machine_id.get(machine_id, "offline")
            )

            # 恢复已缓存的文字。
            cached_result = self.ocr_results_by_machine_id.get(machine_id)
            if cached_result is not None:
                ordered_lines = cached_result[1]
                normalized_lines = cached_result[2]
                card.set_ocr_result(ordered_lines, normalized_lines)

            # 恢复当前周期的进度节点。
            measurement_state = self.measurement_states_by_machine_id.get(machine_id)
            if measurement_state is not None:
                # 运行中的周期恢复皮带和仍在执行的子动画。
                if measurement_state["machine_running"]:
                    card.belt_animation.start_machine()

                    # 没有失败进度时恢复仍在执行的子动画。
                    progress_statuses = measurement_state["progress_statuses"]
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
        """根据后端统一机器整体状态刷新设备总览。

        Args:
            无。

        Returns:
            返回示例：
                None  # 启用机器总数、在线机器数和故障机器数已刷新
        """
        # 读取后端发布的机器整体状态。
        machine_statuses = self.machine_statuses_by_machine_id.values()

        # 统计在线机器数。
        online_count = sum(status == "online" for status in machine_statuses)

        # 统计后端发布的故障机器数。
        fault_count = sum(status == "fault" for status in machine_statuses)

        # 刷新设备总览的三项数量。
        self.device_overview_card.set_values(
            len(self.machines),
            online_count,
            fault_count,
        )

    def refresh_today_detection_summary(self) -> None:
        """通过 Controller 读取并显示今天的正式检测统计。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 今日统计已显示，读取失败时显示占位符并记录日志
        """
        # 请求今天的正式入库统计。
        result = self.controller.get_today_measurement_summary()
        if not result.success:
            self.today_detection_card.set_values(None, None)
            logger.warning("今日检测统计读取失败：%s", result.message)
            return

        # 更新今日识别数量和待复核数量。
        self.today_detection_card.set_values(
            result.data["recognition_count"],
            result.data["pending_review_count"],
        )

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
        self.machine_statuses_by_machine_id.clear()
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

    def update_machine_status(self, machine_id: str, status: str) -> None:
        """保存并显示后端发送的机器整体状态。

        Args:
            machine_id: 数据库机器编号的字符串形式。
            status: 后端发送的 online、offline 或 fault 状态标识。

        Returns:
            返回示例：
                None  # 整体状态已缓存，现有卡片和选中详情已同步
        """
        # 缓存后端状态并查找当前卡片。
        self.machine_statuses_by_machine_id[machine_id] = status
        card = self.cards_by_machine_id.get(machine_id)
        if card is None:
            return

        # 显示后端状态并刷新总览。
        card.set_machine_status(status)
        self.update_dashboard_summary()

        # 同步选中机器的详情。
        if machine_id == self.selected_machine_id:
            self.refresh_selected_machine_detail()

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
        card = self.cards_by_machine_id.get(machine_id)
        if card is None:
            return

        # 将相机连接结果转换为当前流程文字。
        current_state = status
        if status == "未启动":
            current_state = "未启动监测"
        elif status == "连接中":
            current_state = "正在连接相机"
        elif status == "相机已连接":
            current_state = "等待启停信号"
        elif status == "停止中":
            current_state = "正在停止监测"
        elif status == "已停止":
            current_state = "监测已停止"

        # 更新卡片的当前流程和频率。
        card.update_data({
            "title": card.title.text(),
            "state": current_state,
            "frequency": "--",
        })

        # 显示相机连接结果。
        card.set_camera_status(status)
        card.setToolTip(reason)

        # 同步选中机器的详情。
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

        # 证据入库成功后重新读取正式检测统计。
        if stage == "evidence_storage" and status == "success":
            self.refresh_today_detection_summary()

        # 找到对应机器的卡片。
        card = self.cards_by_machine_id.get(machine_id)
        if card is None:
            return

        # 新周期清空上一轮的 OCR 展示。
        if is_new_session:
            card.clear_ocr_result()

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

        # 更新卡片的当前测量流程和频率。
        stage_title = PROGRESS_STAGE_TITLES[stage]
        status_title = PROGRESS_STATUS_TITLES[status]
        card.update_data({
            "title": card.title.text(),
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

