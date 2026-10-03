"""以分组表单展示系统配置，并在内存中编辑可撤销的预览草稿。"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    TitleLabel,
    setCustomStyleSheet,
)

from src.controller.controller import AppController
from ui.configuration_preview import read_configuration_preview
from ui.theme import COLORS


def normalize_preview_value(value: object, value_kind: str) -> tuple:
    """按字段类型比较草稿，保留空值和无法解析的原始值。

    Args:
        value: 当前草稿值或读取时的原始值。
        value_kind: number、integer 或 text。

    Returns:
        返回示例：
            ("null",)  # 空值与零或空字符串不同
            ("number", Decimal("80"))  # 80 和 80.0 视为同一数值
            ("raw", "str", "'现场值'")  # 不能按数值解释的内容保持原样
    """
    # 为未配置的值保留独立标识。
    if value is None:
        return ("null",)

    # 只在数值字段中比较有限数值，不把布尔值转换成零或一。
    if value_kind in ("number", "integer") and isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            pass
        else:
            if number.is_finite():
                return (
                    "number",
                    number,
                )

    # 其他值同时保留原有类型和文本，避免隐式修正异常配置。
    return (
        "raw",
        type(value).__name__,
        repr(value),
    )


class ConfigurationValueEditor(QWidget):
    """保留实际值的文本输入，可显式选择沿用设备或填写数值。"""

    value_changed = Signal()

    def __init__(
        self,
        value_kind: str = "text",
        unit: str = "",
        nullable: bool = False,
        null_label: str = "沿用设备 (null)",
        parent: QWidget | None = None,
    ) -> None:
        """建立不限制设备范围的草稿输入控件。

        Args:
            value_kind: number、integer 或 text。
            unit: 输入框旁的只读单位。
            nullable: 是否提供显式空值选项。
            null_label: 空值选项的可读说明。
            parent: 所属表单行。

        Returns:
            返回示例：
                None  # 输入框和可选的空值选择器已建立
        """
        super().__init__(parent)
        self.value_kind = value_kind
        self.original_value = None
        self.original_text = ""
        self.mode_combo = None

        # 在同一行中排列空值选项、文本输入和单位。
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        if nullable:
            self.mode_combo = ComboBox(self)
            self.mode_combo.addItem(null_label, userData=None)
            self.mode_combo.addItem("指定值", userData="value")
            self.mode_combo.setFixedWidth(150)
            layout.addWidget(self.mode_combo)
        self.input = LineEdit(self)
        self.input.setObjectName("settingsValueInput")
        self.input.setMinimumWidth(90)
        self.input.setFixedHeight(36)
        self.input.setPlaceholderText("未配置" if not nullable else "填写实际值")
        layout.addWidget(self.input, 1)
        if unit:
            unit_label = QLabel(unit, self)
            unit_label.setObjectName("settingsUnit")
            layout.addWidget(unit_label)

        # 输入变化只更新草稿；切换空值时保留输入框中的临时文本。
        self.input.textChanged.connect(self.update_value_state)
        if self.mode_combo is not None:
            self.mode_combo.currentIndexChanged.connect(self.update_value_state)
        self.set_value(None)

    def set_value(self, value: object) -> None:
        """把基线值原样放入控件，并保留撤销时需要的类型。

        Args:
            value: 从现有配置读取的实际值。

        Returns:
            返回示例：
                None  # 空值、原始文本和数值均已原样展示
        """
        # 阻止回填期间发出草稿变更信号。
        blocker = QSignalBlocker(self)
        self.original_value = deepcopy(value)
        self.original_text = "" if value is None else str(value)
        self.input.setText(self.original_text)
        if self.mode_combo is not None:
            self.mode_combo.setCurrentIndex(0 if value is None else 1)
        self.update_value_state()
        del blocker

    def get_value(self) -> object:
        """取得草稿值，未修改的文本继续返回原有值和类型。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 显式沿用设备
                80.0  # 手动填写的有限数值
                "Line2"  # 现场线路值
                "待填写"  # 尚未完成的输入保持原文
        """
        # 显式空值与手动填写的空字符串分开保存。
        if self.mode_combo is not None and self.mode_combo.currentData() is None:
            return None
        text = self.input.text()
        if text == self.original_text and self.original_value is not None:
            return deepcopy(self.original_value)
        if text == "" and self.mode_combo is None and self.original_value is None:
            return None

        # 数值草稿只转换完整且有限的输入，不截断、夹取或补默认值。
        if self.value_kind in ("number", "integer"):
            try:
                number = Decimal(text.strip())
            except InvalidOperation:
                return text
            if number.is_finite() and math.isfinite(float(number)):
                if self.value_kind == "integer" and number == number.to_integral_value():
                    return int(number)
                if self.value_kind == "number" and Decimal(str(float(number))) == number:
                    return float(number)
        return text

    def update_value_state(self) -> None:
        """同步空值选项的输入可用状态并通知页面。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 输入状态和页面草稿提示已同步
        """
        # 沿用设备时保留文本，但不将其作为当前草稿值。
        enabled = self.mode_combo is None or self.mode_combo.currentData() is not None
        self.input.setEnabled(enabled)
        self.value_changed.emit()


class SystemConfigurationPage(QWidget):
    """展示配置分组、只读运行信息和不会生效的本次草稿。"""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        """建立页面壳体，第一次进入时再读取现有配置。

        Args:
            controller: 提供配置目录和现有机器名称的控制器。
            parent: 所属主窗口。

        Returns:
            返回示例：
                None  # 表单、折叠区和固定底栏已建立，尚未读取配置
        """
        super().__init__(parent)
        self.setObjectName("settings")
        self.controller = controller
        self.configuration_loaded = False
        self.baseline_values = {}
        self.editors = {}
        self.io_editors = {}
        self.path_inputs = {}
        self.path_copy_buttons = {}
        self.readonly_inputs = {}
        self.is_dirty = False

        # 创建固定标题和明确的预览说明。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 4, 24, 20)
        layout.setSpacing(16)
        self.title_label = TitleLabel("系统配置", self)
        self.title_label.setObjectName("pageTitle")
        layout.addWidget(self.title_label)
        self.preview_label = QLabel("界面预览 · 修改仅保留为本次草稿，不写入配置文件，也不影响设备。", self)
        self.preview_label.setObjectName("settingsPreviewNotice")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        # 在滚动区域内限制内容宽度，底部操作栏保持固定。
        self.scroll_area = ScrollArea(self)
        self.scroll_area.setObjectName("settingsScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_body = QWidget()
        scroll_body.setObjectName("settingsScrollBody")
        scroll_layout = QHBoxLayout(scroll_body)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget()
        self.content.setObjectName("settingsContent")
        self.content.setMaximumWidth(1000)
        self.content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 8)
        self.content_layout.setSpacing(22)
        scroll_layout.addStretch(1)
        scroll_layout.addWidget(self.content, 20)
        scroll_layout.addStretch(1)
        self.scroll_area.setWidget(scroll_body)
        layout.addWidget(self.scroll_area, 1)

        # 读取错误使用页面内说明，不以弹窗阻断页面导航。
        self.error_card = QFrame()
        self.error_card.setObjectName("settingsErrorCard")
        error_layout = QVBoxLayout(self.error_card)
        error_layout.setContentsMargins(18, 14, 18, 14)
        self.error_label = QLabel()
        self.error_label.setObjectName("settingsReadError")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.retry_button = PushButton("重新读取")
        self.retry_button.clicked.connect(self.load_configuration_preview)
        error_layout.addWidget(self.error_label)
        error_layout.addWidget(self.retry_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.content_layout.addWidget(self.error_card)
        self.error_card.hide()
        self.error_label.hide()

        # 先建立全部设置分组，读取成功后才开放草稿编辑。
        self.form = QWidget()
        self.form.setObjectName("settingsForm")
        self.form_layout = QVBoxLayout(self.form)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(22)
        self.content_layout.addWidget(self.form)
        self.build_configuration_form()
        self.content_layout.addStretch()
        self.form.setEnabled(False)
        self.build_action_bar(layout)
        self.apply_page_style()

    def build_group(self, title: str, description: str, target_layout: QVBoxLayout) -> QVBoxLayout:
        """向指定区域添加带标题、说明和圆角卡片的设置分组。

        Args:
            title: 分组标题。
            description: 简短说明。
            target_layout: 承载分组的垂直布局。

        Returns:
            返回示例：
                QVBoxLayout()  # 承载各设置行的白色卡片布局
        """
        # 将分组名称和简短说明放在白色卡片上方。
        group = QWidget()
        group.setObjectName("settingsGroup")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("settingsGroupTitle")
        group_layout.addWidget(heading)
        if description:
            caption = QLabel(description)
            caption.setObjectName("settingsGroupDescription")
            caption.setWordWrap(True)
            group_layout.addWidget(caption)

        # 白色卡片内部使用无间隔的设置行和细分隔线。
        card = QFrame()
        card.setObjectName("settingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        group_layout.addWidget(card)
        target_layout.addWidget(group)
        return card_layout

    def add_setting_row(self, layout: QVBoxLayout, title: str, description: str, control: QWidget) -> None:
        """添加左侧名称说明、右侧控件的设置行。

        Args:
            layout: 所属白色卡片布局。
            title: 设置名称。
            description: 设置的短说明。
            control: 编辑控件或只读显示控件。

        Returns:
            返回示例：
                None  # 设置行及必要的分隔线已加入卡片
        """
        # 从第二行开始加入轻量分隔线。
        if layout.count():
            divider = QFrame()
            divider.setObjectName("settingsDivider")
            divider.setFixedHeight(1)
            layout.addWidget(divider)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(20, 14, 20, 14)
        row_layout.setSpacing(24)
        labels = QVBoxLayout()
        labels.setSpacing(4)
        name_label = QLabel(title)
        name_label.setObjectName("settingsFieldTitle")
        name_label.setTextFormat(Qt.TextFormat.PlainText)
        name_label.setWordWrap(True)
        labels.addWidget(name_label)
        if description:
            detail_label = QLabel(description)
            detail_label.setObjectName("settingsFieldDescription")
            detail_label.setTextFormat(Qt.TextFormat.PlainText)
            detail_label.setWordWrap(True)
            labels.addWidget(detail_label)

        # 为右侧控件保留统一宽度和可访问名称。
        control.setFixedWidth(340)
        control.setAccessibleName(title)
        if isinstance(control, ConfigurationValueEditor):
            control.input.setAccessibleName(title)
            name_label.setBuddy(control.input)
            if control.mode_combo is not None:
                control.mode_combo.setAccessibleName(f"{title}填写方式")
        row_layout.addLayout(labels, 1)
        row_layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(row)

    def add_editable_field(
        self,
        layout: QVBoxLayout,
        key: str,
        title: str,
        description: str,
        value_kind: str = "text",
        unit: str = "",
        nullable: bool = False,
        null_label: str = "沿用设备 (null)",
    ) -> None:
        """创建一个可撤销草稿字段并登记其配置键。

        Args:
            layout: 所属白色卡片布局。
            key: 现有配置键。
            title: 设置名称。
            description: 简短说明。
            value_kind: text、integer 或 number。
            unit: 只读单位。
            nullable: 是否允许显式选择空值。
            null_label: 空值选项说明。

        Returns:
            返回示例：
                None  # 已登记输入控件并连接草稿状态更新
        """
        # 使用文本型数值控件，避免数值范围或精度自动修正现有配置。
        editor = ConfigurationValueEditor(value_kind, unit, nullable, null_label)
        self.editors[key] = editor
        self.add_setting_row(layout, title, description, editor)
        editor.value_changed.connect(self.update_draft_state)

    def build_configuration_form(self) -> None:
        """按采集、IO 通信、高级设置和运行路径顺序建立表单。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 现有参数对应的控件与默认折叠的高级区域已建立
        """
        # 展示全局相机草稿和只读采集规则。
        capture_layout = self.build_group("采集与识别", "全局相机参数，适用于各机器的采集。", self.form_layout)
        self.add_editable_field(
            capture_layout, "camera_exposure_time_us", "曝光时间", "选择沿用设备时保留 null。",
            "number", "μs", True,
        )
        self.add_editable_field(
            capture_layout, "camera_gain", "相机增益", "选择沿用设备时保留 null。", "number", "dB", True,
        )
        for key, title, description in (
            ("capture_window_ms", "单轮采集窗口", "业务规则：每轮 1 秒（1000 ms），此处只读。"),
            ("camera_pixel_format", "像素格式", "采集规则为 Mono8，此处只读展示实际配置。"),
        ):
            value_input = LineEdit()
            value_input.setReadOnly(True)
            value_input.setObjectName("settingsReadonlyInput")
            self.readonly_inputs[key] = value_input
            self.add_setting_row(capture_layout, title, description, value_input)

        # 通信分组明确指向 Modbus IO，不提供频率仪连接操作。
        communication_layout = self.build_group(
            "设备通信", "以下参数用于 Modbus IO。当前频率读取仍为占位实现。", self.form_layout,
        )
        self.add_editable_field(
            communication_layout, "modbus_serial_port", "IO 串口", "填写现场 IO 模块的串口名称。",
            nullable=True, null_label="未配置 (null)",
        )
        self.add_editable_field(
            communication_layout, "modbus_baudrate", "IO 波特率", "与 IO 模块的通信速率一致。", "integer", "bps",
        )
        self.add_editable_field(
            communication_layout, "modbus_unit_id", "IO 设备地址", "Modbus 从站地址，不是频率仪编号。", "integer",
        )

        # 高级入口默认收起，展开状态与草稿互不影响。
        self.advanced_toggle = PushButton("高级设置  ·  频闪、频率范围、超时与 DI 绑定    展开 ▾")
        self.advanced_toggle.setObjectName("settingsAdvancedToggle")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAccessibleName("展开或收起高级设置")
        self.advanced_toggle.clicked.connect(self.toggle_advanced_settings)
        self.form_layout.addWidget(self.advanced_toggle)
        self.advanced_body = QWidget()
        self.advanced_body.setObjectName("settingsAdvancedBody")
        advanced_layout = QVBoxLayout(self.advanced_body)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(20)
        self.form_layout.addWidget(self.advanced_body)
        self.build_advanced_fields(advanced_layout)
        self.advanced_body.hide()

        # 路径只读并支持复制，不添加路径选择或保存入口。
        paths_layout = self.build_group("运行路径", "只读展示配置来源和现有运行路径。", self.form_layout)
        for key, title, description in (
            ("configuration_file", "配置文件", "本页的读取来源。"),
            ("database_path", "业务数据库", "机器信息与测量结果。"),
            ("recovery_database_path", "运行数据库", "运行恢复与异常事件记录。"),
            ("evidence_directory", "证据图片目录", "采集与识别证据文件。"),
            ("mvs_development_directory", "MVS SDK 目录", "现有海康相机 SDK 开发目录。"),
            ("mvs_dll_directory", "MVS 动态库目录", "null 时按现有规则查找公共 MVS Runtime。"),
        ):
            control = QWidget()
            path_layout = QHBoxLayout(control)
            path_layout.setContentsMargins(0, 0, 0, 0)
            path_layout.setSpacing(8)
            path_input = LineEdit()
            path_input.setObjectName("settingsReadonlyInput")
            path_input.setReadOnly(True)
            path_input.setAccessibleName(title)
            copy_button = PushButton("复制")
            copy_button.setObjectName("settingsCopyButton")
            copy_button.setFixedWidth(58)
            copy_button.setAccessibleName(f"复制{title}")
            copy_button.clicked.connect(
                lambda checked=False, field=path_input: QApplication.clipboard().setText(field.text())
            )
            path_layout.addWidget(path_input, 1)
            path_layout.addWidget(copy_button)
            self.path_inputs[key] = path_input
            self.path_copy_buttons[key] = copy_button
            self.add_setting_row(paths_layout, title, description, control)

    def build_advanced_fields(self, layout: QVBoxLayout) -> None:
        """建立频闪、频率范围、处理期限和现有 DI 绑定区域。

        Args:
            layout: 默认折叠的高级区域布局。

        Returns:
            返回示例：
                None  # 高级字段已建立，未补充设备参数或新绑定
        """
        # 频闪保留三种状态，线路相关字段使用现场文本和显式空值。
        strobe_layout = self.build_group("相机频闪", "线路值请以现场 MVS 为准；本页不会检查或连接设备。", layout)
        self.strobe_combo = ComboBox()
        self.strobe_combo.addItem("沿用设备 (null)", userData=None)
        self.strobe_combo.addItem("关闭", userData=False)
        self.strobe_combo.addItem("开启", userData=True)
        self.strobe_combo.currentIndexChanged.connect(self.update_draft_state)
        self.add_setting_row(strobe_layout, "频闪输出", "开启时需填写实际线路选择、模式和信号源。", self.strobe_combo)
        for key, title in (
            ("camera_line_selector", "线路选择"),
            ("camera_line_mode", "线路模式"),
            ("camera_line_source", "线路信号源"),
        ):
            self.add_editable_field(strobe_layout, key, title, "保留现有文本，不预设设备枚举。", nullable=True)

        # 频率范围和处理期限只编辑已有配置键。
        frequency_layout = self.build_group("有效频率范围", "仅展示已有有效读数范围，不代表真实频率仪已接入。", layout)
        self.add_editable_field(frequency_layout, "minimum_frequency_hz", "有效频率下限", "保留现有下限。", "number", "Hz")
        self.add_editable_field(frequency_layout, "maximum_frequency_hz", "有效频率上限", "保留现有上限。", "number", "Hz")
        timeout_layout = self.build_group("识别与周期超时", "统一以毫秒显示现有期限。", layout)
        for key, title, description in (
            ("ocr_lock_wait_timeout_ms", "OCR 等待超时", "等待共享 OCR 识别资源的最长时间。"),
            ("ocr_result_timeout_ms", "OCR 识别超时", "获得识别资源后的处理期限。"),
            ("max_cycle_open_ms", "测量周期超时", "等待机器关闭信号的最长时间。"),
        ):
            self.add_editable_field(timeout_layout, key, title, description, "integer", "ms")

        # 绑定区只展示配置中已有的机器编号与通道，不提供机器增删。
        self.io_layout = self.build_group("机器与 DI 绑定", "通道使用从 0 开始的整数索引，仅编辑已有绑定。", layout)
        self.io_hint = QLabel("尚未读取绑定信息。")
        self.io_hint.setObjectName("settingsBindingHint")
        self.io_hint.setWordWrap(True)
        self.io_hint.setContentsMargins(20, 16, 20, 16)
        self.io_layout.addWidget(self.io_hint)

    def build_action_bar(self, layout: QVBoxLayout) -> None:
        """创建固定底栏的修改状态、撤销和待接入保存按钮。

        Args:
            layout: 页面最外层布局。

        Returns:
            返回示例：
                None  # 底栏不随表单滚动，保存始终禁用
        """
        # 将固定底栏与正文保持相同的最大宽度。
        self.action_bar = QFrame()
        self.action_bar.setObjectName("settingsActionBar")
        self.action_bar.setMaximumWidth(1000)
        self.action_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        action_layout = QHBoxLayout(self.action_bar)
        action_layout.setContentsMargins(18, 12, 18, 12)
        action_layout.setSpacing(12)
        labels = QVBoxLayout()
        labels.setSpacing(4)
        self.status_label = QLabel("尚未读取配置")
        self.status_label.setObjectName("settingsDraftStatus")
        self.status_hint = QLabel("草稿仅在本次打开期间保留")
        self.status_hint.setObjectName("settingsFieldDescription")
        labels.addWidget(self.status_label)
        labels.addWidget(self.status_hint)
        action_layout.addLayout(labels, 1)

        # 撤销只恢复内存基线；保存按钮没有连接任何操作。
        self.undo_button = PushButton("撤销修改")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self.undo_changes)
        self.save_button = PrimaryPushButton("保存配置 · 待接入")
        self.save_button.setEnabled(False)
        self.save_button.setToolTip("当前仅完成界面，配置校验、保存和生效逻辑尚未接入。")
        action_layout.addWidget(self.undo_button)
        action_layout.addWidget(self.save_button)
        container_layout = QHBoxLayout()
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addStretch(1)
        container_layout.addWidget(self.action_bar, 20)
        container_layout.addStretch(1)
        layout.addLayout(container_layout)

    def load_configuration_preview(self) -> None:
        """首次进入时读取配置，后续导航保留草稿，失败时允许重新读取。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 成功时回填实际值，失败时显示错误并保留页面壳体
        """
        # 已有读取基线时不因切换页面而覆盖草稿。
        if self.configuration_loaded:
            return
        settings, error_message = read_configuration_preview(self.controller.configuration_directory)
        if error_message:
            self.error_label.setText(error_message)
            self.error_label.show()
            self.error_card.show()
            self.form.setEnabled(False)
            self.status_label.setText("配置未读取")
            return

        # 保存独立基线，再回填所有输入与三态频闪选项。
        self.baseline_values = deepcopy(settings)
        self.build_io_bindings()
        self.undo_changes()
        self.populate_readonly_values()
        self.configuration_loaded = True
        self.form.setEnabled(True)
        self.error_card.hide()
        self.error_label.hide()
        self.update_draft_state()
        self.apply_page_style()

    def build_io_bindings(self) -> None:
        """以机器实际名称展示已有的机器编号到 DI 通道映射。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 已有绑定可编辑，空绑定或名称读取失败时有对应说明
        """
        # 没有绑定时保留空态，不为未绑定机器新增配置。
        bindings = self.baseline_values.get("io_machine_channels", {})
        if not bindings:
            self.io_hint.setText("尚未配置机器与 DI 通道的绑定。")
            return
        result = self.controller.list_machines()
        machines = result.data["machines"] if result.success else []
        names = {str(machine["id"]): machine["machine_name"] for machine in machines}

        # 只读取机器名称，找不到对应记录时保留配置中的机器编号。
        self.io_hint.setText("通道索引从 0 开始，例如 0 表示第一个 DI 通道。")
        if not result.success:
            self.io_hint.setText("机器名称暂时无法读取，以下保留原机器编号。通道索引从 0 开始。")
        for machine_id, channel in bindings.items():
            editor = ConfigurationValueEditor("integer")
            editor.set_value(channel)
            editor.value_changed.connect(self.update_draft_state)
            self.io_editors[machine_id] = editor
            title = names.get(machine_id) or f"机器 #{machine_id}（名称不可用）"
            self.add_setting_row(self.io_layout, title, f"机器编号 {machine_id} · DI 通道索引从 0 开始", editor)

    def populate_readonly_values(self) -> None:
        """展示实际采集配置和路径，未显式指定的运行库按现有规则显示。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 只读字段和复制按钮已更新，基线没有改变
        """
        # 显示实际只读值，不将异常配置偷偷修正为业务推荐值。
        for key, field in self.readonly_inputs.items():
            value = self.baseline_values.get(key)
            text = "未配置" if value is None else str(value)
            if key == "capture_window_ms" and value is not None:
                text = f"{value} ms"
                if normalize_preview_value(value, "number") == ("number", Decimal("1000")):
                    text = f"1 秒（{value} ms）"
            field.setText(text)
            field.setToolTip(text)

        # 复制路径只使用读取结果，不改写 YAML 中的相对路径或空值。
        paths = dict(self.baseline_values)
        paths["configuration_file"] = (self.controller.configuration_directory / "config.yaml").resolve()
        if paths.get("recovery_database_path") is None:
            paths["recovery_database_path"] = paths["database_path"].with_suffix(".recovery.sqlite3")
        for key, field in self.path_inputs.items():
            value = paths.get(key)
            text = "自动查找公共 MVS Runtime (null)" if key == "mvs_dll_directory" and value is None else str(value)
            field.setText(text)
            field.setCursorPosition(0)
            field.setToolTip(text)
            self.path_copy_buttons[key].setEnabled(value is not None)

    def get_draft_values(self) -> dict:
        """取得独立草稿字典，未展示字段继续保留读取时的值。

        Args:
            无外部参数。

        Returns:
            返回示例：
                {
                    "camera_gain": None,  # 当前可编辑字段的草稿值
                    "capture_window_ms": 1000,  # 未改动的只读配置
                    "io_machine_channels": {  # 已有机器绑定的草稿
                        "1": 0,  # 一号机器对应的零起始 DI 索引
                    },
                }
        """
        # 在基线副本上更新已展示字段，不补入未配置且仍为空的键。
        draft = deepcopy(self.baseline_values)
        for key, editor in self.editors.items():
            value = editor.get_value()
            if key in draft or value is not None:
                draft[key] = value
        strobe_value = deepcopy(self.strobe_combo.currentData())
        if "camera_strobe_enabled" in draft or strobe_value is not None:
            draft["camera_strobe_enabled"] = strobe_value

        # 只替换现有绑定的通道值，不增删机器编号。
        for machine_id, editor in self.io_editors.items():
            draft["io_machine_channels"][machine_id] = editor.get_value()
        return draft

    def update_draft_state(self) -> None:
        """比较规范化数值和原始文本，更新底栏与撤销按钮。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # dirty 状态反映当前草稿，保存按钮仍然禁用
        """
        # 未成功读取时不把初始化控件变化当作用户修改。
        if not self.configuration_loaded:
            return
        draft = self.get_draft_values()
        changed = any(
            normalize_preview_value(draft.get(key), editor.value_kind)
            != normalize_preview_value(self.baseline_values.get(key), editor.value_kind)
            for key, editor in self.editors.items()
        )

        # 频闪的 true、false、null 与 DI 的零索引分别比较。
        changed = changed or (
            normalize_preview_value(draft.get("camera_strobe_enabled"), "text")
            != normalize_preview_value(self.baseline_values.get("camera_strobe_enabled"), "text")
        )
        baseline_bindings = self.baseline_values.get("io_machine_channels", {})
        changed = changed or any(
            normalize_preview_value(editor.get_value(), "integer")
            != normalize_preview_value(baseline_bindings[machine_id], "integer")
            for machine_id, editor in self.io_editors.items()
        )
        self.is_dirty = changed
        self.status_label.setText("有未保存的修改 · 仅草稿" if changed else "无未保存的修改")
        self.status_label.setProperty("dirty", changed)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.undo_button.setEnabled(changed)
        self.save_button.setEnabled(False)

    def undo_changes(self) -> None:
        """恢复本次读取的完整草稿基线，保持高级区的展开状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 全部可编辑字段恢复原始值，null 和原始类型不丢失
        """
        # 回填普通字段和既有 DI 通道，控件内部阻止中间变更信号。
        for key, editor in self.editors.items():
            editor.set_value(self.baseline_values.get(key))
        for machine_id, editor in self.io_editors.items():
            editor.set_value(self.baseline_values["io_machine_channels"][machine_id])

        # 三态之外的异常原始值也保留为独立选项，不自动变成 false。
        value = self.baseline_values.get("camera_strobe_enabled")
        blocker = QSignalBlocker(self.strobe_combo)
        matching_index = next(
            (
                index for index in range(self.strobe_combo.count())
                if normalize_preview_value(self.strobe_combo.itemData(index), "text")
                == normalize_preview_value(value, "text")
            ),
            -1,
        )
        if matching_index < 0:
            self.strobe_combo.addItem(f"原始值：{value}", userData=value)
            matching_index = self.strobe_combo.count() - 1
        self.strobe_combo.setCurrentIndex(matching_index)
        del blocker
        self.update_draft_state()

    def toggle_advanced_settings(self) -> None:
        """根据高级入口的选中状态展开或收起完整高级分组。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 展开状态已改变，草稿内容保持不变
        """
        expanded = self.advanced_toggle.isChecked()
        self.advanced_body.setVisible(expanded)
        action = "收起 ▴" if expanded else "展开 ▾"
        self.advanced_toggle.setText(f"高级设置  ·  频闪、频率范围、超时与 DI 绑定    {action}")

    def apply_page_style(self) -> None:
        """为页面卡片、标题和 Fluent 输入追加统一浅色样式。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 本页样式已应用且不会影响其他页面
        """
        # 从独立样式文件读取页面颜色和圆角。
        stylesheet_path = Path(__file__).parents[1] / "styles" / "system_configuration_page.qss"
        stylesheet = stylesheet_path.read_text(encoding="utf-8")
        for name, color in COLORS.items():
            stylesheet = stylesheet.replace(f"@{name}", color)
        self.setStyleSheet(stylesheet)

        # 为有内部样式表的 Fluent 控件追加页面专属规则。
        styled_widgets = [
            self.title_label, self.advanced_toggle, *self.path_inputs.values(), *self.readonly_inputs.values()
        ]
        styled_widgets.extend(self.path_copy_buttons.values())
        styled_widgets.extend(editor.input for editor in (*self.editors.values(), *self.io_editors.values()))
        for widget in styled_widgets:
            setCustomStyleSheet(widget, stylesheet, stylesheet)
