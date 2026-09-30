# /// script
# requires-python = ">=3.10"
# dependencies = ["PySide6>=6.8,<7"]
# ///
"""皮带检测装置：可独立运行的 PySide6 动画样机。

运行：
    uv run belt_animation_demo.py
    # 已安装 PySide6 的环境也可以直接运行：
    python belt_animation_demo.py

只依赖 PySide6 和 Python 标准库，不导入 belt_ocr 项目中的任何模块。
设备图形由本文件生成，没有外部图片、字体、模型或 SVG 文件。

BeltAnimationWidget 是可嵌入的动画组件；DemoWindow 只负责演示按钮和模拟时序。
组件只展示状态，不连接硬件，不创建工作线程，不生成业务事件。
所有控制方法都应在 Qt 主线程调用；接入项目时用 Qt 信号连接这些槽函数。

这是半写实的 2.5D 动画，不是按现场设备尺寸制作的三维模型。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from html import escape
from typing import Optional

try:
    from PySide6.QtCore import (
        QByteArray, QElapsedTimer, QRectF, QSize, Qt, QTimer, Signal, Slot,
    )
    from PySide6.QtGui import QFont, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
        QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("PySide6"):
        raise SystemExit(
            "缺少 PySide6。请运行：\n"
            "  uv run belt_animation_demo.py\n"
            "或先安装：python -m pip install \"PySide6>=6.8,<7\""
        ) from exc
    raise


# ---------------------------------------------------------------------------
# 1. 场景与几何：纯绘图数据，不包含窗口、计时器或业务逻辑。
# ---------------------------------------------------------------------------

SCENE_WIDTH = 1000.0
SCENE_HEIGHT = 480.0
Point = tuple[float, float]
TEXT_FONT_FAMILY = ("Microsoft YaHei" if sys.platform == "win32" else
                    "PingFang SC" if sys.platform == "darwin" else "Noto Sans CJK SC")
MONO_FONT_FAMILY = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


@dataclass
class AnimationState:
    """保存当前画面状态；频率值和动画倍率互不影响。"""

    extension: float = 0.0
    running: bool = False
    capturing: bool = False
    frequency_listening: bool = False
    fault: bool = False
    travel: float = 0.0
    time: float = 0.0
    frequency_hz: Optional[float] = None


@dataclass(frozen=True)
class BeltGeometry:
    left: float
    right: float
    cy: float = 263.0
    small_radius: float = 34.0
    large_radius: float = 66.0
    depth_x: float = 46.0
    depth_y: float = -33.0

    @property
    def tangents(self) -> tuple[Point, Point, Point, Point]:
        """返回上左、上右、下右、下左四个外公切点。"""
        k = (self.large_radius - self.small_radius) / (self.right - self.left)
        h = math.sqrt(1.0 - k * k)
        r, R = self.small_radius, self.large_radius
        return (
            (self.left - r * k, self.cy - r * h),
            (self.right - R * k, self.cy - R * h),
            (self.right - R * k, self.cy + R * h),
            (self.left - r * k, self.cy + r * h),
        )

    def back(self, point: Point) -> Point:
        return point[0] + self.depth_x, point[1] + self.depth_y


class SvgDrawing:
    """组装 Qt SVG 支持的基本图形，不使用滤镜、脚本或外部资源。"""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def path(self, d: str, fill: str = "none", stroke: str = "none",
             width: float = 1.0, opacity: float = 1.0) -> None:
        self.add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="{width}" stroke-linejoin="round" '
                 f'stroke-linecap="round" opacity="{opacity:.3f}"/>')

    def polygon(self, points: list[Point], fill: str,
                stroke: str = "none", width: float = 1.0,
                opacity: float = 1.0) -> None:
        coords = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
        self.add(f'<polygon points="{coords}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="{width}" stroke-linejoin="round" '
                 f'opacity="{opacity:.3f}"/>')

    def rect(self, x: float, y: float, w: float, h: float,
             fill: str, radius: float = 0, stroke: str = "none",
             width: float = 1.0) -> None:
        self.add(f'<rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" '
                 f'height="{h:.3f}" rx="{radius}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{width}"/>')

    def line(self, x1: float, y1: float, x2: float, y2: float,
             stroke: str, width: float = 1.0, opacity: float = 1.0) -> None:
        self.path(f"M{x1:.3f},{y1:.3f} L{x2:.3f},{y2:.3f}",
                  stroke=stroke, width=width, opacity=opacity)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float,
                fill: str, stroke: str = "none", width: float = 1.0,
                opacity: float = 1.0) -> None:
        self.add(f'<ellipse cx="{cx:.3f}" cy="{cy:.3f}" rx="{rx:.3f}" '
                 f'ry="{ry:.3f}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="{width}" opacity="{opacity:.3f}"/>')

    def text(self, x: float, y: float, value: str, size: float = 12,
             fill: str = "#667085", weight: int = 400,
             anchor: str = "start", mono: bool = False) -> None:
        family = MONO_FONT_FAMILY if mono else TEXT_FONT_FAMILY
        self.add(f'<text x="{x:.3f}" y="{y:.3f}" font-family="{family}" '
                 f'font-size="{size}" fill="{fill}" font-weight="{weight}" '
                 f'text-anchor="{anchor}">{escape(value)}</text>')


SVG_DEFS = '''
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
'''


def draw_box(s: SvgDrawing, x: float, y: float, w: float, h: float,
             dx: float = 13, dy: float = -9,
             front: str = "url(#frame)", side: str = "#8795a1",
             top: str = "#e3e9ee") -> None:
    """用三个面画出具有厚度的金属件。"""
    s.polygon([(x, y), (x + dx, y + dy), (x + w + dx, y + dy), (x + w, y)],
              top, "#a9b5bf", 0.7)
    s.polygon([(x + w, y), (x + w + dx, y + dy),
               (x + w + dx, y + h + dy), (x + w, y + h)], side)
    s.rect(x, y, w, h, front, stroke="#91a0ad", width=0.7)
    s.line(x + 1, y + 1, x + w - 1, y + 1, "#ffffff", 1.1, 0.75)


def draw_bolt(s: SvgDrawing, x: float, y: float, r: float = 3.2) -> None:
    s.ellipse(x, y, r, r, "#778591", "#e5ecf1", 0.7)
    s.line(x - r * 0.4, y - 0.25, x + r * 0.4, y + 0.25, "#263744", 0.9)


def draw_frame(s: SvgDrawing, g: BeltGeometry) -> None:
    """画底座、可调脚、滑轨、张紧滑台和后置支架。"""
    s.ellipse(512, 409, 419, 35, "url(#shadow)")
    # 前后四只脚都落在同一个底座上；柔和阴影代替大块背景面板。
    for x, y in [(210, 362), (825, 362), (164, 395), (779, 395)]:
        s.ellipse(x + 3, y + 10, 25, 8, "#33414c", opacity=0.1)
        s.ellipse(x, y + 5, 17, 6, "#26313c")
        s.rect(x - 11, y - 17, 22, 21, "url(#post)", radius=2)
        s.ellipse(x, y + 1, 13, 4, "#94a2ae")
        for yy in (y - 12, y - 7):
            s.line(x - 10, yy, x + 10, yy, "#7c8b98", 1)
    draw_box(s, 128, 350, 721, 29, 46, -33)
    s.rect(140, 360, 695, 5, "#637581", radius=2)
    s.line(140, 366, 834, 366, "#eaf0f4", 1.2)
    s.rect(141, 373, 694, 2, "#92a0ac")
    s.text(467, 372, "BELT  /  VISION", 8.8, "#4b5c6c", 500, mono=True)
    for x in (153, 306, 688, 822):
        draw_bolt(s, x, 355, 2.4)

    # 两条导轨穿过左侧滑台；轮距变化时滑台沿导轨移动。
    draw_box(s, 161, 338, 646, 8, 27, -19, front="url(#post)")
    s.line(164, 340, 806, 340, "#f7f9fa", 1.4)
    s.line(165, 345, 805, 345, "#627585", 1)
    s.line(169, 325, 496, 325, "#71818e", 5)
    s.line(169, 324, 496, 324, "#dce3e8", 1.3)
    for x in range(178, 494, 7):
        s.line(x, 322, x - 2, 328, "#526474", 0.7)
    draw_box(s, g.left - 43, 327, 86, 13, 26, -19, front="url(#dark)")
    draw_box(s, g.left - 21, 265, 42, 63, 22, -16, front="url(#post)")
    s.rect(g.left - 12, 278, 24, 29, "#8898a5", radius=3)
    draw_bolt(s, g.left - 29, 332, 2.5)
    draw_bolt(s, g.left + 29, 332, 2.5)
    s.ellipse(159, 325, 8, 8, "url(#hub)", "#758593", 1)
    s.ellipse(159, 325, 3.2, 3.2, "#485b6b")

    # 右轴座和后置电机，不遮住前面的滚筒端面。
    draw_box(s, g.right - 28, 300, 61, 40, 30, -21, front="url(#post)")
    draw_box(s, g.right + 18, 278, 81, 43, 25, -18,
             front="url(#dark)", side="#283d4d", top="#617483")
    for x in range(7):
        s.rect(g.right + 30 + x * 8, 287, 3, 26, "#203440", radius=1)
        s.line(g.right + 31 + x * 8, 287, g.right + 31 + x * 8, 311,
               "#6c8190", 0.7)
    s.rect(g.right + 54, 280, 28, 7, "#cad6df", radius=1)

    # 相机立柱固定在底座上，横臂和线缆都保留。
    draw_box(s, 571, 320, 57, 14, 23, -16, front="url(#dark)")
    draw_box(s, 584, 69, 23, 252, 13, -9, front="url(#post)")
    s.rect(593, 75, 5, 235, "#8494a1", radius=2)
    s.line(598, 76, 598, 307, "#eff3f6", 1)
    draw_box(s, 479, 81, 130, 18, 13, -9)
    s.rect(486, 88, 116, 4, "#8e9eab", radius=1)
    for x, y in ((590, 91), (599, 91), (580, 325), (617, 325)):
        draw_bolt(s, x, y, 2.7)
    s.path("M519,91 C518,50 559,48 597,55 C625,59 628,75 627,103 "
           "L627,292 Q627,315 639,321", stroke="#334858", width=3.2)
    s.path("M519,91 C518,50 559,48 597,55", stroke="#8c9ca8", width=0.8)


def belt_outline(g: BeltGeometry) -> str:
    tl, tr, br, bl = g.tangents
    r, R = g.small_radius, g.large_radius
    return (f"M{tl[0]:.3f},{tl[1]:.3f} L{tr[0]:.3f},{tr[1]:.3f} "
            f"A{R},{R} 0 1 1 {br[0]:.3f},{br[1]:.3f} "
            f"L{bl[0]:.3f},{bl[1]:.3f} "
            f"A{r},{r} 0 0 1 {tl[0]:.3f},{tl[1]:.3f} Z")


def draw_belt_shell(s: SvgDrawing, g: BeltGeometry, state: AnimationState,
                    detailed: bool) -> None:
    """画带宽、回程面和绕轮面；中间不填实，保留真实的空心结构。"""
    tl, tr, br, bl = g.tangents
    s.add(f'<g transform="translate({g.depth_x},{g.depth_y})">')
    s.path(belt_outline(g), stroke="#343e47", width=7)
    s.add("</g>")
    s.polygon([bl, br, g.back(br), g.back(bl)], "url(#returnBelt)")

    # 皮带绕过两端的曲面，让宽度延续到滚筒背面。
    k = (g.large_radius - g.small_radius) / (g.right - g.left)
    top_angle = math.atan2(-math.sqrt(1 - k * k), -k)
    bottom_angle = -top_angle
    for cx, radius, start, end in (
        (g.right, g.large_radius, top_angle, bottom_angle),
        (g.left, g.small_radius, bottom_angle, top_angle + 2 * math.pi),
    ):
        points = [(cx + radius * math.cos(start + (end - start) * i / 28),
                   g.cy + radius * math.sin(start + (end - start) * i / 28))
                  for i in range(29)]
        s.polygon(points + [g.back(p) for p in reversed(points)], "url(#rubber)")
        if detailed:
            phase = state.travel / radius
            # 同一线速度下，小滚筒转得更快；纹理与轮面使用相同相位。
            count = 34 if radius > 40 else 19
            for i in range(count):
                a = (i * math.tau / count + phase - start) % math.tau + start
                if a <= end:
                    p = (cx + radius * math.cos(a), g.cy + radius * math.sin(a))
                    q = g.back(p)
                    s.line(p[0], p[1], q[0], q[1], "#a4adb3", 0.65, 0.10)

    s.polygon([tl, tr, g.back(tr), g.back(tl)], "url(#beltTop)", "#1f2931", 0.6)
    s.line(*g.back(tl), *g.back(tr), "#7d878e", 1, 0.7)

    # 把文字坐标贴在皮带上表面，而不是悬浮在皮带前面。
    length = math.hypot(tr[0] - tl[0], tr[1] - tl[1])
    ux, uy = (tr[0] - tl[0]) / length, (tr[1] - tl[1]) / length
    depth = math.hypot(g.depth_x, g.depth_y)
    vx, vy = -g.depth_x / depth, -g.depth_y / depth
    ox, oy = g.back(tl)
    s.add(f'<g transform="matrix({ux:.6f},{uy:.6f},{vx:.6f},{vy:.6f},{ox:.3f},{oy:.3f})">')
    for v in (6, 13, depth - 13, depth - 6):
        s.line(3, v, length - 3, v, "#a7afb3", 0.7, 0.20)
    if detailed:
        for i in range(-1, int(length / 9) + 1):
            x = i * 9 + state.travel % 9
            if 2 < x < length - 2:
                s.line(x, 3, x, depth - 3, "#c3cacd", 0.6, 0.11)
    # 逐字限制绘制范围，避免依赖不同 Qt SVG 版本的裁剪扩展。
    lettering = "2378244  VEGA X  5EPJ1152"
    for repeat in range(-1, 3):
        start = 30 + state.travel % 390 + repeat * 390
        for index, char in enumerate(lettering):
            x = start + index * 9.2
            if 12 <= x <= length - 18 and char != " ":
                s.text(x, depth * 0.64, char, 14, "#d9dccf", 500, mono=True)
    s.add("</g>")
    s.path(belt_outline(g), stroke="#131c24", width=7.5)
    s.line(*tl, *tr, "#606c74", 0.9, 0.6)
    s.line(*bl, *br, "#72808a", 0.85, 0.55)


def draw_roller(s: SvgDrawing, cx: float, cy: float, radius: float,
                travel: float, detailed: bool, is_drive: bool) -> None:
    """画金属轮缘、车削纹、旋转孔位、轴套和轴心。"""
    r = radius - 4
    s.ellipse(cx, cy, r, r, "url(#rim)", "#1f2b35", 1)
    s.ellipse(cx, cy, r - 4.5, r - 4.5, "url(#face)", "#ebf0f4", 0.8)
    s.ellipse(cx, cy, r * 0.72, r * 0.72, "none", "#8a99a5", 1.1)
    if detailed:
        for frac in (0.79, 0.83, 0.87, 0.91):
            s.ellipse(cx, cy, r * frac, r * frac, "none", "#edf2f6", 0.6, 0.46)
    rotation = travel / radius
    count = 6 if is_drive else 4
    for i in range(count):
        a = rotation + i * math.tau / count
        x, y = cx + math.cos(a) * r * 0.53, cy + math.sin(a) * r * 0.53
        hole = r * (0.105 if is_drive else 0.10)
        s.ellipse(x, y + 0.6, hole + 1.1, hole + 1.1, "#eef3f6")
        s.ellipse(x, y, hole, hole, "#5d6c79", "#8f9fae", 0.6)
        s.ellipse(x - 0.2, y - 0.8, hole * 0.72, hole * 0.70, "#394c5d")
    if is_drive:
        # 一枚反光标记经过传感器的位置，避免用夸张箭头代替机械运动。
        mark = rotation - 0.6
        p1 = (cx + math.cos(mark) * (r - 3), cy + math.sin(mark) * (r - 3))
        p2 = (cx + math.cos(mark + 0.15) * (r - 3), cy + math.sin(mark + 0.15) * (r - 3))
        s.line(*p1, *p2, "#eed18b", 3.2)
    s.ellipse(cx + 1.2, cy + 1.8, r * 0.27, r * 0.27, "#778794")
    s.ellipse(cx, cy, r * 0.255, r * 0.255, "url(#hub)", "#f0f4f7", 0.7)
    points = [(cx + math.cos(rotation + i * math.tau / 6) * r * 0.13,
               cy + math.sin(rotation + i * math.tau / 6) * r * 0.13) for i in range(6)]
    s.polygon(points, "#526675", "#f3f6f8", 0.65)
    s.ellipse(cx - 0.45, cy - 0.45, r * 0.055, r * 0.055, "#273e50")


def draw_sensor(s: SvgDrawing, g: BeltGeometry, state: AnimationState,
                detailed: bool) -> None:
    x, y = g.right + 103, g.cy - 41
    # 支架随驱动轴位置移动；光头向左对准轮缘。
    draw_box(s, x + 10, y + 7, 9, 111, 7, -5, front="url(#post)")
    draw_box(s, x - 4, 333, 35, 8, 13, -9)
    draw_box(s, x - 2, y - 9, 34, 20, 11, -8,
             front="url(#dark)", side="#233746", top="#657888")
    s.rect(x - 14, y - 3, 13, 10, "url(#post)", radius=2)
    s.ellipse(x - 14, y + 2, 3.2, 5, "#2a3946")
    active = state.frequency_listening and state.running and not state.fault
    # 闪灯与驱动轮反光标记经过光头的相位对应，而不是随机闪烁。
    phase = (state.travel / g.large_radius) % math.tau
    pulse = active and min(phase, math.tau - phase) < 0.20
    s.ellipse(x + 21, y - 3, 2.2, 2.2, "#4adea1" if active else "#7e8c98")
    s.ellipse(x - 15, y + 2, 1.5, 2.4, "#ff896f" if active else "#4b5d6c")
    if active:
        tx, ty = g.right + math.cos(-0.6) * 61, g.cy + math.sin(-0.6) * 61
        s.line(tx, ty, x - 16, y + 2, "#ee8979", 0.85, 0.36)
        s.ellipse(tx, ty, 2.4 if pulse else 1.3, 2.4 if pulse else 1.3,
                  "#f6a88a", opacity=0.95 if pulse else 0.36)
    s.path(f"M{x + 34},{y - 3} Q{x + 45},{y + 3} {x + 35},{y + 25} "
           f"L{x + 35},314 Q{x + 36},335 {x + 49},337",
           stroke="#405665", width=2.2)
    if detailed:
        s.text(805, 101, "频率传感器", 14, "#465a6d", 500)
        value = (f"{state.frequency_hz:.1f} Hz" if state.frequency_hz is not None
                 else "监听中") if active else "等待监听"
        s.text(805, 121, value, 11.5, "#438575" if active else "#8b99a6")
        s.path(f"M843,133 L{x + 7:.2f},165 L{x + 7:.2f},{y - 17:.2f}",
               stroke="#cbd5dd", width=0.9)
        s.ellipse(x + 7, y - 17, 2, 2, "#a6b4bf")


def draw_camera(s: SvgDrawing, g: BeltGeometry, state: AnimationState,
                detailed: bool) -> None:
    active = state.capturing and state.running and not state.fault
    if active:
        tl, tr, _, _ = g.tangents
        u = (510 - tl[0]) / (tr[0] - tl[0])
        front = (510.0, tl[1] + u * (tr[1] - tl[1]))
        back = g.back(front)
        s.polygon([(500, 166), (516, 166),
                   (back[0] + 6, back[1]), (front[0] + 6, front[1]),
                   (front[0] - 6, front[1]), (back[0] - 6, back[1])],
                  "url(#beam)")
        pulse = 0.7 + 0.3 * math.sin(state.time * 6)
        s.line(*front, *back, "#7ecaff", 8, 0.13 * pulse)
        s.line(*front, *back, "#a5e1ff", 2.0, 0.9)
        for shift in (-10, 10):
            s.line(front[0] + shift, front[1] - 1, back[0] + shift, back[1] - 1,
                   "#75b6e7", 0.6, 0.6)

    # 相机不是悬空图标：机身固定在横臂下面，有安装板和独立线光源。
    draw_box(s, 495, 97, 26, 8, 8, -5, front="url(#dark)")
    draw_box(s, 486, 106, 45, 39, 13, -9,
             front="url(#frame)", side="#667e90", top="#dce7ee")
    s.rect(490, 112, 37, 10, "#2c475d", radius=1)
    s.text(508.5, 119.3, "VISION", 6.2, "#e5eef5", 600, "middle", mono=True)
    for xx in range(492, 526, 5):
        s.line(xx, 128, xx, 137, "#8e9ba8", 1.0)
    draw_bolt(s, 490, 140, 1.5)
    draw_bolt(s, 527, 140, 1.5)
    s.ellipse(527, 109, 1.8, 1.8, "#4bdfa1" if active else "#8799a8")
    s.rect(498, 145, 22, 20, "url(#dark)", radius=2)
    for yy in (149, 153, 157):
        s.line(499, yy, 519, yy, "#8799a6", 0.8)
    s.ellipse(509, 164, 11, 4.2, "#172b3e", "#8799a7", 0.8)
    s.ellipse(509, 165, 7.5, 2.6, "url(#glass)")
    s.ellipse(506.5, 164, 2.4, 0.8, "#bbdef4", opacity=0.7)
    s.path("M540,137 L551,144 L551,179 L535,179", stroke="#8396a4", width=3)
    draw_box(s, 478, 178, 67, 8, 8, -6,
             front="url(#dark)", side="#334d60", top="#a9bac7")
    s.line(482, 185, 540, 185, "#d9f2ff" if active else "#a0b5c4", 2.4)
    if detailed:
        s.text(287, 74, "工业相机", 14, "#465a6d", 500)
        s.text(287, 94, "固定支架 · 线光源", 11.5, "#8b99a6")
        s.path("M377,80 L438,80 L478,117", stroke="#cbd5dd", width=0.9)
        s.ellipse(478, 117, 2, 2, "#a6b4bf")


def render_belt_svg(state: AnimationState, detailed: bool = True) -> str:
    """生成一帧自包含的 SVG；运行界面和静态预览使用同一份几何。"""
    extension = min(1.0, max(0.0, state.extension))
    distance = 330.0 + 230.0 * extension
    geometry = BeltGeometry(480 - distance / 2, 480 + distance / 2)
    s = SvgDrawing()
    s.add(f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="480" '
          f'viewBox="0 0 {SCENE_WIDTH} {SCENE_HEIGHT}">')
    s.add(SVG_DEFS)
    draw_frame(s, geometry)
    draw_belt_shell(s, geometry, state, detailed)
    draw_roller(s, geometry.left, geometry.cy, geometry.small_radius,
                state.travel, detailed, False)
    draw_roller(s, geometry.right, geometry.cy, geometry.large_radius,
                state.travel, detailed, True)
    draw_sensor(s, geometry, state, detailed)
    draw_camera(s, geometry, state, detailed)
    if detailed:
        s.text(162, 442, "01", 10.5, "#a4b0bc", 500, mono=True)
        s.text(187, 442, "张紧滑台", 11.5, "#7b8b99")
        s.text(650, 442, "02", 10.5, "#a4b0bc", 500, mono=True)
        s.text(675, 442, "驱动滚筒 / 后置电机", 11.5, "#7b8b99")
    if state.fault:
        s.ellipse(142, 52, 4, 4, "#dd625c")
        s.text(155, 56, "设备故障 · 等待关闭", 12, "#c26560", 500)
    s.add("</svg>")
    return "".join(s.parts)


# ---------------------------------------------------------------------------
# 2. 可嵌入组件：仅一只刷新计时器，隐藏和完全静止时停止刷新。
# ---------------------------------------------------------------------------

class BeltAnimationWidget(QWidget):
    """半写实皮带检测动画，可直接放入现有机器卡片的布局。"""

    state_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state = AnimationState()
        self._animation_speed = 1.0
        self._annotations = True
        self._target_extension = 0.0
        self._transition_from = 0.0
        self._transition_elapsed = 0.7
        self._transition_duration = 0.7
        self._renderer = QSvgRenderer(self)
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance_animation)
        self._dirty = True
        self._last_detail = None
        self.setMinimumSize(260, 145)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

    def sizeHint(self) -> QSize:
        return QSize(900, 432)

    @property
    def running(self) -> bool:
        return self._state.running

    @property
    def capturing(self) -> bool:
        return self._state.capturing

    @property
    def frequency_listening(self) -> bool:
        return self._state.frequency_listening

    @property
    def fault(self) -> bool:
        return self._state.fault

    @Slot()
    def start_machine(self) -> None:
        """START：展开并转动。重复调用不会重新开始展开动画。"""
        if self._state.fault or self._state.running:
            return
        self._state.running = True
        self._set_extension_target(1.0)
        self._changed()

    @Slot()
    def stop_machine(self) -> None:
        """CLOSE：停止子动画并收缩，和业务失败动作分开。"""
        self._state.running = False
        self._state.capturing = False
        self._state.frequency_listening = False
        self._state.frequency_hz = None
        self._set_extension_target(0.0)
        self._changed()

    @Slot()
    def start_capture(self) -> None:
        if self._state.running and not self._state.fault:
            self._state.capturing = True
            self._changed()

    @Slot()
    def stop_capture(self) -> None:
        """只停止相机光线，不停止皮带、不触发收缩。"""
        self._state.capturing = False
        self._changed()

    @Slot(bool)
    def set_frequency_listening(self, listening: bool) -> None:
        self._state.frequency_listening = bool(
            listening and self._state.running and not self._state.fault
        )
        if not self._state.frequency_listening:
            self._state.frequency_hz = None
        self._changed()

    def set_frequency_hz(self, value: Optional[float]) -> None:
        """只更新显示值；None 表示没有实际读数。"""
        if value is not None:
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError("频率必须为非负有限数值，或 None。")
        self._state.frequency_hz = value
        self._changed()

    @Slot(bool)
    def set_fault(self, fault: bool = True) -> None:
        """设备故障冻结画面，不提前收缩；清除故障也不会自动启动。"""
        self._state.fault = bool(fault)
        if fault:
            self._state.running = False
            self._state.capturing = False
            self._state.frequency_listening = False
            self._state.frequency_hz = None
            self._target_extension = self._state.extension
            self._transition_elapsed = self._transition_duration
        self._changed()

    @Slot(float)
    def set_animation_speed(self, speed: float) -> None:
        """动画倍率，不代表设备实际转速或实测频率。"""
        speed = float(speed)
        if not math.isfinite(speed) or not 0.25 <= speed <= 2.0:
            raise ValueError("动画倍率必须位于 0.25 到 2.0 之间。")
        self._animation_speed = speed

    @Slot(bool)
    def set_annotations_visible(self, visible: bool) -> None:
        self._annotations = bool(visible)
        self._dirty = True
        self.update()

    @Slot()
    def reset(self) -> None:
        """只清空演示状态，不应当用于处理运行时迟到事件。"""
        self._state = AnimationState()
        self._target_extension = 0.0
        self._transition_from = 0.0
        self._transition_elapsed = self._transition_duration
        self._changed()

    def _set_extension_target(self, target: float) -> None:
        if target == self._target_extension:
            return
        self._target_extension = target
        self._transition_from = self._state.extension
        self._transition_elapsed = 0.0

    def _is_animating(self) -> bool:
        return (self._transition_elapsed < self._transition_duration
                or self._state.running)

    def _sync_timer(self) -> None:
        if self.isVisible() and self._is_animating():
            if not self._timer.isActive():
                self._elapsed.start()
                self._timer.start()
        else:
            self._timer.stop()

    def _changed(self) -> None:
        self._dirty = True
        self._sync_timer()
        self.update()
        self.state_changed.emit()

    def _advance_animation(self) -> None:
        # 用实际间隔推进相位，窗口拖动或重绘变慢时不会简单累积帧数误差。
        dt = min(self._elapsed.restart() / 1000.0, 0.08)
        if self._transition_elapsed < self._transition_duration:
            self._transition_elapsed = min(
                self._transition_duration, self._transition_elapsed + dt
            )
            t = self._transition_elapsed / self._transition_duration
            eased = t * t * (3.0 - 2.0 * t)
            self._state.extension = (
                self._transition_from
                + (self._target_extension - self._transition_from) * eased
            )
        if self._state.running:
            # 先展开、再完整运转；收缩时不继续播放采集或监听动画。
            movement = 0.35 + 0.65 * self._state.extension
            self._state.travel += dt * 63.0 * self._animation_speed * movement
            self._state.time += dt
        self._dirty = True
        self.update()
        self._sync_timer()

    def paintEvent(self, event) -> None:
        # 小卡片隐藏标注与细纹，保留机器本体，避免缩小后文字拥挤。
        detailed = self._annotations and self.width() >= 620 and self.height() >= 285
        if self._dirty or detailed != self._last_detail:
            source = render_belt_svg(self._state, detailed).encode("utf-8")
            if not self._renderer.load(QByteArray(source)):
                raise RuntimeError("内部 SVG 场景生成失败。")
            self._dirty = False
            self._last_detail = detailed
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            scale = min(self.width() / SCENE_WIDTH, self.height() / SCENE_HEIGHT)
            w, h = SCENE_WIDTH * scale, SCENE_HEIGHT * scale
            target = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
            self._renderer.render(painter, target)
        finally:
            painter.end()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)


# ---------------------------------------------------------------------------
# 3. 独立演示窗口：模拟数据与自动步骤只存在于这一层。
# ---------------------------------------------------------------------------

DEMO_STYLE = """
QMainWindow, QWidget#demoRoot { background: #F4F4F6; }
QWidget { color: #243343; font-size: 13px; }
QFrame#sceneCard, QFrame#controlsCard {
    background: #FFFFFF; border: 1px solid #E5E8EC; border-radius: 18px;
}
QLabel { background: transparent; border: none; }
QLabel#eyebrow { color: #8A96A3; font-size: 11px; }
QLabel#title { color: #233141; font-size: 25px; font-weight: 600; }
QLabel#subtitle { color: #7D8996; font-size: 12px; }
QLabel#hint { color: #718090; font-size: 12px; }
QLabel#badge { color: #477965; background: #EDF5F0; border-radius: 11px;
               padding: 5px 13px; font-size: 12px; }
QLabel#status { color: #526B83; font-size: 12px; }
QPushButton { background: #F7F8FA; border: 1px solid #E3E7EC;
              border-radius: 9px; padding: 9px 15px; color: #415369; }
QPushButton:hover { background: #EEF2F6; border-color: #CFD8E1; }
QPushButton:pressed { background: #E4EBF2; }
QPushButton:checked { background: #EDF4FF; border-color: #BDD3F5; color: #356BB1; }
QPushButton#primary { background: #3269B6; border-color: #3269B6; color: white; }
QPushButton#primary:hover { background: #2A5FA8; }
QPushButton#faultButton { color: #B66759; background: #FFF9F6; border-color: #F0DFD8; }
QSlider::groove:horizontal { height: 4px; background: #E4EAF1; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #668BB7; border-radius: 2px; }
QSlider::handle:horizontal { width: 13px; margin: -5px 0;
                            border-radius: 6px; background: #456F9C; }
QCheckBox { color: #758496; spacing: 6px; background: transparent; }
"""


class DemoWindow(QMainWindow):
    """模拟一轮 START、采集、监听完成和 CLOSE，可随时切到手动。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("belt_ocr · 皮带检测装置动画样机")
        self.resize(1120, 800)
        self.setMinimumSize(830, 645)
        self.setStyleSheet(DEMO_STYLE)
        root = QWidget()
        root.setObjectName("demoRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 25, 30, 23)
        layout.setSpacing(17)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(5)
        for text, name in (
            ("BELT OCR  /  ANIMATION STUDY", "eyebrow"),
            ("皮带检测单元", "title"),
            ("半写实机械场景 · 独立演示，不连接相机、IO 或 OCR", "subtitle"),
        ):
            label = QLabel(text)
            label.setObjectName(name)
            titles.addWidget(label)
        heading.addLayout(titles)
        heading.addStretch()
        self.badge = QLabel("自动演示")
        self.badge.setObjectName("badge")
        heading.addWidget(self.badge, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        card = QFrame()
        card.setObjectName("sceneCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(23, 17, 23, 15)
        card_layout.setSpacing(0)
        card_head = QHBoxLayout()
        device_title = QLabel("机器 01")
        device_title.setStyleSheet("font-weight: 600; color: #46566A;")
        card_head.addWidget(device_title)
        card_head.addStretch()
        self.status = QLabel("待机")
        self.status.setObjectName("status")
        card_head.addWidget(self.status)
        card_layout.addLayout(card_head)
        self.belt = BeltAnimationWidget()
        card_layout.addWidget(self.belt, 1)
        card_foot = QHBoxLayout()
        self.stage_label = QLabel()
        self.stage_label.setObjectName("hint")
        card_foot.addWidget(self.stage_label, 1)
        annotations = QCheckBox("设备标注")
        annotations.setChecked(True)
        annotations.toggled.connect(self.belt.set_annotations_visible)
        card_foot.addWidget(annotations)
        card_layout.addLayout(card_foot)
        layout.addWidget(card, 1)

        controls = QFrame()
        controls.setObjectName("controlsCard")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(20, 17, 20, 16)
        controls_layout.setSpacing(14)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.auto_button = QPushButton("停止自动演示")
        self.auto_button.setObjectName("primary")
        self.auto_button.clicked.connect(self._toggle_demo)
        actions.addWidget(self.auto_button)
        start_button = QPushButton("START · 展开")
        start_button.clicked.connect(self._manual_start)
        actions.addWidget(start_button)
        stop_button = QPushButton("CLOSE · 收缩")
        stop_button.clicked.connect(self._manual_stop)
        actions.addWidget(stop_button)
        fault_button = QPushButton("模拟设备故障")
        fault_button.setObjectName("faultButton")
        fault_button.clicked.connect(self._manual_fault)
        actions.addWidget(fault_button)
        actions.addStretch()
        controls_layout.addLayout(actions)

        second = QHBoxLayout()
        second.setSpacing(10)
        self.capture_button = QPushButton("相机采集")
        self.capture_button.setCheckable(True)
        self.capture_button.clicked.connect(self._manual_capture)
        second.addWidget(self.capture_button)
        self.frequency_button = QPushButton("频率监听")
        self.frequency_button.setCheckable(True)
        self.frequency_button.clicked.connect(self._manual_frequency)
        second.addWidget(self.frequency_button)
        second.addStretch()
        second.addWidget(QLabel("动画倍率"))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(25, 200)
        slider.setValue(100)
        slider.setFixedWidth(135)
        slider.valueChanged.connect(self._change_speed)
        second.addWidget(slider)
        self.speed_label = QLabel("1.00×")
        self.speed_label.setFixedWidth(44)
        second.addWidget(self.speed_label)
        controls_layout.addLayout(second)
        layout.addWidget(controls)
        footnote = QLabel("画面中的印字与 48.6 Hz 均为演示数据；动画倍率不代表实际转速。")
        footnote.setObjectName("hint")
        layout.addWidget(footnote)

        # 不使用一串 singleShot；停止自动演示后不会遗留回调覆盖手动状态。
        self._demo_timer = QTimer(self)
        self._demo_timer.setInterval(80)
        self._demo_timer.timeout.connect(self._advance_demo)
        self._demo_clock = QElapsedTimer()
        self._demo_stage = -1
        self.belt.state_changed.connect(self._refresh_status)
        self._start_demo()

    def _refresh_status(self) -> None:
        b = self.belt
        mode = "设备故障" if b.fault else ("运行中" if b.running else "待机 / 已关闭")
        camera = "采集中" if b.capturing else "相机待机"
        frequency = "监听中" if b.frequency_listening else "监听停止"
        self.status.setText(f"{mode}    ·    {camera}    ·    {frequency}")
        self.capture_button.setChecked(b.capturing)
        self.frequency_button.setChecked(b.frequency_listening)

    def _start_demo(self) -> None:
        self._demo_timer.stop()
        self.belt.reset()
        self._demo_stage = -1
        self._demo_clock.start()
        self._demo_timer.start()
        self.auto_button.setText("停止自动演示")
        self.badge.setText("自动演示")
        self._advance_demo()

    def _stop_demo(self) -> None:
        self._demo_timer.stop()
        self.auto_button.setText("开始自动演示")
        self.badge.setText("手动控制")
        self.stage_label.setText("手动模式：使用 START、采集、监听与 CLOSE 测试各个动作。")

    def _toggle_demo(self) -> None:
        if self._demo_timer.isActive():
            self._stop_demo()
        else:
            self._start_demo()

    def _advance_demo(self) -> None:
        elapsed = self._demo_clock.elapsed() / 1000.0
        if elapsed >= 14.0:
            self._start_demo()
            return
        stage = (0 if elapsed < 0.9 else 1 if elapsed < 2.0 else
                 2 if elapsed < 7.0 else 3 if elapsed < 9.5 else
                 4 if elapsed < 11.0 else 5)
        if stage == self._demo_stage:
            return
        self._demo_stage = stage
        if stage == 0:
            self.stage_label.setText("01 / 待机：皮带保持收缩，所有采集动画停止。")
        elif stage == 1:
            self.belt.start_machine()
            self.stage_label.setText("02 / START：两端滑台展开，皮带开始转动。")
        elif stage == 2:
            self.belt.start_capture()
            self.belt.set_frequency_listening(True)
            self.belt.set_frequency_hz(48.6)
            self.stage_label.setText("03 / 并行采集：相机投光、皮带印字移动、频率传感器监听。")
        elif stage == 3:
            self.belt.stop_capture()
            self.stage_label.setText("04 / 图像采集完成：相机熄灯，皮带和频率监听继续运行。")
        elif stage == 4:
            self.belt.set_frequency_listening(False)
            self.stage_label.setText("05 / 监听完成：皮带继续转动，等待 CLOSE。")
        elif stage == 5:
            self.belt.stop_machine()
            self.stage_label.setText("06 / CLOSE：停止所有子动画，皮带平滑收缩。")

    def _manual_start(self) -> None:
        self._stop_demo()
        self.belt.set_fault(False)
        self.belt.start_machine()

    def _manual_stop(self) -> None:
        self._stop_demo()
        self.belt.stop_machine()

    def _manual_fault(self) -> None:
        self._stop_demo()
        self.belt.set_fault(True)
        self.stage_label.setText("设备故障：冻结转动和采集，但不自动收缩；CLOSE 才收缩。")

    def _manual_capture(self, checked: bool) -> None:
        self._stop_demo()
        if checked:
            self.belt.start_capture()
        else:
            self.belt.stop_capture()
        self._refresh_status()

    def _manual_frequency(self, checked: bool) -> None:
        self._stop_demo()
        self.belt.set_frequency_listening(checked)
        if self.belt.frequency_listening:
            self.belt.set_frequency_hz(48.6)
        self._refresh_status()

    def _change_speed(self, value: int) -> None:
        self.belt.set_animation_speed(value / 100.0)
        self.speed_label.setText(f"{value / 100.0:.2f}×")

    def closeEvent(self, event) -> None:
        self._demo_timer.stop()
        self.belt.reset()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI",
                      "Noto Sans CJK SC", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)
    window = DemoWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
