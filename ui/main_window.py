"""桌面主窗口、导航容器和自定义标题栏。"""

from pathlib import Path

from PySide6.QtCore import QDateTime, QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.service.machine_service import MachineService
from ui.theme import COLORS, create_icon
from ui.pages.realtime_page import RealtimePage
from ui.pages.devices_page import DevicesPage


# 按导航顺序配置页面标题和说明。
PAGES = {
    "realtime": ("实时监测", "实时查看皮带机的检测画面、状态和事件"),
    "history": ("历史记录", "查看历史检测记录和测量结果"),
    "images": ("图片管理", "查看和管理检测图片"),
    "settings": ("系统配置", "配置检测系统运行参数"),
    "devices": ("设备管理", "管理检测设备的基本信息，包括相机、频率仪等设备的绑定与启用状态。"),
    "logs": ("日志查看", "查看系统运行日志和异常信息"),
}


class TitleBar(QWidget):
    """处理标题栏拖动、双击和窗口控制。"""

    def __init__(self, window: QMainWindow):
        """构建标题、时钟、状态和窗口按钮。

        Args:
            window: 所属主窗口。

        Returns:
            返回示例：
                None  # 初始化标题栏
        """
        # 设置标题栏标识、高度和水平布局。
        super().__init__(window)
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 8, 0)
        layout.setSpacing(0)

        # 创建品牌图标、名称和当前页面标题。
        logo = QLabel()
        logo.setFixedSize(28, 28)
        logo.setPixmap(create_icon("logo", "white").pixmap(QSize(28, 28)))
        brand = QLabel("BeltVision")
        brand.setObjectName("brand")
        self.caption = QLabel("| 实时监测")
        self.caption.setObjectName("titleCaption")

        # 按顺序排列品牌区，并将右侧控件推向窗口右端。
        layout.addWidget(logo)
        layout.addSpacing(10)
        layout.addWidget(brand)
        layout.addSpacing(8)
        layout.addWidget(self.caption)
        layout.addStretch()

        # 创建时间和系统状态展示区域。
        self.clock_label = QLabel()
        self.clock_label.setObjectName("clock")
        self.clock_label.setMinimumWidth(148)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(8, 8)
        self.status_dot.setProperty("statusDot", True)
        self.status_text = QLabel("系统运行正常")
        self.status_text.setObjectName("systemStatus")

        # 排列时间、状态圆点和状态文字。
        layout.addWidget(self.clock_label)
        layout.addSpacing(18)
        layout.addWidget(self.status_dot)
        layout.addSpacing(8)
        layout.addWidget(self.status_text)
        layout.addSpacing(18)

        # 在状态区域和操作区域之间放置分隔线。
        divider = QFrame()
        divider.setObjectName("titleDivider")
        divider.setFixedSize(1, 20)
        layout.addWidget(divider)
        layout.addSpacing(8)

        # 创建设置、最小化、最大化和关闭按钮。
        self.control_buttons = {}
        for name, tooltip in (
            ("settings", "系统配置"),
            ("minimize", "最小化"),
            ("maximize", "最大化"),
            ("close", "关闭"),
        ):
            # 根据操作类型设置按钮样式标识和尺寸。
            button = QPushButton()
            object_name = "windowButton"
            if name == "settings":
                object_name = "settingsButton"
            elif name == "close":
                object_name = "closeButton"
            button.setObjectName(object_name)
            if name == "settings":
                button.setFixedSize(40, 40)
            else:
                button.setFixedSize(44, 48)
            # 设置图标、操作提示和鼠标样式，并保存按钮引用。
            button.setIcon(create_icon(name, "white"))
            button.setIconSize(QSize(18, 18))
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(button)
            self.control_buttons[name] = button

        # 将窗口控制按钮连接到对应操作。
        self.control_buttons["minimize"].clicked.connect(window.showMinimized)
        self.control_buttons["maximize"].clicked.connect(self.toggle_maximized)
        self.control_buttons["close"].clicked.connect(window.close)

        # 将文字和分隔线上的鼠标事件交给标题栏。
        for label in self.findChildren(QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        divider.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def toggle_maximized(self):
        """切换窗口最大化和还原状态。

        Args:
            无。

        Returns:
            返回示例：
                None  # 更新窗口状态
        """
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def mousePressEvent(self, event):
        """通过系统窗口管理器拖动标题栏。

        Args:
            event: 鼠标按下事件。

        Returns:
            返回示例：
                None  # 分发鼠标事件
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().windowHandle().startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击标题栏切换最大化状态。

        Args:
            event: 鼠标双击事件。

        Returns:
            返回示例：
                None  # 更新窗口状态
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    """提供六个页面的统一桌面容器。"""

    def __init__(self, machine_service: MachineService):
        """依次初始化窗口、页面、导航、时钟和样式。

        Args:
            machine_service: 设备业务服务。

        Returns:
            返回示例：
                None  # 初始化主窗口
        """
        # 初始化当前页面和导航按钮集合。
        super().__init__()
        self.machine_service = machine_service
        self.nav_buttons = {}
        self.current_page_key = "realtime"

        # 顺序创建窗口、组件和交互绑定，并启动时钟和应用主题。
        self.setup_window()
        self.setup_ui()
        self.setup_navigation()
        self.setup_clock()
        self.apply_style()

        # 为无边框窗口安装边缘缩放事件处理。
        QApplication.instance().installEventFilter(self)

    def closeEvent(self, event):
        """等待监测后台释放资源后关闭窗口。

        Args:
            event: Qt 窗口关闭事件。

        Returns:
            None  # 后台运行时延迟关闭，否则接受关闭事件
        """
        # 后台运行期间保留窗口，等待线程结束后再次关闭。
        page = self.page_stack.widget(0)
        service = page.monitoring_service
        if service is not None and service.isRunning():
            event.ignore()
            if not page.closing_requested:
                page.closing_requested = True
                service.finished.connect(self.close)
                page.stop_monitoring()
            return
        super().closeEvent(event)

    def setup_window(self):
        """配置窗口大小、字体、图标和居中位置。

        Args:
            无。

        Returns:
            返回示例：
                None  # 完成窗口配置
        """
        # 设置无边框窗口、窗口标题和尺寸约束。
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("BeltVision | 实时监测")
        self.setWindowIcon(create_icon("logo", "white"))
        self.resize(1600, 900)
        self.setMinimumSize(1280, 720)

        # 按系统可用字体设置默认字体。
        families = QFontDatabase.families()
        family = "Microsoft YaHei UI" if "Microsoft YaHei UI" in families else "Segoe UI"
        font = QFont(family)
        font.setPixelSize(13)
        self.setFont(font)

        # 将窗口放置在当前屏幕可用区域中央。
        geometry = self.frameGeometry()
        geometry.moveCenter(self.screen().availableGeometry().center())
        self.move(geometry.topLeft())

    def setup_ui(self):
        """构建顶部栏和左右分区的组件层级。

        Args:
            无。

        Returns:
            返回示例：
                None  # 完成组件布局
        """
        # 创建主窗口根容器和纵向布局。
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        # 将独立标题栏放在根布局顶部。
        self.title_bar = self.create_title_bar()
        root_layout.addWidget(self.title_bar)

        # 创建标题栏下方的水平布局，并放入固定宽度侧栏。
        body = QWidget()
        body.setObjectName("bodyWidget")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.sidebar = self.create_sidebar()
        body_layout.addWidget(self.sidebar)

        # 创建可伸缩的页面容器并装入页面栈。
        container = QWidget()
        container.setObjectName("pageContainer")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        self.page_stack = self.create_page_stack()
        container_layout.addWidget(self.page_stack)
        body_layout.addWidget(container, 1)
        root_layout.addWidget(body, 1)

    def create_title_bar(self) -> TitleBar:
        """创建独立标题栏。

        Args:
            无。

        Returns:
            返回示例：
                TitleBar(self)  # 顶部标题栏组件
        """
        return TitleBar(self)

    def create_sidebar(self) -> QWidget:
        """创建固定宽度导航和底部连接信息。

        Args:
            无。

        Returns:
            返回示例：
                QWidget()  # 包含六个导航按钮和服务状态的侧栏
        """
        # 设置侧栏宽度、导航区边距和按钮间距。
        sidebar = QWidget()
        sidebar.setObjectName("sideBar")
        sidebar.setFixedWidth(168)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 18, 10, 18)
        layout.setSpacing(6)

        # 一次创建全部导航按钮并保存页面映射。
        for page_key, (title, description) in PAGES.items():
            button = QPushButton()
            button.setProperty("navigation", True)
            button.setObjectName(f"nav_{page_key}")
            button.setCheckable(True)
            button.setFixedHeight(44)
            button.setAccessibleName(title)
            button.setToolTip(description)
            button.setCursor(Qt.CursorShape.PointingHandCursor)

            # 使用内部布局固定图标和文字的间距。
            button_layout = QHBoxLayout(button)
            button_layout.setContentsMargins(14, 0, 0, 0)
            button_layout.setSpacing(11)

            # 创建导航图标和文字，并放入按钮布局。
            icon = QLabel()
            icon.setObjectName("navigationIcon")
            icon.setFixedSize(18, 18)
            icon.setPixmap(create_icon(page_key).pixmap(QSize(18, 18)))
            text = QLabel(title)
            text.setObjectName("navigationText")
            button_layout.addWidget(icon)
            button_layout.addWidget(text)
            button_layout.addStretch()

            # 将子控件点击传递给导航按钮，并登记按钮引用。
            for label in (icon, text):
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.nav_buttons[page_key] = button
            layout.addWidget(button)

        # 将连接状态和版本信息固定在侧栏底部。
        layout.addStretch()
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(10, 0, 0, 0)
        footer_layout.setSpacing(8)

        # 创建连接状态行及其圆点和文字。
        connection_layout = QHBoxLayout()
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.setSpacing(8)
        self.connection_dot = QLabel()
        self.connection_dot.setProperty("statusDot", True)
        self.connection_dot.setFixedSize(8, 8)
        self.connection_text = QLabel("服务已连接")
        self.connection_text.setObjectName("serviceStatus")

        # 排列连接状态，并在下一行显示版本信息。
        connection_layout.addWidget(self.connection_dot)
        connection_layout.addWidget(self.connection_text)
        connection_layout.addStretch()
        footer_layout.addLayout(connection_layout)
        version = QLabel("v1.0.0")
        version.setObjectName("version")
        footer_layout.addWidget(version)
        layout.addWidget(footer)
        return sidebar

    def create_page_stack(self) -> QStackedWidget:
        """一次创建所有页面并放入堆叠容器。

        Args:
            无。

        Returns:
            返回示例：
                QStackedWidget()  # 持有六个页面的容器
        """
        # 创建实时监测页、设备管理页和其他占位页面。
        stack = QStackedWidget()
        stack.setObjectName("pageStack")
        for page_key in PAGES:
            if page_key == "realtime":
                stack.addWidget(RealtimePage(self.machine_service))
            elif page_key == "devices":
                stack.addWidget(DevicesPage(self.machine_service))
            else:
                stack.addWidget(self.create_placeholder_page(page_key))
        return stack

    def create_placeholder_page(self, page_key: str) -> QWidget:
        """创建带页面标题和空白卡片的占位页。

        Args:
            page_key: PAGES 中的页面标识。

        Returns:
            返回示例：
                QWidget()  # 包含页面头部和内容卡片的页面
        """
        # 获取页面文案并设置页面外围布局。
        title, description = PAGES[page_key]
        page = QWidget()
        page.setObjectName(page_key)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        # 设置页面头部的高度和水平间距。
        header = QWidget()
        header.setObjectName("pageHeader")
        header.setFixedHeight(64)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)

        # 在头部左侧放置主题蓝色图标。
        icon = QLabel()
        icon.setFixedSize(24, 24)
        icon.setPixmap(create_icon(page_key, "blue").pixmap(QSize(24, 24)))
        header_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        # 将页面标题和说明按两行排列。
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        subtitle = QLabel(description)
        subtitle.setObjectName("pageSubtitle")
        text_layout.addWidget(title_label)
        text_layout.addWidget(subtitle)

        # 将文案布局加入页面头部，再将头部放入页面。
        header_layout.addLayout(text_layout)
        header_layout.addStretch()
        layout.addWidget(header)

        # 用空白内容卡片填满剩余空间。
        content = QFrame()
        content.setObjectName("pageContent")
        layout.addWidget(content, 1)
        return page

    def setup_navigation(self):
        """绑定互斥导航和顶部设置入口。

        Args:
            无。

        Returns:
            返回示例：
                None  # 完成导航绑定并显示首页
        """
        # 创建互斥按钮组并绑定每个页面的导航操作。
        self.navigation_group = QButtonGroup(self)
        self.navigation_group.setExclusive(True)
        for page_key, button in self.nav_buttons.items():
            self.navigation_group.addButton(button)
            button.clicked.connect(lambda checked=False, target_page=page_key: self.switch_page(target_page))

        # 绑定顶部设置快捷入口，并选中默认首页。
        self.title_bar.control_buttons["settings"].clicked.connect(lambda: self.switch_page("settings"))
        self.switch_page("realtime")

    def switch_page(self, page_key: str):
        """同步页面、导航高亮和窗口标题。

        Args:
            page_key: realtime、history、images、settings、devices 或 logs。

        Returns:
            返回示例：
                None  # 更新当前页面且复用原有页面实例
        """
        # 切换页面栈，并同步当前页面标识和导航选中状态。
        title = PAGES[page_key][0]
        self.current_page_key = page_key
        self.page_stack.setCurrentIndex(list(PAGES).index(page_key))
        self.nav_buttons[page_key].setChecked(True)

        # 同步顶部页面名和系统窗口标题。
        self.title_bar.caption.setText(f"| {title}")
        self.setWindowTitle(f"BeltVision | {title}")

        # 同步导航图标的选中颜色。
        for key, button in self.nav_buttons.items():
            color = "white" if key == page_key else "navigation"
            label = button.findChild(QLabel, "navigationIcon")
            label.setPixmap(create_icon(key, color).pixmap(QSize(18, 18)))

    def setup_clock(self):
        """立即显示本地时间并启动每秒刷新。

        Args:
            无。

        Returns:
            返回示例：
                None  # 启动窗口所属的时钟定时器
        """
        # 设置一秒刷新间隔，立即显示时间并启动定时器。
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self.update_clock)
        self.update_clock()
        self.clock_timer.start()

    def update_clock(self):
        """刷新顶部本地日期时间。

        Args:
            无。

        Returns:
            返回示例：
                None  # 更新时间文字
        """
        self.title_bar.clock_label.setText(QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"))

    def set_system_status(self, text: str, status: str = "normal"):
        """更新系统状态文字和圆点颜色。

        Args:
            text: 显示的状态说明。
            status: normal、warning 或 error。

        Returns:
            返回示例：
                None  # 更新系统状态展示
        """
        # 校验业务层传入的状态名称。
        if status not in ("normal", "warning", "error"):
            raise ValueError(f"不支持的系统状态：{status}")
        # 更新状态文案和圆点属性，并重新应用圆点样式。
        self.title_bar.status_text.setText(text)
        dot = self.title_bar.status_dot
        dot.setProperty("status", status)
        dot.style().unpolish(dot)
        dot.style().polish(dot)
        dot.update()

    def set_connection_status(self, connected: bool):
        """更新服务连接文字和圆点颜色。

        Args:
            connected: 是否已连接服务。

        Returns:
            返回示例：
                None  # 更新服务连接展示
        """
        # 更新连接文案和圆点属性，并重新应用圆点样式。
        self.connection_text.setText("服务已连接" if connected else "服务未连接")
        self.connection_dot.setProperty("status", "normal" if connected else "disconnected")
        self.connection_dot.style().unpolish(self.connection_dot)
        self.connection_dot.style().polish(self.connection_dot)
        self.connection_dot.update()

    def apply_style(self):
        """从统一主题加载并应用窗口样式。

        Args:
            无。

        Returns:
            返回示例：
                None  # 应用主题样式
        """
        # 读取样式文件，将主题颜色占位符替换为统一色值。
        stylesheet = (Path(__file__).parent / "styles" / "main_window.qss").read_text(encoding="utf-8")
        for name, color in COLORS.items():
            stylesheet = stylesheet.replace(f"@{name}", color)
        self.setStyleSheet(stylesheet)

    def changeEvent(self, event):
        """在窗口状态变化后同步最大化按钮和外轮廓圆角。

        Args:
            event: 窗口状态变化事件。

        Returns:
            返回示例：
                None  # 更新按钮图标、提示和窗口圆角
        """
        # 根据窗口状态同步最大化按钮的图标、提示和无障碍名称。
        if event.type() == QEvent.Type.WindowStateChange:
            button = self.title_bar.control_buttons["maximize"]
            button.setIcon(create_icon("restore" if self.isMaximized() else "maximize", "white"))
            title = "还原窗口" if self.isMaximized() else "最大化"
            button.setToolTip(title)
            button.setAccessibleName(title)

            # 最大化时移除外轮廓圆角，还原时恢复圆角。
            root = self.centralWidget()
            root.setProperty("windowMaximized", self.isMaximized())
            self.apply_style()
        super().changeEvent(event)

    def eventFilter(self, watched, event):
        """在无边框窗口边缘启动系统缩放。

        Args:
            watched: 接收事件的对象。
            event: 待处理事件。

        Returns:
            返回示例：
                True  # 系统已接收缩放操作并停止后续分发
                False  # 未启动缩放，继续分发事件
        """
        # 仅处理当前普通窗口内的鼠标左键按下事件。
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and isinstance(watched, QWidget)
            and watched.window() is self
            and event.button() == Qt.MouseButton.LeftButton
            and not self.isMaximized()
        ):
            # 根据鼠标在窗口边缘的位置确定缩放方向。
            position = self.mapFromGlobal(event.globalPosition().toPoint())
            edges = Qt.Edge(0)
            if position.x() < 5:
                edges |= Qt.Edge.LeftEdge
            elif position.x() >= self.width() - 5:
                edges |= Qt.Edge.RightEdge
            if position.y() < 5:
                edges |= Qt.Edge.TopEdge
            elif position.y() >= self.height() - 5:
                edges |= Qt.Edge.BottomEdge

            # 将边缘拖动交给系统窗口管理器执行。
            if edges:
                return self.windowHandle().startSystemResize(edges)
        return super().eventFilter(watched, event)
