"""机器管理页面。"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.controller.controller import AppController
from ui.theme import create_icon


class MachinesPage(QWidget):
    """展示机器列表与新增、编辑共用的弹窗。"""

    def __init__(self, controller: AppController):
        """读取机器数据并初始化列表、表单和页面展示。

        Args:
            controller: 界面业务控制器。

        Returns:
            返回示例：
                None  # 完成机器页面初始化
        """
        # 读取数据库机器记录并创建页面布局。
        super().__init__()
        self.setObjectName("machines")
        self.controller = controller
        result = self.controller.list_machines()
        self.machines = result.data if result.success else []
        if not result.success:
            QMessageBox.warning(self, "机器读取失败", result.message)
        # 记录正在编辑的机器编号，None 表示新增。
        self.editing_machine_id = None
        self.field_inputs = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        # 沿用页面标题与主题图标。
        header = QWidget()
        header.setObjectName("pageHeader")
        header.setMinimumHeight(64)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(create_icon("machines", "blue").pixmap(QSize(28, 28)))
        header_layout.addWidget(icon)
        heading_layout = QVBoxLayout()
        heading_layout.setSpacing(4)
        title = QLabel("机器管理")
        title.setObjectName("pageTitle")
        subtitle = QLabel("管理检测机器的基本信息，包括相机、频率仪的绑定与启用状态。")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        heading_layout.addWidget(title)
        heading_layout.addWidget(subtitle)
        header_layout.addLayout(heading_layout, 1)
        self.create_button = QPushButton("＋ 新建机器")
        self.create_button.setProperty("buttonRole", "primary")
        self.create_button.setFixedSize(132, 42)
        header_layout.addWidget(self.create_button)
        layout.addWidget(header)

        # 将机器表格铺满白色内容区。
        content = QFrame()
        content.setObjectName("pageContent")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(16)
        self.table = QTableWidget(0, 8)
        self.table.setObjectName("machineTable")
        self.table.setHorizontalHeaderLabels(
            ["ID", "机器名称", "相机序列号", "频率仪序列号", "状态", "创建时间", "更新时间", "操作"]
        )
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(58)
        self.table.horizontalHeader().setMinimumHeight(48)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # 编号居中，其余表头与对应内容左对齐。
        self.table.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMinimumWidth(400)
        for column, width in enumerate((40, 120, 128, 112, 96, 152, 152, 136)):
            self.table.setColumnWidth(column, width)
        # 操作列承接末尾空白，业务字段保持紧凑排列。
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        content_layout.addWidget(self.table, 1)

        layout.addWidget(content, 1)

        # 创建新增和编辑共用的标准模态弹窗。
        self.editor = QDialog(self)
        self.editor.setObjectName("machineEditor")
        self.editor.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.editor.resize(440, 590)
        self.editor.setMinimumSize(400, 540)
        self.build_machine_editor()
        self.populate_machines()

        # 绑定保存与取消，单击表格仅选中整行。
        self.create_button.clicked.connect(self.create_machine)
        self.save_button.clicked.connect(self.save_machine)
        self.cancel_button.clicked.connect(self.editor.reject)

    def build_machine_editor(self):
        """创建带滚动内容和固定操作区的机器表单。

        Args:
            无。

        Returns:
            返回示例：
                None  # 创建并保存表单控件引用
        """
        # 创建弹窗布局，关闭入口使用系统标题栏。
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        # 在滚动区域中排列必填字段。
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        fields = QWidget()
        fields.setObjectName("machineFields")
        fields_layout = QVBoxLayout(fields)
        fields_layout.setContentsMargins(18, 18, 18, 18)
        fields_layout.setSpacing(10)
        for field, caption in (
            ("machine_name", "机器名称"),
            ("camera_serial", "相机序列号"),
            ("frequency_meter_serial", "频率仪序列号"),
        ):
            label = QLabel(f'{caption} <span style="color:#EF4444">*</span>')
            label.setProperty("fieldLabel", True)
            field_input = QLineEdit()
            field_input.setMinimumHeight(36)
            field_input.setAccessibleName(caption)
            label.setBuddy(field_input)
            self.field_inputs[field] = field_input
            fields_layout.addWidget(label)
            fields_layout.addWidget(field_input)
            fields_layout.addSpacing(10)

        # 使用标准复选框展示启用状态，并添加备注输入。
        enabled_label = QLabel("是否启用")
        enabled_label.setProperty("fieldLabel", True)
        self.enabled_checkbox = QCheckBox("启用")
        self.enabled_checkbox.setObjectName("machineEnabled")
        fields_layout.addWidget(enabled_label)
        fields_layout.addWidget(self.enabled_checkbox)
        fields_layout.addSpacing(10)
        remark_label = QLabel("备注")
        remark_label.setProperty("fieldLabel", True)
        self.remark_input = QTextEdit()
        self.remark_input.setAcceptRichText(False)
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
        actions.setContentsMargins(18, 16, 18, 18)
        actions.setSpacing(16)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setProperty("buttonRole", "secondary")
        self.save_button = QPushButton("保存")
        self.save_button.setProperty("buttonRole", "primary")
        self.save_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        for button in (self.cancel_button, self.save_button):
            button.setMinimumHeight(40)
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
            status_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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
            actions_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for action, caption in (("edit", "编辑"), ("delete", "删除")):
                button = QPushButton(caption)
                button.setFixedHeight(30)
                button.setProperty("buttonRole", "text")
                button.setIcon(create_icon(action, "muted"))
                button.setObjectName(f"{action}Machine_{machine['id']}")
                if action == "edit":
                    button.clicked.connect(
                        lambda checked=False, selected_id=machine["id"]: self.edit_machine(selected_id)
                    )
                else:
                    button.clicked.connect(
                        lambda checked=False, selected_id=machine["id"]: self.delete_machine(selected_id)
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
        self.editor.setWindowTitle("新建机器")
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
        self.editor.setWindowTitle("编辑机器")
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
                QMessageBox.warning(self.editor, "请填写必填信息", f"请填写{field_input.accessibleName()}。")
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
            QMessageBox.warning(self.editor, "保存失败", result.message)
            if result.data is not None:
                self.field_inputs[result.data["field"]].setFocus()
            return

        # 保存成功后关闭表单，并重新读取数据库记录。
        saved_machine_id = result.data
        self.editor.accept()
        result = self.controller.list_machines()
        if not result.success:
            QMessageBox.warning(self, "列表刷新失败", f"机器已保存，列表刷新失败：{result.message}")
            return
        self.machines = result.data

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
        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("删除机器")
        confirmation.setIcon(QMessageBox.Icon.Question)
        confirmation.setText(f"确定删除“{machine['machine_name']}”吗？")
        delete_button = confirmation.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = confirmation.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        confirmation.setDefaultButton(cancel_button)
        confirmation.exec()
        if confirmation.clickedButton() is not delete_button:
            return

        # 标记删除机器。
        result = self.controller.delete_machine(machine_id)
        if not result.success:
            QMessageBox.warning(self, "删除失败", result.message)
            return

        # 删除成功后重新读取列表并刷新表格。
        result = self.controller.list_machines()
        if not result.success:
            QMessageBox.warning(self, "列表刷新失败", f"机器已删除，列表刷新失败：{result.message}")
            return
        self.machines = result.data
        self.populate_machines()
