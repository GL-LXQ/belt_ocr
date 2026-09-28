"""实时监测页面、机器卡片和步骤进度组件。"""

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QPixmap
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
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    SubtitleLabel,
    TitleLabel,
)

from src.controller.controller import AppController
from ui.belt_animation import BeltAnimationWidget
from ui.theme import create_icon


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
        for index, title in enumerate(PROGRESS_STAGE_TITLES.values()):
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
                "success": "#18AE59",
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
                "color: #34465F; font-weight: bold;" if active else ""
            )
            dot.setStyleSheet(
                f"background: {color}; color: white; "
                f"border: 2px solid {border}; border-radius: 12px;"
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
            connector.setGeometry(left, left_dot.center().y(), right - left, 2)


class MachineCard(SimpleCardWidget):
    """展示一台机器的动画、状态、频率和进度。"""

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
        self.progress_session_id = ""
        self.progress_statuses = {}
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

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
        heading_divider = QFrame()
        heading_divider.setFrameShape(QFrame.Shape.HLine)
        heading_divider.setObjectName("cardDivider")
        layout.addWidget(heading_divider)

        # 在卡片画面区域显示皮带机动画。
        self.belt_animation = BeltAnimationWidget()
        layout.addWidget(self.belt_animation)
        animation_divider = QFrame()
        animation_divider.setFrameShape(QFrame.Shape.HLine)
        animation_divider.setObjectName("cardDivider")
        layout.addWidget(animation_divider)

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
        frequency_panel.addWidget(self.frequency_label)
        metrics.addLayout(state_panel, 2)
        metrics.addLayout(frequency_panel, 1)
        layout.addLayout(metrics)
        metrics_divider = QFrame()
        metrics_divider.setFrameShape(QFrame.Shape.HLine)
        metrics_divider.setObjectName("cardDivider")
        layout.addWidget(metrics_divider)

        # 让 OCR 文字独占整行并支持复制。
        layout.addWidget(CaptionLabel("OCR 识别结果"))
        self.ocr_result_label = QLabel("--")
        self.ocr_result_label.setObjectName("ocrResult")
        self.ocr_result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.ocr_result_label.setWordWrap(True)
        self.ocr_result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.ocr_result_label)
        self.clear_ocr_result()
        ocr_divider = QFrame()
        ocr_divider.setFrameShape(QFrame.Shape.HLine)
        ocr_divider.setObjectName("cardDivider")
        layout.addWidget(ocr_divider)

        # 创建本轮步骤进度区域。
        self.steps = StepProgress()
        layout.addWidget(self.steps)

        self.update_data(data)

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
        """按去空格文字长度分类显示原始 OCR 文字。

        Args:
            ordered_lines: 保留原始格式的最终文字。
            normalized_lines: 与原始文字逐条对应的去空格文字。

        Returns:
            返回示例：
                None  # 按 20、8、3、2 分类显示，每条结果单独换行
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
        self.ocr_result_label.setText("\n".join(result_lines))

    def clear_ocr_result(self) -> None:
        """将四类 OCR 结果恢复为占位文字。

        Args:
            无。

        Returns:
            返回示例：
                None  # 四个类别均显示 --
        """
        self.ocr_result_label.setText("20  --\n8  --\n3  --\n2  --")


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
        self.controller = controller
        self.closing_requested = False
        self.connection_states = {}
        self.ocr_results_by_machine_id = {}
        self.measurement_states_by_machine_id = {}
        self.cards_by_machine_id = {}
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(24, 24, 24, 24)
        outer_layout.setSpacing(20)

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

        # 将机器卡片放入可滚动区域。
        self.scroll_area = ScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer_layout.addWidget(self.scroll_area)
        content = QWidget()
        content.setObjectName("monitorContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(16)
        self.scroll_area.setWidget(content)

        # 网格靠上排列，空余高度留在底部。
        cards_layout = QGridLayout()
        cards_layout.setSpacing(16)
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
            return

        # 按机器编号创建卡片，并恢复最近一次连接结果。
        for machine in self.machines:
            card = MachineCard({
                "title": machine["machine_name"],
                "tone": "idle",
                "status": "未启动",
                "state": "未启动监测",
                "frequency": "--",
            })
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
                card.steps.update_steps(card.progress_statuses)

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

    def reflow_cards(self):
        """按滚动区宽度重新排列已有机器卡片。

        Args:
            无。

        Returns:
            None  # 卡片实例已按一至三列排列
        """
        if not self.machine_cards:
            return
        available_width = self.scroll_area.viewport().width()
        column_count = max(1, min(3, (available_width + 16) // 376))
        if column_count == self.card_column_count:
            return

        # 清除旧位置和列宽，再复用原有卡片。
        while self.cards_layout.count():
            self.cards_layout.takeAt(0)
        for column_index in range(3):
            self.cards_layout.setColumnStretch(column_index, 0)
            self.cards_layout.setColumnMinimumWidth(column_index, 0)
        for card_index, card in enumerate(self.machine_cards):
            self.cards_layout.addWidget(
                card,
                card_index // column_count,
                card_index % column_count,
            )
        for column_index in range(column_count):
            self.cards_layout.setColumnStretch(column_index, 1)
        self.card_column_count = column_count
        self.scroll_area.widget().updateGeometry()

    def eventFilter(self, watched, event):
        """在滚动区宽度变化时重新排列卡片。

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
        card = self.cards_by_machine_id.get(machine_id)
        if card is None:
            return

        # 将连接结果转换为卡片文字和颜色，频率及测量进度保持未接入。
        tone = "waiting" if status in ("连接中", "停止中", "连接失败", "相机故障", "监测失败") else "idle"
        card.update_data({
            "title": card.title.text(),
            "tone": tone,
            "status": status,
            "state": "等待启停信号接入" if status == "相机已连接" else status,
            "frequency": "--",
        })
        card.setToolTip(reason)

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

        # 更新本轮进度节点。
        card.steps.update_steps(progress_statuses)

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
            return

        # 更新卡片当前测量状态。
        stage_title = PROGRESS_STAGE_TITLES[stage]
        status_title = PROGRESS_STATUS_TITLES[status]
        card.update_data({
            "title": card.title.text(),
            "tone": "waiting" if status == "failed" else "running",
            "status": "测量失败" if status == "failed" else "测量中",
            "state": f"{stage_title}{status_title}",
            "frequency": card.frequency_label.text(),
        })

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

        # 恢复操作按钮，并展示没有对应卡片的初始化故障。
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if failure_message and not self.closing_requested:
            InfoBar.error("监测已停止", failure_message, duration=-1, parent=self)

