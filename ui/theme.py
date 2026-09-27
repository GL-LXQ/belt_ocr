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
    "edit": '<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14z"/>',
    "delete": '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    "pulse": '<path d="M2 12h5l3-9 4 18 3-9h5"/>',
    "play": '<path d="m9 5 10 7-10 7z" fill="{white}" stroke="none"/>',
    "pause": '<path d="M8 6v12m8-12v12" stroke-width="3"/>',
    "hourglass": '<path d="M7 3h10M7 21h10M8 3v5l8 8v5M16 3v5l-8 8v5M9 17h6"/>',
    "check": '<path d="m6 12 4 4 8-9" stroke-width="2.3"/>',
    "camera": '<path d="M8 6 10 3h5l2 3h4v14H3V6z"/><circle cx="12" cy="12" r="4"/>',
    "alarm": '<circle cx="12" cy="13" r="8"/><path d="m3 5 3-3m12 0 3 3M7 21l-2 2m12-2 2 2M12 8v5l3 2"/>',
    "warning_mark": '<path d="M12 5v9m0 4v1" stroke-width="2.5"/>',
    "fan": (
        '<circle cx="12" cy="12" r="2"/>'
        '<path d="M11 9C2 7 8 0 13 3c3 2 1 5-1 7M15 12c7-6 11 2 6 6c-3 2-5-1-7-4'
        'M11 15c2 9-7 8-8 3c0-4 4-4 7-4" fill="{blue}" stroke="none"/>'
    ),
    "realtime": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4M5 11h3l2-4 4 7 2-3h3"/>',
    "history": '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6m3-4v6l4 2"/>',
    "abnormal_events": '<path d="M12 5v9m0 4v1" stroke-width="2.5"/>',
    "images": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>'
    ),
    "settings": '<path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 3v6m8 0v6m-6 0v6"/>',
    "machines": (
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
