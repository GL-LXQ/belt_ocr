"""Fluent 主窗口与页面导航。"""

from pathlib import Path

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    FluentWindow,
    NavigationItemPosition,
    SubtitleLabel,
)

from src.controller.controller import AppController
from ui.pages.abnormal_events_page import AbnormalEventsPage
from ui.pages.history_page import HistoryPage
from ui.pages.image_management_page import ImageManagementPage
from ui.pages.machines_page import MachinesPage
from ui.pages.realtime_page import RealtimePage
from ui.pages.system_configuration_page import SystemConfigurationPage
from ui.theme import COLORS, create_icon


# 保存页面导航文案和图标。
PAGES = {
    "realtime": ("实时监测", "查看当前机器的检测状态", FluentIcon.PLAY),
    "history": ("历史记录", "查看已保存的测量结果", FluentIcon.HISTORY),
    "abnormal_events": ("异常事件", "查看测量过程中的异常事件和原始信息", FluentIcon.INFO),
    "machines": ("机器管理", "管理检测机器的基本信息", FluentIcon.ROBOT),
    "images": ("图片管理", "按测量归组查看证据图片", FluentIcon.PHOTO),
    "settings": ("系统配置", "编辑系统配置，保存后下次监测生效", FluentIcon.SETTING),
    "logs": ("日志查看", "日志查看功能尚未开放", FluentIcon.DOCUMENT),
}

NAVIGATION_WIDTH = 176


class MainWindow(FluentWindow):
    """展示监测、历史、异常、机器、图片管理和系统配置页面。"""

    def __init__(self, controller: AppController):
        """初始化窗口、页面、导航、状态和时钟。

        Args:
            controller: 界面业务控制器。

        Returns:
            None  # 主窗口已初始化
        """
        super().__init__()
        self.controller = controller
        self.current_page_key = "realtime"

        # 配置窗口外观并创建正式页面。
        self.setup_window()
        self.realtime_page = RealtimePage(controller, self)
        self.history_page = HistoryPage(controller, self)
        self.abnormal_events_page = AbnormalEventsPage(controller, self)
        self.machines_page = MachinesPage(controller, self)
        self.images_page = ImageManagementPage(self, controller=controller)
        self.images_page.record_requested.connect(self.show_image_record)
        self.settings_page = SystemConfigurationPage(controller, self)
        self.pages = {
            "realtime": self.realtime_page,
            "history": self.history_page,
            "abnormal_events": self.abnormal_events_page,
            "machines": self.machines_page,
            "images": self.images_page,
            "settings": self.settings_page,
        }

        # 固定展开导航并隐藏折叠与返回按钮。
        self.navigationInterface.setExpandWidth(NAVIGATION_WIDTH)
        self.navigationInterface.setMenuButtonVisible(False)
        self.navigationInterface.setReturnButtonVisible(False)
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.expand(useAni=False)

        # 在页面入口上方显示不可点击的品牌。
        brand_item = self.navigationInterface.addItem(
            routeKey="beltvisionBrand",
            icon=create_icon("logo", "blue"),
            text="BeltVision",
            selectable=False,
            position=NavigationItemPosition.TOP,
        )
        brand_item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        brand_font = QFont(brand_item.font())
        brand_font.setPixelSize(14)
        brand_font.setWeight(QFont.Weight.DemiBold)
        brand_item.setFont(brand_font)
        brand_item.setLightTextColor(COLORS["text"])

        # 在品牌下方显示工作台分组标题。
        workspace_header = self.navigationInterface.addItemHeader(
            "工作台", NavigationItemPosition.TOP
        )
        header_font = QFont(workspace_header.font())
        header_font.setPixelSize(12)
        header_font.setWeight(QFont.Weight.Normal)
        workspace_header.setFont(header_font)
        workspace_header.setLightTextColor(COLORS["muted"])

        # 建立正式页面的顶部导航入口。
        for page_key in ("realtime", "history", "abnormal_events", "machines"):
            self.addSubInterface(
                self.pages[page_key], PAGES[page_key][2], PAGES[page_key][0]
            )

        # 保留底部导航顺序，图片管理使用独立的只读页面。
        for page_key in ("images", "settings", "logs"):
            page = self.create_placeholder_page(page_key) if page_key == "logs" else self.pages[page_key]
            self.pages[page_key] = page
            self.addSubInterface(
                page,
                PAGES[page_key][2],
                PAGES[page_key][0],
                position=NavigationItemPosition.BOTTOM,
            )
        self.stackedWidget.currentChanged.connect(self.update_current_page)

        # 设置标题栏和页面栈的项目样式标识。
        self.titleBar.setObjectName("mainTitleBar")
        self.titleBar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.stackedWidget.setObjectName("mainStackedWidget")

        # 隐藏标题栏中的重复品牌和页面标题。
        self.titleBar.iconLabel.hide()
        self.titleBar.titleLabel.hide()

        # 在现有标题栏显示时钟和连接状态。
        self.status_area = QWidget(self.titleBar)
        self.status_area.setObjectName("windowStatusArea")
        status_layout = QHBoxLayout(self.status_area)
        status_layout.setContentsMargins(0, 0, 18, 0)
        status_layout.setSpacing(8)
        self.clock_label = CaptionLabel(self.status_area)
        self.status_dot = QLabel(self.status_area)
        self.status_dot.setObjectName("windowStatusDot")
        self.status_dot.setFixedSize(8, 8)
        self.status_text = CaptionLabel("系统运行正常", self.status_area)
        self.connection_dot = QLabel(self.status_area)
        self.connection_dot.setObjectName("windowStatusDot")
        self.connection_dot.setFixedSize(8, 8)
        self.connection_text = CaptionLabel("服务已连接", self.status_area)
        for status_widget in (
            self.clock_label,
            self.status_dot,
            self.status_text,
            self.connection_dot,
            self.connection_text,
        ):
            status_layout.addWidget(status_widget)
        # 将状态区域放在弹性留白之后、窗口按钮之前。
        self.titleBar.hBoxLayout.insertWidget(
            self.titleBar.hBoxLayout.count() - 1,
            self.status_area,
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        self.set_system_status("系统运行正常")
        self.set_connection_status(True)
        self.setup_clock()
        self.apply_style()
        self.update_title_bar_geometry()

    def update_title_bar_geometry(self):
        """将标题栏放在固定导航的右侧。

        Args:
            无。

        Returns:
            None  # 标题栏位置和宽度已更新
        """
        self.titleBar.move(NAVIGATION_WIDTH, 0)
        self.titleBar.resize(
            max(0, self.width() - NAVIGATION_WIDTH), self.titleBar.height()
        )

    def resizeEvent(self, event):
        """在窗口缩放时更新标题栏位置。

        Args:
            event: 窗口缩放事件。

        Returns:
            None  # 标题栏已与导航边界对齐
        """
        super().resizeEvent(event)
        self.update_title_bar_geometry()

    def setup_window(self):
        """设置窗口标题、尺寸、图标和字体。

        Args:
            无。

        Returns:
            None  # 窗口外观已设置
        """
        self.setWindowTitle("BeltVision | 实时监测")
        self.setWindowIcon(create_icon("logo", "white"))
        self.resize(1600, 900)
        self.setMinimumSize(1320, 720)
        self.setMicaEffectEnabled(False)

        # 选择目标系统中的界面字体并居中窗口。
        families = QFontDatabase.families()
        family = (
            "Microsoft YaHei UI"
            if "Microsoft YaHei UI" in families
            else "Segoe UI"
        )
        font = QFont(family)
        font.setPixelSize(14)
        self.setFont(font)
        geometry = self.frameGeometry()
        geometry.moveCenter(self.screen().availableGeometry().center())
        self.move(geometry.topLeft())

    def create_placeholder_page(self, page_key: str) -> QWidget:
        """创建简短说明的占位页。

        Args:
            page_key: 页面标识。

        Returns:
            QWidget()  # 包含图标、标题和说明的占位页
        """
        title, description, icon = PAGES[page_key]
        page = QWidget(self)
        page.setObjectName(page_key)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        icon_label = QLabel(page)
        icon_label.setPixmap(icon.icon().pixmap(36, 36))
        layout.addStretch()
        layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(
            SubtitleLabel(title, page), alignment=Qt.AlignmentFlag.AlignHCenter
        )
        layout.addWidget(
            BodyLabel(description, page), alignment=Qt.AlignmentFlag.AlignHCenter
        )
        layout.addStretch()
        return page

    def switch_page(self, page_key: str):
        """切换到已有页面。

        Args:
            page_key: 目标页面标识。

        Returns:
            None  # 页面栈已切换
        """
        self.switchTo(self.pages[page_key])

    def show_image_record(self, session_id: str) -> None:
        """从图片管理切换到已有历史页并打开测量详情。

        Args:
            session_id: 图片所属的测量周期编号。

        Returns:
            None  # 历史页已打开，并沿用现有详情和人工复核入口
        """
        self.switch_page("history")
        self.history_page.show_record_detail(session_id)

    def update_current_page(self, page_index: int):
        """在页面变化后更新标题并读取对应列表。

        Args:
            page_index: 当前页面栈下标。

        Returns:
            None  # 标题和目标页面数据已更新
        """
        page = self.stackedWidget.widget(page_index)
        page_key = page.objectName()
        if page_key == self.current_page_key:
            return
        self.current_page_key = page_key
        self.setWindowTitle(f"BeltVision | {PAGES[page_key][0]}")
        if page_key == "history":
            self.history_page.refresh_history()
        elif page_key == "abnormal_events":
            self.abnormal_events_page.refresh_events()
        elif page_key == "settings":
            self.settings_page.load_configuration_preview()

    def setup_clock(self):
        """启动本地时间更新。

        Args:
            无。

        Returns:
            None  # 时钟定时器已启动
        """
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self.update_clock)
        self.update_clock()
        self.clock_timer.start()

    def update_clock(self):
        """更新标题栏日期时间。

        Args:
            无。

        Returns:
            None  # 时钟标签已更新
        """
        self.clock_label.setText(
            QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
        )

    def set_system_status(self, text: str, status: str = "normal"):
        """更新系统状态文字和颜色。

        Args:
            text: 状态说明。
            status: normal、warning 或 error。

        Returns:
            None  # 系统状态已更新
        """
        self.status_text.setText(text)
        self.status_dot.setProperty("status", status)
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)

    def set_connection_status(self, connected: bool):
        """更新服务连接状态。

        Args:
            connected: 服务是否已连接。

        Returns:
            None  # 连接状态已更新
        """
        self.connection_text.setText("服务已连接" if connected else "服务未连接")
        status = "normal" if connected else "disconnected"
        self.connection_dot.setProperty("status", status)
        self.connection_dot.style().unpolish(self.connection_dot)
        self.connection_dot.style().polish(self.connection_dot)

    def apply_style(self):
        """应用项目专属的界面样式。

        Args:
            无。

        Returns:
            None  # 样式已应用
        """
        stylesheet_path = Path(__file__).parent / "styles" / "main_window.qss"
        stylesheet = stylesheet_path.read_text(encoding="utf-8")
        for name, color in COLORS.items():
            stylesheet = stylesheet.replace(f"@{name}", color)
        self.setStyleSheet(stylesheet)

        # 在标题栏的 Fluent 样式后应用项目背景和无边框样式。
        self.titleBar.setStyleSheet(self.titleBar.styleSheet() + "\n" + stylesheet)

        # 在页面栈的 Fluent 样式后应用项目背景和无边框样式。
        self.stackedWidget.setStyleSheet(
            self.stackedWidget.styleSheet() + "\n" + stylesheet
        )

        # 在导航面板的 Fluent 样式后应用项目导航背景。
        navigation_panel = self.navigationInterface.panel
        navigation_panel.setStyleSheet(
            navigation_panel.styleSheet() + "\n" + stylesheet
        )

    def closeEvent(self, event):
        """等待监测结束后关闭窗口。

        Args:
            event: 窗口关闭事件。

        Returns:
            None  # 运行中等待结束，否则关闭窗口
        """
        if self.controller.is_monitoring_running().data["running"]:
            event.ignore()
            if not self.realtime_page.closing_requested:
                self.realtime_page.closing_requested = True
                self.controller.monitoring_finished_signal.connect(self.close)
                self.controller.stop_monitoring()
            return
        super().closeEvent(event)

        # 仅在监测结束且窗口确认关闭后释放图片任务。
        if event.isAccepted():
            self.images_page.shutdown()
