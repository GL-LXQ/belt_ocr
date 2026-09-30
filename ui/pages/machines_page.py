"""机器管理页面。"""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    InfoBar,
    LineEdit,
    MaskDialogBase,
    MessageBox,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    TableWidget,
    TitleLabel,
    TransparentPushButton,
)

from src.controller.controller import AppController
from ui.theme import create_icon


class MachinesPage(QWidget):
    """展示机器列表与新增、编辑共用的弹窗。"""

    def __init__(self, controller: AppController, parent: QWidget | None = None):
        """读取机器数据并初始化列表、表单和页面展示。

        Args:
            controller: 界面业务控制器。
            parent: 所属主窗口。

        Returns:
            返回示例：
                None  # 完成机器页面初始化
        """
        # 读取数据库机器记录并创建页面布局。
        super().__init__(parent)
        self.setObjectName("machines")
        self.controller = controller
        result = self.controller.list_machines()
        self.machines = result.data["machines"] if result.success else []
        if not result.success:
            InfoBar.error("机器读取失败", result.message, duration=-1, parent=self)
        # 记录正在编辑的机器编号，None 表示新增。
        self.editing_machine_id = None
        self.field_inputs = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 4, 24, 20)
        layout.setSpacing(20)

        # 在固定页头显示标题和新建入口。
        header_layout = QHBoxLayout()
        heading_layout = QVBoxLayout()
        heading_layout.setSpacing(4)
        title = TitleLabel("机器管理")
        subtitle = BodyLabel("管理检测机器的基本信息，包括相机、频率仪的绑定与启用状态。")
        subtitle.setWordWrap(True)
        heading_layout.addWidget(title)
        heading_layout.addWidget(subtitle)
        header_layout.addLayout(heading_layout, 1)
        self.create_button = PrimaryPushButton(FluentIcon.ADD, "新建机器")
        header_layout.addWidget(self.create_button)
        layout.addLayout(header_layout)

        # 将机器表格铺满白色内容区。
        content = SimpleCardWidget()
        content.setObjectName("machineTableCard")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(16)
        self.table = TableWidget()
        self.table.setColumnCount(8)
        self.table.setObjectName("machineTable")
        self.table.setHorizontalHeaderLabels(
            ["ID", "机器名称", "相机序列号", "频率仪序列号", "状态", "创建时间", "更新时间", "操作"]
        )
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(50)
        self.table.horizontalHeader().setMinimumHeight(48)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # 编号居中，其余表头与对应内容左对齐。
        self.table.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMinimumWidth(0)
        self.table.setSortingEnabled(False)
        for column, width in enumerate((50, 180, 170, 170, 96, 160, 160, 180)):
            self.table.setColumnWidth(column, width)
        # 机器名称随可用宽度伸展，并保留最低可读宽度。
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.table.viewport().installEventFilter(self)
        content_layout.addWidget(self.table, 1)

        layout.addWidget(content, 1)

        # 创建新增和编辑共用的遮罩表单。
        dialog_parent = self.parentWidget() or self
        self.editor = MaskDialogBase(dialog_parent)
        self.editor.setObjectName("machineEditor")
        self.editor.widget.setObjectName("machineEditorContent")
        self.editor.widget.setFixedSize(
            min(620, dialog_parent.width() - 80),
            min(640, dialog_parent.height() - 80),
        )
        self.editor.setClosableOnMaskClicked(False)
        self.build_machine_editor()
        self.editor.hide()
        self.populate_machines()

        # 绑定保存与取消，单击表格仅选中整行。
        self.create_button.clicked.connect(self.create_machine)
        self.save_button.clicked.connect(self.save_machine)
        self.cancel_button.clicked.connect(self.editor.reject)

    def eventFilter(self, watched, event):
        """按表格可用宽度调整机器名称列。

        Args:
            watched: 接收事件的控件。
            event: Qt 事件。

        Returns:
            bool  # 事件继续交给父类处理
        """
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            fixed_columns = (0, 2, 3, 4, 5, 6, 7)
            fixed_width = sum(
                self.table.columnWidth(column) for column in fixed_columns
            )
            available_width = self.table.viewport().width() - fixed_width
            self.table.setColumnWidth(1, max(220, available_width))
        return super().eventFilter(watched, event)

    def build_machine_editor(self):
        """创建带滚动内容和固定操作区的机器表单。

        Args:
            无。

        Returns:
            返回示例：
                None  # 创建并保存表单控件引用
        """
        # 在中心容器中安排表单标题和可滚动正文。
        editor_layout = QVBoxLayout(self.editor.widget)
        editor_layout.setContentsMargins(24, 22, 24, 20)
        editor_layout.setSpacing(14)
        self.editor_title = TitleLabel("新建机器")
        self.editor_description = CaptionLabel("填写机器与设备的绑定信息")
        editor_layout.addWidget(self.editor_title)
        editor_layout.addWidget(self.editor_description)

        # 在滚动区域中排列必填字段。
        self.editor_scroll = ScrollArea()
        self.editor_scroll.setObjectName("machineEditorScroll")
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        fields = QWidget()
        fields.setObjectName("machineFields")
        fields_layout = QVBoxLayout(fields)
        fields_layout.setContentsMargins(0, 0, 4, 0)
        fields_layout.setSpacing(10)
        for field, caption in (
            ("machine_name", "机器名称"),
            ("camera_serial", "相机序列号"),
            ("frequency_meter_serial", "频率仪序列号"),
        ):
            label = BodyLabel(f"{caption} *")
            field_input = LineEdit()
            field_input.setAccessibleName(caption)
            label.setBuddy(field_input)
            self.field_inputs[field] = field_input
            fields_layout.addWidget(label)
            fields_layout.addWidget(field_input)
            fields_layout.addSpacing(10)

        # 使用标准复选框展示启用状态，并添加备注输入。
        enabled_label = BodyLabel("是否启用")
        self.enabled_checkbox = CheckBox("启用")
        self.enabled_checkbox.setObjectName("machineEnabled")
        fields_layout.addWidget(enabled_label)
        fields_layout.addWidget(self.enabled_checkbox)
        fields_layout.addSpacing(10)
        remark_label = BodyLabel("备注")
        self.remark_input = PlainTextEdit()
        self.remark_input.setFixedHeight(84)
        self.remark_input.setAccessibleName("备注")
        fields_layout.addWidget(remark_label)
        fields_layout.addWidget(self.remark_input)
        fields_layout.addSpacing(12)

        # 将表单放入滚动区域并保留底部弹性空间。
        fields_layout.addStretch()
        self.editor_scroll.setWidget(fields)
        editor_layout.addWidget(self.editor_scroll, 1)

        # 将取消和保存按钮固定在滚动区域之外。
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        actions.addStretch()
        self.cancel_button = PushButton("取消")
        self.save_button = PrimaryPushButton("保存")
        for button in (self.cancel_button, self.save_button):
            actions.addWidget(button)
        editor_layout.addLayout(actions)

    def populate_machines(self):
        """将内存机器数据填入表格并创建每行操作入口。

        Args:
            无。

        Returns:
            返回示例：
                None  # 更新表格内容
        """
        # 按固定字段顺序填入机器信息。
        self.table.setRowCount(len(self.machines))
        for row, machine in enumerate(self.machines):
            values = (
                machine["id"],
                machine["machine_name"],
                machine["camera_serial"],
                machine["frequency_meter_serial"],
                "",
                machine["created_at"],
                machine["updated_at"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                if column == 0:
                    alignment = Qt.AlignmentFlag.AlignCenter
                item.setTextAlignment(alignment)
                self.table.setItem(row, column, item)

            # 在状态列中放置圆角状态标签。
            status_container = QWidget()
            status_layout = QHBoxLayout(status_container)
            status_layout.setContentsMargins(8, 0, 8, 0)
            status_layout.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            status = QLabel("● 启用" if machine["enabled"] else "● 停用")
            status.setObjectName("machineStatus")
            status.setFixedSize(64, 28)
            status.setProperty("enabledStatus", machine["enabled"])
            status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status_layout.addWidget(status)
            self.table.setCellWidget(row, 4, status_container)

            # 为每行复用相同的标准操作按钮布局，按钮记录本行机器编号。
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(4, 0, 4, 0)
            actions_layout.setSpacing(4)
            actions_layout.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            machine_id = machine["id"]
            for action, caption in (("edit", "编辑"), ("delete", "删除")):
                button = TransparentPushButton(caption)
                button.setIcon(create_icon(action, "muted"))
                button.setObjectName(f"{action}Machine_{machine['id']}")
                if action == "edit":
                    button.clicked.connect(
                        lambda checked=False, selected_id=machine_id:
                        self.edit_machine(selected_id)
                    )
                else:
                    button.clicked.connect(
                        lambda checked=False, selected_id=machine_id:
                        self.delete_machine(selected_id)
                    )
                actions_layout.addWidget(button)
            self.table.setCellWidget(row, 7, actions)

    def select_machine_row(self, machine_id: int):
        """按机器编号选中表格行。

        Args:
            machine_id: 要选中的机器编号。

        Returns:
            返回示例：
                None  # 选中该机器所在行，编号不存在时保持原选中
        """
        # 在已读取的机器列表中定位编号并选中对应行。
        for row, machine in enumerate(self.machines):
            if machine["id"] == machine_id:
                self.table.selectRow(row)
                return

    def create_machine(self):
        """清空新增表单并进入新增机器状态。

        Args:
            无。

        Returns:
            返回示例：
                None  # 展示未保存的新机器表单
        """
        # 进入新增状态，清空当前机器选择和输入内容。
        self.editing_machine_id = None
        self.table.clearSelection()
        for field_input in self.field_inputs.values():
            field_input.clear()
        self.remark_input.clear()
        self.enabled_checkbox.setChecked(True)

        # 更新新增提示并将焦点放到机器名称。
        self.editor_title.setText("新建机器")
        self.save_button.setEnabled(True)
        dialog_parent = self.editor.parentWidget()
        self.editor.widget.setFixedSize(
            min(620, dialog_parent.width() - 80),
            min(640, dialog_parent.height() - 80),
        )
        self.editor.open()
        self.field_inputs["machine_name"].setFocus()

    def edit_machine(self, machine_id: int):
        """把机器信息填入共用表单并进入编辑状态。

        Args:
            machine_id: 要编辑的机器编号。

        Returns:
            返回示例：
                None  # 展示待修改的机器信息
        """
        # 按编号取出机器并回填表单、启用状态和备注。
        machine = next(item for item in self.machines if item["id"] == machine_id)
        self.editing_machine_id = machine_id
        for field, field_input in self.field_inputs.items():
            field_input.setText(machine[field])
        self.enabled_checkbox.setChecked(machine["enabled"])
        self.remark_input.setPlainText(machine["remark"])

        # 选中该行并打开编辑弹窗。
        self.select_machine_row(machine_id)
        self.editor_title.setText("编辑机器")
        self.save_button.setEnabled(True)
        dialog_parent = self.editor.parentWidget()
        self.editor.widget.setFixedSize(
            min(620, dialog_parent.width() - 80),
            min(640, dialog_parent.height() - 80),
        )
        self.editor.open()
        self.field_inputs["machine_name"].setFocus()

    def save_machine(self):
        """校验表单、写入新增或修改结果并刷新列表。

        Args:
            无。

        Returns:
            返回示例：
                None  # 保存机器并刷新列表，或提示输入与数据库错误
        """
        # 读取并校验三个必填输入。
        values = {}
        for field, field_input in self.field_inputs.items():
            value = field_input.text().strip()
            if not value:
                InfoBar.error(
                    "请填写必填信息",
                    f"请填写{field_input.accessibleName()}。",
                    duration=-1,
                    parent=self.editor.widget,
                )
                field_input.setFocus()
                return
            values[field] = value

        # 读取启用状态和备注，并按当前状态新增或修改机器。
        values["enabled"] = self.enabled_checkbox.isChecked()
        values["remark"] = self.remark_input.toPlainText().strip() or None
        if self.editing_machine_id is None:
            result = self.controller.create_machine(**values)
        else:
            result = self.controller.update_machine(self.editing_machine_id, **values)

        # 保存失败时显示提示并定位对应字段。
        if not result.success:
            InfoBar.error(
                "保存失败", result.message, duration=-1, parent=self.editor.widget
            )
            if result.data is not None:
                self.field_inputs[result.data["field"]].setFocus()
            return

        # 保存成功后关闭表单，并重新读取数据库记录。
        saved_machine_id = result.data["machine_id"]
        self.save_button.setEnabled(False)
        self.editor.accept()
        result = self.controller.list_machines()
        if not result.success:
            InfoBar.error(
                "列表刷新失败",
                f"机器已保存，列表刷新失败：{result.message}",
                duration=-1,
                parent=self,
            )
            return
        self.machines = result.data["machines"]

        # 刷新列表并选中刚保存的机器。
        self.populate_machines()
        self.select_machine_row(saved_machine_id)

    def delete_machine(self, machine_id: int):
        """确认后删除机器并刷新列表。

        Args:
            machine_id: 要删除的机器编号。

        Returns:
            返回示例：
                None  # 删除机器并刷新列表，或保留记录并提示失败
        """
        # 显示机器名称并请求确认。
        machine = next(item for item in self.machines if item["id"] == machine_id)
        confirmation = MessageBox(
            "删除机器", f"确定删除“{machine['machine_name']}”吗？", self.window()
        )
        confirmation.yesButton.setText("删除")
        confirmation.cancelButton.setText("取消")
        confirmation.yesButton.setAutoDefault(False)
        confirmation.yesButton.setDefault(False)
        confirmation.cancelButton.setDefault(True)
        confirmation.cancelButton.setFocus()
        if not confirmation.exec():
            return

        # 标记删除机器。
        result = self.controller.delete_machine(machine_id)
        if not result.success:
            InfoBar.error("删除失败", result.message, duration=-1, parent=self)
            return

        # 删除成功后重新读取列表并刷新表格。
        result = self.controller.list_machines()
        if not result.success:
            InfoBar.error(
                "列表刷新失败",
                f"机器已删除，列表刷新失败：{result.message}",
                duration=-1,
                parent=self,
            )
            return
        self.machines = result.data["machines"]
        self.populate_machines()
