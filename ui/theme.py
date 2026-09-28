"""统一浅色主题和仍在使用的矢量图标。"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from qfluentwidgets import Theme, setTheme, setThemeColor


# 保存项目背景、文字和业务状态颜色。
COLORS = {
    "blue": "#2563EB",
    "background": "#F5F7FA",
    "white": "#FFFFFF",
    "border": "#E7ECF2",
    "text": "#182230",
    "muted": "#667085",
    "secondary": "#AEB8C4",
    "navigation": "#C5CED8",
    "normal": "#22C55E",
    "warning": "#FF9F1C",
    "error": "#EF4444",
}


# 保存机器操作、步骤和品牌图标的 SVG 图形。
ICON_PATHS = {
    "edit": '<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14z"/>',
    "delete": '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    "check": '<path d="m6 12 4 4 8-9" stroke-width="2.3"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "logo": (
        '<rect x="1" y="1" width="22" height="22" rx="5" '
        'fill="{blue}" stroke="none"/>'
        '<path d="M7 6v12h6a3 3 0 0 0 0-6H7m0-6h5a3 3 0 0 1 0 6"/>'
    ),
}


def initialize_theme() -> None:
    """设置应用的浅色 Fluent 主题。

    Args:
        无。

    Returns:
        None  # 浅色主题和主色已设置
    """
    setTheme(Theme.LIGHT)
    setThemeColor("#2563EB")


def create_icon(name: str, color: str = "navigation") -> QIcon:
    """生成普通与选中状态的矢量图标。

    Args:
        name: ICON_PATHS 中的图标名称。
        color: COLORS 中的普通状态颜色名称。

    Returns:
        QIcon()  # 包含普通和选中状态的图标
    """
    icon = QIcon()
    for state, color_name in ((QIcon.State.Off, color), (QIcon.State.On, "white")):
        # 填充图形颜色并组装 SVG 文本。
        drawing = ICON_PATHS[name].format(**COLORS)
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'fill="none" stroke="{COLORS[color_name]}" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round">{drawing}</svg>'
        )

        # 在透明画布中绘制高分辨率图标。
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(source.encode())).render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(2)
        icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
    return icon
