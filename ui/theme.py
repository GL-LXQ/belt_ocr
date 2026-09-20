"""统一界面颜色和矢量图标。"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


# 集中配置背景、文字、交互和状态颜色。
COLORS = {
    "title": "#1E2A38",
    "sidebar": "#1B2735",
    "hover": "#263849",
    "blue": "#2F7CF6",
    "background": "#F4F7FA",
    "white": "#FFFFFF",
    "border": "#E5EAF0",
    "text": "#182230",
    "muted": "#667085",
    "light": "#F8FAFC",
    "secondary": "#AEB8C4",
    "navigation": "#C5CED8",
    "caption": "#C7D0DB",
    "clock": "#B8C2CD",
    "status": "#E8EDF2",
    "divider": "#394654",
    "version": "#7F8B99",
    "normal": "#22C55E",
    "warning": "#FF9F1C",
    "error": "#EF4444",
    "close": "#E5484D",
}

# 保存各入口和窗口操作的 SVG 图形片段。
ICON_PATHS = {
    "realtime": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4M5 11h3l2-4 4 7 2-3h3"/>',
    "history": '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6m3-4v6l4 2"/>',
    "images": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>'
    ),
    "settings": '<path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 3v6m8 0v6m-6 0v6"/>',
    "devices": (
        '<rect x="3" y="4" width="18" height="12" rx="2"/>'
        '<path d="M8 20h8m-4-4v4"/><circle cx="12" cy="10" r="3"/>'
    ),
    "logs": '<path d="M6 3h9l4 4v14H6zM15 3v5h4M9 12h7m-7 4h7"/>',
    "minimize": '<path d="M5 12h14"/>',
    "maximize": '<rect x="5" y="5" width="14" height="14" rx="1"/>',
    "restore": '<path d="M9 5V3h12v12h-2"/><rect x="3" y="9" width="12" height="12" rx="1"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "logo": (
        '<rect x="1" y="1" width="22" height="22" rx="5" fill="{blue}" stroke="none"/>'
        '<path d="M7 6v12h6a3 3 0 0 0 0-6H7m0-6h5a3 3 0 0 1 0 6"/>'
    ),
}


def create_icon(name: str, color: str = "navigation") -> QIcon:
    """生成具有普通和选中状态的矢量图标。

    Args:
        name: ICON_PATHS 中的图标名称，例如 realtime。
        color: COLORS 中的普通状态颜色名称，默认使用 navigation。

    Returns:
        返回示例：
            QIcon()  # 包含普通和选中状态的图标
    """
    # 分别绘制普通颜色和选中后的白色图标。
    icon = QIcon()
    for state, color_name in ((QIcon.State.Off, color), (QIcon.State.On, "white")):
        # 填充图形中的主题颜色并组装 SVG 文本。
        drawing = ICON_PATHS[name].format(**COLORS)
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'fill="none" stroke="{COLORS[color_name]}" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round">{drawing}</svg>'
        )
        # 在透明画布上以双倍像素绘制图标。
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(source.encode())).render(painter)
        painter.end()

        # 设置设备像素比例，并保存当前选中状态的图标。
        pixmap.setDevicePixelRatio(2)
        icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
    return icon
