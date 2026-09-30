"""绘制机器卡片中的机械皮带场景并响应现有动画控制。"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from enum import Enum, auto
from html import escape

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QSizePolicy, QWidget

SCENE_WIDTH = 1000.0
SCENE_HEIGHT = 480.0
Point = tuple[float, float]
TEXT_FONT_FAMILY = (
    "Microsoft YaHei"
    if sys.platform == "win32"
    else "PingFang SC" if sys.platform == "darwin" else "Noto Sans CJK SC"
)
MONO_FONT_FAMILY = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


@dataclass(frozen=True)
class BeltVisualState:
    """保存绘图所需的展开比例、子动画开关和运动相位。"""

    extension: float = 0.0
    capturing: bool = False
    frequency_listening: bool = False
    travel: float = 0.0
    scan_phase: float = 0.0
    frequency_phase: float = 0.0


@dataclass(frozen=True)
class BeltGeometry:
    """保存大小滚筒和皮带厚度的场景坐标。"""

    left: float
    right: float
    center_y: float = 263.0
    small_radius: float = 34.0
    large_radius: float = 66.0
    depth_x: float = 46.0
    depth_y: float = -33.0

    def get_tangents(self) -> tuple[Point, Point, Point, Point]:
        """计算皮带与滚筒的四个外公切点。

        Args:
            无外部参数。

        Returns:
            返回示例：
                (
                    (313.09, 229.05),  # 上左切点
                    (641.21, 197.31),  # 上右切点
                    (641.21, 328.69),  # 下右切点
                    (313.09, 296.95),  # 下左切点
                )
        """
        tangent_ratio = (self.large_radius - self.small_radius) / (
            self.right - self.left
        )
        tangent_height = math.sqrt(1.0 - tangent_ratio * tangent_ratio)
        small_radius, large_radius = self.small_radius, self.large_radius
        return (
            (
                self.left - small_radius * tangent_ratio,
                self.center_y - small_radius * tangent_height,
            ),
            (
                self.right - large_radius * tangent_ratio,
                self.center_y - large_radius * tangent_height,
            ),
            (
                self.right - large_radius * tangent_ratio,
                self.center_y + large_radius * tangent_height,
            ),
            (
                self.left - small_radius * tangent_ratio,
                self.center_y + small_radius * tangent_height,
            ),
        )

    def project_back_point(self, point: Point) -> Point:
        """将正面坐标平移到机械背面。

        Args:
            point: 正面坐标。

        Returns:
            返回示例：
                (
                    356.0,  # 背面横坐标
                    197.0,  # 背面纵坐标
                )
        """
        return (
            point[0] + self.depth_x,
            point[1] + self.depth_y,
        )


class SvgDrawing:
    """组装 Qt SVG 支持的基本图形，不使用滤镜、脚本或外部资源。"""

    def __init__(self) -> None:
        """初始化 SVG 片段列表。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        self.parts: list[str] = []

    def append_markup(self, markup: str) -> None:
        """追加 SVG 图形片段。

        Args:
            markup: 待追加的 SVG 片段。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        self.parts.append(markup)

    def draw_path(
        self,
        path_data: str,
        fill: str = 'none',
        stroke: str = 'none',
        width: float = 1.0,
        opacity: float = 1.0,
    ) -> None:
        """追加 SVG 路径。

        Args:
            path_data: SVG 路径指令。
            fill: 填充颜色或渐变。
            stroke: 描边颜色。
            width: 描边宽度。
            opacity: 图形透明度。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        self.append_markup(
            f'<path d="{path_data}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linejoin="round" '
            f'stroke-linecap="round" opacity="{opacity:.3f}"/>'
        )

    def draw_polygon(
        self,
        points: list[Point],
        fill: str,
        stroke: str = 'none',
        width: float = 1.0,
        opacity: float = 1.0,
    ) -> None:
        """追加多边形图形。

        Args:
            points: 多边形顶点。
            fill: 填充颜色或渐变。
            stroke: 描边颜色。
            width: 描边宽度。
            opacity: 图形透明度。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        coordinates = " ".join(
            f"{horizontal_position:.3f},{vertical_position:.3f}"
            for horizontal_position, vertical_position in points
        )
        self.append_markup(
            f'<polygon points="{coordinates}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linejoin="round" '
            f'opacity="{opacity:.3f}"/>'
        )

    def draw_rectangle(
        self,
        horizontal_position: float,
        vertical_position: float,
        rectangle_width: float,
        rectangle_height: float,
        fill: str,
        radius: float = 0,
        stroke: str = 'none',
        width: float = 1.0,
    ) -> None:
        """追加矩形图形。

        Args:
            horizontal_position: 横坐标。
            vertical_position: 纵坐标。
            rectangle_width: 矩形宽度。
            rectangle_height: 矩形高度。
            fill: 填充颜色或渐变。
            radius: 圆角或滚筒半径。
            stroke: 描边颜色。
            width: 描边宽度。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        self.append_markup(
            f'<rect x="{horizontal_position:.3f}" y="{vertical_position:.3f}" '
            f'width="{rectangle_width:.3f}" '
            f'height="{rectangle_height:.3f}" rx="{radius}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{width}"/>'
        )

    def draw_line(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        stroke: str,
        width: float = 1.0,
        opacity: float = 1.0,
    ) -> None:
        """追加线段图形。

        Args:
            start_x: 起点横坐标。
            start_y: 起点纵坐标。
            end_x: 终点横坐标。
            end_y: 终点纵坐标。
            stroke: 描边颜色。
            width: 描边宽度。
            opacity: 图形透明度。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        self.draw_path(
            f"M{start_x:.3f},{start_y:.3f} L{end_x:.3f},{end_y:.3f}",
            stroke=stroke,
            width=width,
            opacity=opacity,
        )

    def draw_ellipse(
        self,
        center_x: float,
        center_y: float,
        radius_x: float,
        radius_y: float,
        fill: str,
        stroke: str = 'none',
        width: float = 1.0,
        opacity: float = 1.0,
    ) -> None:
        """追加椭圆图形。

        Args:
            center_x: 中心横坐标。
            center_y: 中心纵坐标。
            radius_x: 横向半径。
            radius_y: 纵向半径。
            fill: 填充颜色或渐变。
            stroke: 描边颜色。
            width: 描边宽度。
            opacity: 图形透明度。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        self.append_markup(
            f'<ellipse cx="{center_x:.3f}" cy="{center_y:.3f}" rx="{radius_x:.3f}" '
            f'ry="{radius_y:.3f}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{width}" opacity="{opacity:.3f}"/>'
        )

    def draw_text(
        self,
        horizontal_position: float,
        vertical_position: float,
        value: str,
        size: float = 12,
        fill: str = '#667085',
        weight: int = 400,
        anchor: str = 'start',
        monospaced: bool = False,
    ) -> None:
        """追加转义后的文字。

        Args:
            horizontal_position: 横坐标。
            vertical_position: 纵坐标。
            value: 显示文字。
            size: 文字大小。
            fill: 填充颜色或渐变。
            weight: 文字字重。
            anchor: 文字对齐方式。
            monospaced: 是否使用等宽字体。

        Returns:
            返回示例：
                None  # 图形内容已更新
        """
        family = MONO_FONT_FAMILY if monospaced else TEXT_FONT_FAMILY
        self.append_markup(
            f'<text x="{horizontal_position:.3f}" y="{vertical_position:.3f}" '
            f'font-family="{family}" '
            f'font-size="{size}" fill="{fill}" font-weight="{weight}" '
            f'text-anchor="{anchor}">{escape(value)}</text>'
        )


SVG_DEFS = """
<defs>
  <linearGradient id="frame" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#f3f6f8"/>
    <stop offset="0.12" stop-color="#dce3e9"/>
    <stop offset="0.7" stop-color="#b2bec9"/>
    <stop offset="1" stop-color="#8e9ba9"/>
  </linearGradient>
  <linearGradient id="post" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#a8b4bf"/>
    <stop offset="0.25" stop-color="#edf1f4"/>
    <stop offset="0.7" stop-color="#c5cfd7"/>
    <stop offset="1" stop-color="#8b98a5"/>
  </linearGradient>
  <linearGradient id="dark" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#566674"/>
    <stop offset="0.45" stop-color="#344451"/>
    <stop offset="1" stop-color="#202d38"/>
  </linearGradient>
  <linearGradient id="rubber" x1="0" y1="0" x2="0.4" y2="1">
    <stop offset="0" stop-color="#202a33"/>
    <stop offset="0.22" stop-color="#414a50"/>
    <stop offset="0.6" stop-color="#262e35"/>
    <stop offset="1" stop-color="#151c23"/>
  </linearGradient>
  <linearGradient id="beltTop" x1="0" y1="0" x2="0.25" y2="1">
    <stop offset="0" stop-color="#50585c"/>
    <stop offset="0.2" stop-color="#343e44"/>
    <stop offset="0.66" stop-color="#283139"/>
    <stop offset="1" stop-color="#1d252c"/>
  </linearGradient>
  <linearGradient id="returnBelt" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#41494e"/>
    <stop offset="1" stop-color="#171f27"/>
  </linearGradient>
  <linearGradient id="rim" x1="0" y1="0" x2="1" y2="0.9">
    <stop offset="0" stop-color="#64727e"/>
    <stop offset="0.22" stop-color="#f8fafb"/>
    <stop offset="0.45" stop-color="#a4b1bb"/>
    <stop offset="0.72" stop-color="#e7edf1"/>
    <stop offset="1" stop-color="#677784"/>
  </linearGradient>
  <radialGradient id="face" cx="0.34" cy="0.23" r="0.9">
    <stop offset="0" stop-color="#f9fbfc"/>
    <stop offset="0.32" stop-color="#d6dee4"/>
    <stop offset="0.7" stop-color="#a2afba"/>
    <stop offset="1" stop-color="#778692"/>
  </radialGradient>
  <linearGradient id="hub" x1="0" y1="0" x2="0.65" y2="1">
    <stop offset="0" stop-color="#fbfcfd"/>
    <stop offset="0.5" stop-color="#bcc7d0"/>
    <stop offset="1" stop-color="#657583"/>
  </linearGradient>
  <radialGradient id="shadow">
    <stop offset="0" stop-color="#324455" stop-opacity="0.19"/>
    <stop offset="0.7" stop-color="#526273" stop-opacity="0.055"/>
    <stop offset="1" stop-color="#526273" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="glass" cx="0.35" cy="0.3" r="0.8">
    <stop offset="0" stop-color="#81c7e7"/>
    <stop offset="0.3" stop-color="#224f70"/>
    <stop offset="0.7" stop-color="#102536"/>
    <stop offset="1" stop-color="#08131f"/>
  </radialGradient>
  <linearGradient id="beam" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#64b5fa" stop-opacity="0.015"/>
    <stop offset="1" stop-color="#60baff" stop-opacity="0.24"/>
  </linearGradient>
</defs>
"""


def draw_box(
    drawing: SvgDrawing,
    horizontal_position: float,
    vertical_position: float,
    rectangle_width: float,
    rectangle_height: float,
    depth_x: float = 13,
    depth_y: float = -9,
    front: str = 'url(#frame)',
    side: str = '#8795a1',
    top: str = '#e3e9ee',
) -> None:
    """绘制金属件的正面、侧面和顶面。

    Args:
        drawing: 当前 SVG 绘图内容。
        horizontal_position: 横坐标。
        vertical_position: 纵坐标。
        rectangle_width: 矩形宽度。
        rectangle_height: 矩形高度。
        depth_x: 背面横向偏移。
        depth_y: 背面纵向偏移。
        front: 正面填充。
        side: 侧面填充。
        top: 顶面填充。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """
    drawing.draw_polygon(
        [
            (horizontal_position, vertical_position),
            (horizontal_position + depth_x, vertical_position + depth_y),
            (
                horizontal_position + rectangle_width + depth_x,
                vertical_position + depth_y,
            ),
            (horizontal_position + rectangle_width, vertical_position),
        ],
        top,
        "#a9b5bf",
        0.7,
    )
    drawing.draw_polygon(
        [
            (horizontal_position + rectangle_width, vertical_position),
            (
                horizontal_position + rectangle_width + depth_x,
                vertical_position + depth_y,
            ),
            (
                horizontal_position + rectangle_width + depth_x,
                vertical_position + rectangle_height + depth_y,
            ),
            (
                horizontal_position + rectangle_width,
                vertical_position + rectangle_height,
            ),
        ],
        side,
    )
    drawing.draw_rectangle(
        horizontal_position,
        vertical_position,
        rectangle_width,
        rectangle_height,
        front,
        stroke="#91a0ad",
        width=0.7,
    )
    drawing.draw_line(
        horizontal_position + 1,
        vertical_position + 1,
        horizontal_position + rectangle_width - 1,
        vertical_position + 1,
        "#ffffff",
        1.1,
        0.75,
    )


def draw_bolt(
    drawing: SvgDrawing,
    horizontal_position: float,
    vertical_position: float,
    small_radius: float = 3.2,
) -> None:
    """绘制螺栓端面。

    Args:
        drawing: 当前 SVG 绘图内容。
        horizontal_position: 横坐标。
        vertical_position: 纵坐标。
        small_radius: 螺栓半径。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """
    drawing.draw_ellipse(
        horizontal_position,
        vertical_position,
        small_radius,
        small_radius,
        "#778591",
        "#e5ecf1",
        0.7,
    )
    drawing.draw_line(
        horizontal_position - small_radius * 0.4,
        vertical_position - 0.25,
        horizontal_position + small_radius * 0.4,
        vertical_position + 0.25,
        "#263744",
        0.9,
    )


def draw_frame(drawing: SvgDrawing, geometry: BeltGeometry) -> None:
    """绘制底座、支撑脚、滑台、电机和相机支架。

    Args:
        drawing: 当前 SVG 绘图内容。
        geometry: 皮带几何参数。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """

    # 绘制底座下的柔和阴影。
    drawing.draw_ellipse(512, 409, 419, 35, "url(#shadow)")
    # 绘制支撑脚和底座。
    for horizontal_position, vertical_position in [
        (210, 362),
        (825, 362),
        (164, 395),
        (779, 395),
    ]:
        drawing.draw_ellipse(
            horizontal_position + 3,
            vertical_position + 10,
            25,
            8,
            "#33414c",
            opacity=0.1,
        )
        drawing.draw_ellipse(
            horizontal_position, vertical_position + 5, 17, 6, "#26313c"
        )
        drawing.draw_rectangle(
            horizontal_position - 11,
            vertical_position - 17,
            22,
            21,
            "url(#post)",
            radius=2,
        )
        drawing.draw_ellipse(
            horizontal_position, vertical_position + 1, 13, 4, "#94a2ae"
        )
        for groove_position in (vertical_position - 12, vertical_position - 7):
            drawing.draw_line(
                horizontal_position - 10,
                groove_position,
                horizontal_position + 10,
                groove_position,
                "#7c8b98",
                1,
            )

    # 绘制底座横梁和固定螺栓。
    draw_box(drawing, 128, 350, 721, 29, 46, -33)
    drawing.draw_rectangle(140, 360, 695, 5, "#637581", radius=2)
    drawing.draw_line(140, 366, 834, 366, "#eaf0f4", 1.2)
    drawing.draw_rectangle(141, 373, 694, 2, "#92a0ac")
    drawing.draw_text(467, 372, "BELT  /  VISION", 8.8, "#4b5c6c", 500, monospaced=True)
    for horizontal_position in (153, 306, 688, 822):
        draw_bolt(drawing, horizontal_position, 355, 2.4)

    # 绘制导轨和随左滚筒移动的张紧滑台。
    draw_box(drawing, 161, 338, 646, 8, 27, -19, front="url(#post)")
    drawing.draw_line(164, 340, 806, 340, "#f7f9fa", 1.4)
    drawing.draw_line(165, 345, 805, 345, "#627585", 1)
    drawing.draw_line(169, 325, 496, 325, "#71818e", 5)
    drawing.draw_line(169, 324, 496, 324, "#dce3e8", 1.3)
    for horizontal_position in range(178, 494, 7):
        drawing.draw_line(
            horizontal_position, 322, horizontal_position - 2, 328, "#526474", 0.7
        )

    # 绘制左侧张紧滑台和轴座。
    draw_box(drawing, geometry.left - 43, 327, 86, 13, 26, -19, front="url(#dark)")
    draw_box(drawing, geometry.left - 21, 265, 42, 63, 22, -16, front="url(#post)")
    drawing.draw_rectangle(geometry.left - 12, 278, 24, 29, "#8898a5", radius=3)
    draw_bolt(drawing, geometry.left - 29, 332, 2.5)
    draw_bolt(drawing, geometry.left + 29, 332, 2.5)
    drawing.draw_ellipse(159, 325, 8, 8, "url(#hub)", "#758593", 1)
    drawing.draw_ellipse(159, 325, 3.2, 3.2, "#485b6b")

    # 绘制右轴座和后置电机。
    draw_box(drawing, geometry.right - 28, 300, 61, 40, 30, -21, front="url(#post)")
    draw_box(
        drawing,
        geometry.right + 18,
        278,
        81,
        43,
        25,
        -18,
        front="url(#dark)",
        side="#283d4d",
        top="#617483",
    )
    for fin_index in range(7):
        drawing.draw_rectangle(
            geometry.right + 30 + fin_index * 8,
            287,
            3,
            26,
            "#203440",
            radius=1,
        )
        drawing.draw_line(
            geometry.right + 31 + fin_index * 8,
            287,
            geometry.right + 31 + fin_index * 8,
            311,
            "#6c8190",
            0.7,
        )
    drawing.draw_rectangle(geometry.right + 54, 280, 28, 7, "#cad6df", radius=1)

    # 绘制相机立柱、横臂和线缆。
    draw_box(drawing, 571, 320, 57, 14, 23, -16, front="url(#dark)")
    draw_box(drawing, 584, 69, 23, 252, 13, -9, front="url(#post)")
    drawing.draw_rectangle(593, 75, 5, 235, "#8494a1", radius=2)
    drawing.draw_line(598, 76, 598, 307, "#eff3f6", 1)

    # 绘制相机横臂和连接螺栓。
    draw_box(drawing, 479, 81, 130, 18, 13, -9)
    drawing.draw_rectangle(486, 88, 116, 4, "#8e9eab", radius=1)
    for horizontal_position, vertical_position in (
        (590, 91),
        (599, 91),
        (580, 325),
        (617, 325),
    ):
        draw_bolt(drawing, horizontal_position, vertical_position, 2.7)
    drawing.draw_path(
        "M519,91 C518,50 559,48 597,55 C625,59 628,75 627,103 "
        "L627,292 Q627,315 639,321",
        stroke="#334858",
        width=3.2,
    )
    drawing.draw_path("M519,91 C518,50 559,48 597,55", stroke="#8c9ca8", width=0.8)


def build_belt_outline(geometry: BeltGeometry) -> str:
    """生成绕过大小滚筒的皮带轮廓路径。

    Args:
        geometry: 皮带几何参数。

    Returns:
        返回示例：
            "M0,0 L1,0 A1,1 0 1 1 1,1 L0,1 A1,1 0 0 1 0,0 Z"  # 路径格式
    """
    top_left, top_right, bottom_right, bottom_left = geometry.get_tangents()
    small_radius, large_radius = geometry.small_radius, geometry.large_radius
    return (
        f"M{top_left[0]:.3f},{top_left[1]:.3f} L{top_right[0]:.3f},{top_right[1]:.3f} "
        f"A{large_radius},{large_radius} 0 1 1 "
        f"{bottom_right[0]:.3f},{bottom_right[1]:.3f} "
        f"L{bottom_left[0]:.3f},{bottom_left[1]:.3f} "
        f"A{small_radius},{small_radius} 0 0 1 {top_left[0]:.3f},{top_left[1]:.3f} Z"
    )


def draw_belt_shell(
    drawing: SvgDrawing,
    geometry: BeltGeometry,
    state: BeltVisualState,
    detailed: bool,
) -> None:
    """绘制皮带上下表面、绕轮曲面和移动印字。

    Args:
        drawing: 当前 SVG 绘图内容。
        geometry: 皮带几何参数。
        state: 当前帧的纯视觉参数。
        detailed: 是否绘制机械细纹。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """
    top_left, top_right, bottom_right, bottom_left = geometry.get_tangents()
    drawing.append_markup(
        f'<g transform="translate({geometry.depth_x},{geometry.depth_y})">'
    )
    drawing.draw_path(build_belt_outline(geometry), stroke="#343e47", width=7)
    drawing.append_markup("</g>")
    drawing.draw_polygon(
        [
            bottom_left,
            bottom_right,
            geometry.project_back_point(bottom_right),
            geometry.project_back_point(bottom_left),
        ],
        "url(#returnBelt)",
    )

    # 绘制两端绕轮曲面。
    tangent_ratio = (geometry.large_radius - geometry.small_radius) / (
        geometry.right - geometry.left
    )
    top_angle = math.atan2(
        -math.sqrt(1 - tangent_ratio * tangent_ratio), -tangent_ratio
    )
    bottom_angle = -top_angle
    for center_x, radius, start, end in (
        (geometry.right, geometry.large_radius, top_angle, bottom_angle),
        (geometry.left, geometry.small_radius, bottom_angle, top_angle + 2 * math.pi),
    ):
        points = [
            (
                center_x + radius * math.cos(start + (end - start) * index / 28),
                geometry.center_y
                + radius * math.sin(start + (end - start) * index / 28),
            )
            for index in range(29)
        ]
        drawing.draw_polygon(
            points
            + [
                geometry.project_back_point(front_point)
                for front_point in reversed(points)
            ],
            "url(#rubber)",
        )
        if detailed:
            phase = state.travel / radius
            # 按滚筒半径计算曲面纹理相位。
            count = 34 if radius > 40 else 19
            for index in range(count):
                angle = (index * math.tau / count + phase - start) % math.tau + start
                if angle <= end:
                    front_point = (
                        center_x + radius * math.cos(angle),
                        geometry.center_y + radius * math.sin(angle),
                    )
                    back_point = geometry.project_back_point(front_point)
                    drawing.draw_line(
                        front_point[0],
                        front_point[1],
                        back_point[0],
                        back_point[1],
                        "#a4adb3",
                        0.65,
                        0.10,
                    )

    drawing.draw_polygon(
        [
            top_left,
            top_right,
            geometry.project_back_point(top_right),
            geometry.project_back_point(top_left),
        ],
        "url(#beltTop)",
        "#1f2931",
        0.6,
    )
    drawing.draw_line(
        *geometry.project_back_point(top_left),
        *geometry.project_back_point(top_right),
        "#7d878e",
        1,
        0.7,
    )

    # 将印字坐标映射到皮带上表面。
    length = math.hypot(top_right[0] - top_left[0], top_right[1] - top_left[1])
    surface_direction_x, surface_direction_y = (top_right[0] - top_left[0]) / length, (
        top_right[1] - top_left[1]
    ) / length
    depth = math.hypot(geometry.depth_x, geometry.depth_y)
    depth_direction_x, depth_direction_y = (
        -geometry.depth_x / depth,
        -geometry.depth_y / depth,
    )
    origin_x, origin_y = geometry.project_back_point(top_left)
    drawing.append_markup(
        f'<g transform="matrix({surface_direction_x:.6f},{surface_direction_y:.6f},'
        f'{depth_direction_x:.6f},{depth_direction_y:.6f},'
        f'{origin_x:.3f},{origin_y:.3f})">'
    )
    for texture_position in (6, 13, depth - 13, depth - 6):
        drawing.draw_line(
            3, texture_position, length - 3, texture_position, "#a7afb3", 0.7, 0.20
        )
    if detailed:
        for index in range(-1, int(length / 9) + 1):
            horizontal_position = index * 9 + state.travel % 9
            if 2 < horizontal_position < length - 2:
                drawing.draw_line(
                    horizontal_position,
                    3,
                    horizontal_position,
                    depth - 3,
                    "#c3cacd",
                    0.6,
                    0.11,
                )
    # 绘制处于皮带上表面范围内的移动印字。
    lettering = "2378244  VEGA X  5EPJ1152"
    for repeat in range(-1, 3):
        start = 30 + state.travel % 390 + repeat * 390
        for index, character in enumerate(lettering):
            horizontal_position = start + index * 9.2
            if 12 <= horizontal_position <= length - 18 and character != " ":
                drawing.draw_text(
                    horizontal_position,
                    depth * 0.64,
                    character,
                    14,
                    "#d9dccf",
                    500,
                    monospaced=True,
                )
    drawing.append_markup("</g>")
    drawing.draw_path(build_belt_outline(geometry), stroke="#131c24", width=7.5)
    drawing.draw_line(*top_left, *top_right, "#606c74", 0.9, 0.6)
    drawing.draw_line(*bottom_left, *bottom_right, "#72808a", 0.85, 0.55)


def draw_roller(
    drawing: SvgDrawing,
    center_x: float,
    center_y: float,
    radius: float,
    travel: float,
    detailed: bool,
    is_drive: bool,
) -> None:
    """绘制金属轮缘、旋转孔位和中心轴。

    Args:
        drawing: 当前 SVG 绘图内容。
        center_x: 中心横坐标。
        center_y: 中心纵坐标。
        radius: 圆角或滚筒半径。
        travel: 皮带累计移动距离。
        detailed: 是否绘制机械细纹。
        is_drive: 是否为驱动滚筒。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """

    # 绘制滚筒轮缘和端面。
    rim_radius = radius - 4
    drawing.draw_ellipse(
        center_x, center_y, rim_radius, rim_radius, "url(#rim)", "#1f2b35", 1
    )
    drawing.draw_ellipse(
        center_x,
        center_y,
        rim_radius - 4.5,
        rim_radius - 4.5,
        "url(#face)",
        "#ebf0f4",
        0.8,
    )
    drawing.draw_ellipse(
        center_x,
        center_y,
        rim_radius * 0.72,
        rim_radius * 0.72,
        "none",
        "#8a99a5",
        1.1,
    )
    if detailed:
        for radius_fraction in (0.79, 0.83, 0.87, 0.91):
            drawing.draw_ellipse(
                center_x,
                center_y,
                rim_radius * radius_fraction,
                rim_radius * radius_fraction,
                "none",
                "#edf2f6",
                0.6,
                0.46,
            )

    # 按累计位移绘制旋转孔位。
    rotation = travel / radius
    count = 6 if is_drive else 4
    for index in range(count):
        angle = rotation + index * math.tau / count
        horizontal_position, vertical_position = (
            center_x + math.cos(angle) * rim_radius * 0.53,
            center_y + math.sin(angle) * rim_radius * 0.53,
        )
        hole = rim_radius * (0.105 if is_drive else 0.10)
        drawing.draw_ellipse(
            horizontal_position,
            vertical_position + 0.6,
            hole + 1.1,
            hole + 1.1,
            "#eef3f6",
        )
        drawing.draw_ellipse(
            horizontal_position,
            vertical_position,
            hole,
            hole,
            "#5d6c79",
            "#8f9fae",
            0.6,
        )
        drawing.draw_ellipse(
            horizontal_position - 0.2,
            vertical_position - 0.8,
            hole * 0.72,
            hole * 0.70,
            "#394c5d",
        )
    if is_drive:
        # 绘制随驱动滚筒旋转的反光标记。
        mark = rotation - 0.6
        mark_start = (
            center_x + math.cos(mark) * (rim_radius - 3),
            center_y + math.sin(mark) * (rim_radius - 3),
        )
        mark_end = (
            center_x + math.cos(mark + 0.15) * (rim_radius - 3),
            center_y + math.sin(mark + 0.15) * (rim_radius - 3),
        )
        drawing.draw_line(*mark_start, *mark_end, "#eed18b", 3.2)

    # 绘制滚筒轴套和中心轴。
    drawing.draw_ellipse(
        center_x + 1.2,
        center_y + 1.8,
        rim_radius * 0.27,
        rim_radius * 0.27,
        "#778794",
    )
    drawing.draw_ellipse(
        center_x,
        center_y,
        rim_radius * 0.255,
        rim_radius * 0.255,
        "url(#hub)",
        "#f0f4f7",
        0.7,
    )
    points = [
        (
            center_x + math.cos(rotation + index * math.tau / 6) * rim_radius * 0.13,
            center_y + math.sin(rotation + index * math.tau / 6) * rim_radius * 0.13,
        )
        for index in range(6)
    ]
    drawing.draw_polygon(points, "#526675", "#f3f6f8", 0.65)
    drawing.draw_ellipse(
        center_x - 0.45,
        center_y - 0.45,
        rim_radius * 0.055,
        rim_radius * 0.055,
        "#273e50",
    )


def draw_sensor(
    drawing: SvgDrawing,
    geometry: BeltGeometry,
    state: BeltVisualState,
) -> None:
    """绘制频率传感器和监听光束。

    Args:
        drawing: 当前 SVG 绘图内容。
        geometry: 皮带几何参数。
        state: 当前帧的纯视觉参数。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """
    horizontal_position, vertical_position = (
        geometry.right + 103,
        geometry.center_y - 41,
    )
    # 绘制传感器支架和光头。
    draw_box(
        drawing,
        horizontal_position + 10,
        vertical_position + 7,
        9,
        111,
        7,
        -5,
        front="url(#post)",
    )
    draw_box(drawing, horizontal_position - 4, 333, 35, 8, 13, -9)
    draw_box(
        drawing,
        horizontal_position - 2,
        vertical_position - 9,
        34,
        20,
        11,
        -8,
        front="url(#dark)",
        side="#233746",
        top="#657888",
    )
    drawing.draw_rectangle(
        horizontal_position - 14, vertical_position - 3, 13, 10, "url(#post)", radius=2
    )
    drawing.draw_ellipse(
        horizontal_position - 14, vertical_position + 2, 3.2, 5, "#2a3946"
    )

    # 按监听相位绘制传感器脉冲。
    active = state.frequency_listening
    phase = state.frequency_phase % math.tau
    pulse = active and min(phase, math.tau - phase) < 0.20
    drawing.draw_ellipse(
        horizontal_position + 21,
        vertical_position - 3,
        2.2,
        2.2,
        "#4adea1" if active else "#7e8c98",
    )
    drawing.draw_ellipse(
        horizontal_position - 15,
        vertical_position + 2,
        1.5,
        2.4,
        "#ff896f" if active else "#4b5d6c",
    )
    if active:
        target_x, target_y = (
            geometry.right + math.cos(-0.6) * 61,
            geometry.center_y + math.sin(-0.6) * 61,
        )
        drawing.draw_line(
            target_x,
            target_y,
            horizontal_position - 16,
            vertical_position + 2,
            "#ee8979",
            0.85,
            0.36,
        )
        drawing.draw_ellipse(
            target_x,
            target_y,
            2.4 if pulse else 1.3,
            2.4 if pulse else 1.3,
            "#f6a88a",
            opacity=0.95 if pulse else 0.36,
        )
    drawing.draw_path(
        f"M{horizontal_position + 34},{vertical_position - 3} "
        f"Q{horizontal_position + 45},{vertical_position + 3} "
        f"{horizontal_position + 35},{vertical_position + 25} "
        f"L{horizontal_position + 35},314 Q{horizontal_position + 36},335 "
        f"{horizontal_position + 49},337",
        stroke="#405665",
        width=2.2,
    )


def draw_camera(
    drawing: SvgDrawing,
    geometry: BeltGeometry,
    state: BeltVisualState,
) -> None:
    """绘制工业相机、镜头、线光源和采集光束。

    Args:
        drawing: 当前 SVG 绘图内容。
        geometry: 皮带几何参数。
        state: 当前帧的纯视觉参数。

    Returns:
        返回示例：
            None  # 图形内容已更新
    """

    # 按采集开关绘制光束和扫描线。
    active = state.capturing
    if active:
        top_left, top_right, _, _ = geometry.get_tangents()
        surface_fraction = (510 - top_left[0]) / (top_right[0] - top_left[0])
        front = (510.0, top_left[1] + surface_fraction * (top_right[1] - top_left[1]))
        back_point = geometry.project_back_point(front)
        drawing.draw_polygon(
            [
                (500, 166),
                (516, 166),
                (back_point[0] + 6, back_point[1]),
                (front[0] + 6, front[1]),
                (front[0] - 6, front[1]),
                (back_point[0] - 6, back_point[1]),
            ],
            "url(#beam)",
        )
        pulse = 0.7 + 0.3 * math.sin(state.scan_phase)
        drawing.draw_line(*front, *back_point, "#7ecaff", 8, 0.13 * pulse)
        drawing.draw_line(*front, *back_point, "#a5e1ff", 2.0, 0.9)
        for shift in (-10, 10):
            drawing.draw_line(
                front[0] + shift,
                front[1] - 1,
                back_point[0] + shift,
                back_point[1] - 1,
                "#75b6e7",
                0.6,
                0.6,
            )

    # 绘制相机安装板和机身。
    draw_box(drawing, 495, 97, 26, 8, 8, -5, front="url(#dark)")
    draw_box(
        drawing,
        486,
        106,
        45,
        39,
        13,
        -9,
        front="url(#frame)",
        side="#667e90",
        top="#dce7ee",
    )
    drawing.draw_rectangle(490, 112, 37, 10, "#2c475d", radius=1)
    drawing.draw_text(
        508.5, 119.3, "VISION", 6.2, "#e5eef5", 600, "middle", monospaced=True
    )
    for rib_position in range(492, 526, 5):
        drawing.draw_line(rib_position, 128, rib_position, 137, "#8e9ba8", 1.0)
    draw_bolt(drawing, 490, 140, 1.5)
    draw_bolt(drawing, 527, 140, 1.5)
    drawing.draw_ellipse(527, 109, 1.8, 1.8, "#4bdfa1" if active else "#8799a8")

    # 绘制相机镜头和镜片。
    drawing.draw_rectangle(498, 145, 22, 20, "url(#dark)", radius=2)
    for groove_position in (149, 153, 157):
        drawing.draw_line(499, groove_position, 519, groove_position, "#8799a6", 0.8)
    drawing.draw_ellipse(509, 164, 11, 4.2, "#172b3e", "#8799a7", 0.8)
    drawing.draw_ellipse(509, 165, 7.5, 2.6, "url(#glass)")
    drawing.draw_ellipse(506.5, 164, 2.4, 0.8, "#bbdef4", opacity=0.7)

    # 绘制独立线光源和支架。
    drawing.draw_path("M540,137 L551,144 L551,179 L535,179", stroke="#8396a4", width=3)
    draw_box(
        drawing,
        478,
        178,
        67,
        8,
        8,
        -6,
        front="url(#dark)",
        side="#334d60",
        top="#a9bac7",
    )
    drawing.draw_line(482, 185, 540, 185, "#d9f2ff" if active else "#a0b5c4", 2.4)


def render_belt_svg(state: BeltVisualState, detailed: bool = True) -> str:
    """按机械层次生成一帧透明背景的皮带场景。

    Args:
        state: 当前帧的纯视觉参数。
        detailed: 是否绘制机械细纹。

    Returns:
        返回示例：
            '<svg xmlns="http://www.w3.org/2000/svg"></svg>'  # SVG 文档格式
    """

    # 根据展开比例计算两端滚筒位置。
    extension = state.extension
    distance = 330.0 + 230.0 * extension
    geometry = BeltGeometry(480 - distance / 2, 480 + distance / 2)

    # 建立透明 SVG 画布和机械材质。
    drawing = SvgDrawing()
    drawing.append_markup(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="480" '
        f'viewBox="0 0 {SCENE_WIDTH} {SCENE_HEIGHT}">'
    )
    drawing.append_markup(SVG_DEFS)

    # 绘制机架和皮带上下表面。
    draw_frame(drawing, geometry)
    draw_belt_shell(drawing, geometry, state, detailed)

    # 绘制大小滚筒及旋转轴心。
    draw_roller(
        drawing,
        geometry.left,
        geometry.center_y,
        geometry.small_radius,
        state.travel,
        detailed,
        False,
    )
    draw_roller(
        drawing,
        geometry.right,
        geometry.center_y,
        geometry.large_radius,
        state.travel,
        detailed,
        True,
    )

    # 绘制传感器和相机子动画。
    draw_sensor(drawing, geometry, state)
    draw_camera(drawing, geometry, state)

    # 输出完整的 SVG 帧。
    drawing.append_markup("</svg>")
    return "".join(drawing.parts)


class _MachineState(Enum):
    """记录动画内部的机器运行阶段。"""

    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()


class BeltAnimationWidget(QWidget):
    """显示皮带机、相机扫描和频率监听动画。"""

    def __init__(self) -> None:
        """初始化动画状态、动画控制器和刷新计时器。

        Args:
            无。

        Returns:
            返回示例：
                None  # 创建动画组件
        """
        super().__init__()
        self._machine_state = _MachineState.STOPPED
        self.capturing = False
        self.frequency_listening = False
        self.extension = 0.0
        self.belt_offset = 0.0
        self.belt_travel = 0.0
        self.scan_progress = 0.0
        self.frequency_phase = 0.0
        self.setObjectName("beltAnimation")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(125)
        self.setMaximumHeight(175)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        # 保存当前机械场景的渲染结果。
        self.scene_renderer = QSvgRenderer(self)
        self.rendered_visual_state: BeltVisualState | None = None

        # 创建皮带展开和收缩动画。
        self.extension_animation = QVariantAnimation(self)
        self.extension_animation.setDuration(650)
        self.extension_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.extension_animation.valueChanged.connect(self._on_extension_changed)
        self.extension_animation.finished.connect(self._on_extension_finished)

        # 定时推进皮带纹理、相机扫描和频率脉冲。
        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(30)
        self.animation_timer.timeout.connect(self._advance_animation)
        self.animation_timer.start()

    def start_machine(self) -> None:
        """启动皮带展开和滚筒运行动画。

        Args:
            无。

        Returns:
            返回示例：
                None  # 皮带开始展开
        """
        if self._machine_state in (_MachineState.STARTING, _MachineState.RUNNING):
            return

        self._machine_state = _MachineState.STARTING
        self.extension_animation.stop()
        self.extension_animation.setStartValue(self.extension)
        self.extension_animation.setEndValue(1.0)
        self.extension_animation.start()
        self.update()

    def stop_machine(self) -> None:
        """停止皮带并结束采集和频率监听动画。

        Args:
            无。

        Returns:
            返回示例：
                None  # 皮带开始收缩，采集和监听动画停止
        """
        # 停止采集和频率监听动画。
        self.capturing = False
        self.frequency_listening = False
        if self._machine_state in (_MachineState.STOPPED, _MachineState.STOPPING):
            self.update()
            return

        self._machine_state = _MachineState.STOPPING
        self.extension_animation.stop()
        self.extension_animation.setStartValue(self.extension)
        self.extension_animation.setEndValue(0.0)
        self.extension_animation.start()
        self.update()

    def start_capture(self) -> None:
        """启动相机光束和扫描线动画。

        Args:
            无。

        Returns:
            返回示例：
                None  # 相机采集动画开始
        """
        self.capturing = True
        self.scan_progress = 0.0
        self.update()

    def stop_capture(self) -> None:
        """停止相机光束和扫描线动画。

        Args:
            无。

        Returns:
            返回示例：
                None  # 相机采集动画停止
        """
        self.capturing = False
        self.update()

    def set_frequency_listening(self, listening: bool) -> None:
        """设置频率传感器监听动画的开关。

        Args:
            listening: 是否显示频率监听效果。

        Returns:
            返回示例：
                None  # 频率监听效果按开关状态显示或隐藏
        """
        self.frequency_listening = listening
        self.update()

    def _on_extension_changed(self, value: object) -> None:
        """更新皮带展开比例。

        Args:
            value: 展开动画当前值。

        Returns:
            返回示例：
                None  # 保存当前展开比例并请求重绘
        """
        self.extension = float(value)
        self.update()

    def _on_extension_finished(self) -> None:
        """完成皮带启动或停止状态切换。

        Args:
            无。

        Returns:
            返回示例：
                None  # 动画内部状态更新为运行或停止
        """
        if self._machine_state == _MachineState.STARTING:
            self._machine_state = _MachineState.RUNNING
        elif self._machine_state == _MachineState.STOPPING:
            self._machine_state = _MachineState.STOPPED
        self.update()

    def _advance_animation(self) -> None:
        """推进循环动画，并在动画启用时请求重绘。

        Args:
            无。

        Returns:
            返回示例：
                None  # 动画相位已推进，空闲时不请求重绘
        """
        animation_active = (
            self._machine_state == _MachineState.RUNNING
            or self.capturing
            or self.frequency_listening
        )
        if not animation_active:
            return

        if self._machine_state == _MachineState.RUNNING:
            self.belt_offset = (self.belt_offset + 2.8) % 80
            self.belt_travel += 2.8

        if self.capturing:
            self.scan_progress = (self.scan_progress + 0.025) % 1.0

        if self.frequency_listening:
            self.frequency_phase += 0.15

        self.update()

    def paintEvent(self, event) -> None:
        """将当前动画状态映射到机械场景并等比例居中绘制。

        Args:
            event: Qt 绘制事件。

        Returns:
            返回示例：
                None  # 机械场景已绘制到组件的透明背景上
        """
        # 读取本帧的几何比例和子动画相位。
        visual_state = BeltVisualState(
            extension=self.extension,
            capturing=self.capturing,
            frequency_listening=self.frequency_listening,
            travel=self.belt_travel,
            scan_phase=self.scan_progress * math.tau,
            frequency_phase=self.frequency_phase,
        )

        # 视觉参数变化时更新 SVG 场景。
        if visual_state != self.rendered_visual_state:
            source = render_belt_svg(visual_state).encode("utf-8")
            self.scene_renderer.load(QByteArray(source))
            self.rendered_visual_state = visual_state

        # 按可用宽高计算居中的等比例绘制区域。
        scale = min(self.width() / SCENE_WIDTH, self.height() / SCENE_HEIGHT)
        scene_width = SCENE_WIDTH * scale
        scene_height = SCENE_HEIGHT * scale
        target = QRectF(
            (self.width() - scene_width) / 2,
            (self.height() - scene_height) / 2,
            scene_width,
            scene_height,
        )

        # 绘制机械图形并释放本次画笔。
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            self.scene_renderer.render(painter, target)
        finally:
            painter.end()
