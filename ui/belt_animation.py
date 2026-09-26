"""绘制皮带机示意动画。"""

from __future__ import annotations

import math
from enum import Enum, auto

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


class _MachineState(Enum):
    """记录动画内部的机器运行阶段。"""

    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()


class BeltAnimationWidget(QWidget):
    """显示皮带机、相机扫描和频率监听动画。"""

    LOGICAL_WIDTH = 620
    LOGICAL_HEIGHT = 300
    LOGICAL_TOP = 60

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
        self.scan_progress = 0.0
        self.frequency_phase = 0.0
        self.setObjectName("beltAnimation")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(140)
        self.setMaximumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        # 创建皮带展开和收缩动画。
        self.extension_animation = QVariantAnimation(self)
        self.extension_animation.setDuration(650)
        self.extension_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.extension_animation.valueChanged.connect(self._on_extension_changed)
        self.extension_animation.finished.connect(self._on_extension_finished)

        # 定时推进皮带箭头、相机扫描和频率波形。
        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(30)
        self.animation_timer.timeout.connect(self._advance_animation)
        self.animation_timer.start()

    def start_machine(self) -> None:
        """启动皮带展开和运行箭头动画。

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
        """设置频率监听波形动画的开关。

        Args:
            listening: 是否显示频率监听波形。

        Returns:
            返回示例：
                None  # 频率波形按开关状态显示或隐藏
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
        """推进所有已启用的循环动画。

        Args:
            无。

        Returns:
            返回示例：
                None  # 动画相位前进并请求重绘
        """
        if self._machine_state == _MachineState.RUNNING:
            self.belt_offset = (self.belt_offset + 2.8) % 80

        if self.capturing:
            self.scan_progress = (self.scan_progress + 0.025) % 1.0

        if self.frequency_listening:
            self.frequency_phase += 0.15

        self.update()

    def paintEvent(self, event) -> None:
        """按当前组件尺寸居中绘制等比例动画。

        Args:
            event: Qt 绘制事件。

        Returns:
            返回示例：
                None  # 绘制皮带机动画区域
        """
        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = min(
            self.width() / self.LOGICAL_WIDTH,
            self.height() / self.LOGICAL_HEIGHT,
        )
        offset_x = (self.width() - self.LOGICAL_WIDTH * scale) / 2
        offset_y = (self.height() - self.LOGICAL_HEIGHT * scale) / 2
        painter.translate(offset_x, offset_y)
        painter.scale(scale, scale)
        painter.translate(0, -self.LOGICAL_TOP)

        # 按相机、皮带、滚轮和动画覆盖层的顺序绘制。
        self._draw_camera(painter)
        belt_path = self._draw_belt(painter)
        self._draw_rollers(painter)
        self._draw_running_arrows(painter)
        self._draw_capture_animation(painter, belt_path)
        self._draw_frequency_animation(painter)

    def _draw_camera(self, painter: QPainter) -> None:
        """绘制相机机身、镜头和名称。

        Args:
            painter: 当前动画绘制器。

        Returns:
            返回示例：
                None  # 相机图形绘制完成
        """
        camera_x = self.LOGICAL_WIDTH / 2
        camera_y = 112
        body_rect = QRectF(camera_x - 18, camera_y, 36, 34)
        gradient = QLinearGradient(body_rect.topLeft(), body_rect.bottomRight())
        gradient.setColorAt(0, QColor("#8793A1"))
        gradient.setColorAt(1, QColor("#3C4754"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor("#1F2937"), 2))

        # 绘制相机机身。
        painter.drawRoundedRect(body_rect, 3, 3)

        # 绘制镜头和相机名称。
        painter.setBrush(QColor("#536170"))
        painter.drawRect(QRectF(camera_x - 9, camera_y + 34, 18, 20))
        font = QFont()
        font.setPixelSize(17)
        painter.setFont(font)
        painter.setPen(QColor("#64748B"))
        painter.drawText(
            QRectF(camera_x + 28, camera_y + 20, 100, 30),
            Qt.AlignmentFlag.AlignVCenter,
            "相机",
        )

    def _get_belt_geometry(self) -> tuple[float, float, float, float, float]:
        """根据展开比例计算两侧滚轮的位置和半径。

        Args:
            无。

        Returns:
            返回示例：
                (
                    95.0,  # 左侧滚轮横坐标
                    525.0,  # 右侧滚轮横坐标
                    285.0,  # 滚轮中心纵坐标
                    22.0,  # 左侧滚轮半径
                    53.0,  # 右侧滚轮半径
                )  # 皮带几何参数
        """
        center_x = self.LOGICAL_WIDTH / 2
        center_y = 285
        distance = 220 + (430 - 220) * self.extension
        left_x = center_x - distance / 2
        right_x = center_x + distance / 2
        left_radius = 22
        right_radius = 53
        return left_x, right_x, center_y, left_radius, right_radius

    def _create_belt_path(self) -> QPainterPath:
        """根据滚轮几何参数创建闭合皮带路径。

        Args:
            无。

        Returns:
            返回示例：
                QPainterPath()  # 两侧滚轮之间的闭合皮带路径
        """
        left_x, right_x, center_y, left_radius, right_radius = (
            self._get_belt_geometry()
        )
        distance = right_x - left_x
        ratio = (right_radius - left_radius) / distance
        ratio = max(-0.99, min(0.99, ratio))
        height_ratio = math.sqrt(1 - ratio * ratio)

        # 计算连接两侧滚轮的皮带切点。
        top_left = QPointF(
            left_x - left_radius * ratio,
            center_y - left_radius * height_ratio,
        )
        top_right = QPointF(
            right_x - right_radius * ratio,
            center_y - right_radius * height_ratio,
        )
        bottom_left = QPointF(
            left_x - left_radius * ratio,
            center_y + left_radius * height_ratio,
        )
        path = QPainterPath()
        path.moveTo(top_left)
        path.lineTo(top_right)

        # 使用左右滚轮弧线闭合皮带路径。
        right_rect = QRectF(
            right_x - right_radius,
            center_y - right_radius,
            right_radius * 2,
            right_radius * 2,
        )
        angle = math.degrees(math.acos(-ratio))
        path.arcTo(right_rect, angle, -2 * angle)
        path.lineTo(bottom_left)
        left_rect = QRectF(
            left_x - left_radius,
            center_y - left_radius,
            left_radius * 2,
            left_radius * 2,
        )
        path.arcTo(left_rect, -angle, 2 * angle - 360)
        path.closeSubpath()
        return path

    def _draw_belt(self, painter: QPainter) -> QPainterPath:
        """绘制深色皮带和外缘，并返回皮带路径。

        Args:
            painter: 当前动画绘制器。

        Returns:
            返回示例：
                QPainterPath()  # 后续动画使用的皮带路径
        """
        belt_path = self._create_belt_path()

        # 设置皮带渐变和外缘颜色。
        gradient = QLinearGradient(0, 220, 0, 340)
        gradient.setColorAt(0, QColor("#3D4854"))
        gradient.setColorAt(0.5, QColor("#293540"))
        gradient.setColorAt(1, QColor("#202A34"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor("#70879C"), 6))
        painter.drawPath(belt_path)
        return belt_path

    def _draw_rollers(self, painter: QPainter) -> None:
        """绘制大小不同的左右滚轮。

        Args:
            painter: 当前动画绘制器。

        Returns:
            返回示例：
                None  # 两个滚轮绘制完成
        """
        left_x, right_x, center_y, left_radius, right_radius = (
            self._get_belt_geometry()
        )
        self._draw_single_roller(painter, QPointF(left_x, center_y), left_radius)
        self._draw_single_roller(painter, QPointF(right_x, center_y), right_radius)

    def _draw_single_roller(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
    ) -> None:
        """绘制单个滚轮及中心轴。

        Args:
            painter: 当前动画绘制器。
            center: 滚轮中心坐标。
            radius: 滚轮半径。

        Returns:
            返回示例：
                None  # 单个滚轮绘制完成
        """
        highlight = QPointF(center.x() - radius * 0.3, center.y() - radius * 0.3)
        gradient = QRadialGradient(center, radius, highlight)
        gradient.setColorAt(0, QColor("#D8DCE1"))
        gradient.setColorAt(0.55, QColor("#9EA6AF"))
        gradient.setColorAt(1, QColor("#606A75"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor("#18212B"), 2.5))

        # 绘制滚轮外圈和中心轴。
        painter.drawEllipse(center, radius, radius)
        painter.setBrush(QColor("#E6E9ED"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, radius * 0.18, radius * 0.18)

    def _draw_running_arrows(self, painter: QPainter) -> None:
        """在运行中的皮带上绘制移动箭头。

        Args:
            painter: 当前动画绘制器。

        Returns:
            返回示例：
                None  # 运行箭头绘制完成或当前未运行
        """
        if self._machine_state != _MachineState.RUNNING:
            return

        left_x, right_x, center_y, left_radius, right_radius = (
            self._get_belt_geometry()
        )
        start_x = left_x + left_radius + 25
        end_x = right_x - right_radius - 20
        arrow_width = end_x - start_x
        if arrow_width <= 0:
            return

        # 设置箭头样式并按动画偏移依次绘制。
        painter.setPen(
            QPen(
                QColor(93, 140, 180, 130),
                6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        spacing = 65
        arrow_count = int(arrow_width / spacing) + 2
        offset = self.belt_offset % spacing
        for index in range(arrow_count):
            arrow_x = start_x + index * spacing + offset
            if arrow_x <= end_x:
                self._draw_chevron(painter, arrow_x, center_y)

    def _draw_chevron(
        self,
        painter: QPainter,
        horizontal_position: float,
        vertical_position: float,
    ) -> None:
        """绘制一个向右的皮带运行箭头。

        Args:
            painter: 当前动画绘制器。
            horizontal_position: 箭头尖端横坐标。
            vertical_position: 箭头中心纵坐标。

        Returns:
            返回示例：
                None  # 绘制一个向右箭头
        """
        painter.drawLine(
            QPointF(horizontal_position - 10, vertical_position - 10),
            QPointF(horizontal_position, vertical_position),
        )
        painter.drawLine(
            QPointF(horizontal_position, vertical_position),
            QPointF(horizontal_position - 10, vertical_position + 10),
        )

    def _draw_capture_animation(
        self,
        painter: QPainter,
        belt_path: QPainterPath,
    ) -> None:
        """绘制相机光束和沿皮带移动的扫描线。

        Args:
            painter: 当前动画绘制器。
            belt_path: 扫描线需要裁剪到的皮带路径。

        Returns:
            返回示例：
                None  # 采集动画绘制完成或采集未启动
        """
        if not self.capturing:
            return

        center_x = self.LOGICAL_WIDTH / 2
        camera_bottom = 166
        belt_top = 235
        beam = QPainterPath()
        beam.moveTo(center_x - 8, camera_bottom)
        beam.lineTo(center_x + 8, camera_bottom)
        beam.lineTo(center_x + 55, belt_top + 65)
        beam.lineTo(center_x - 55, belt_top + 65)
        beam.closeSubpath()
        beam_gradient = QLinearGradient(0, camera_bottom, 0, belt_top + 70)
        beam_gradient.setColorAt(0, QColor(45, 148, 255, 90))
        beam_gradient.setColorAt(1, QColor(45, 148, 255, 5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(beam_gradient)

        # 绘制相机照向皮带的光束。
        painter.drawPath(beam)

        # 将扫描光限制在皮带形状内。
        scan_x = center_x - 45 + self.scan_progress * 90
        painter.save()
        painter.setClipPath(belt_path)
        painter.setPen(QPen(QColor(70, 160, 255, 80), 13))
        painter.drawLine(QPointF(scan_x, 225), QPointF(scan_x, 345))
        painter.setPen(QPen(QColor("#2F98FF"), 4))
        painter.drawLine(QPointF(scan_x, 225), QPointF(scan_x, 345))
        painter.restore()

    def _draw_frequency_animation(self, painter: QPainter) -> None:
        """绘制频率监听波形动画。

        Args:
            painter: 当前动画绘制器。

        Returns:
            返回示例：
                None  # 监听波形绘制完成或监听未开启
        """
        if not self.frequency_listening:
            return

        center_x = self.LOGICAL_WIDTH / 2 + 145
        center_y = 135
        painter.setPen(
            QPen(
                QColor("#22C987"),
                5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        bar_count = 7

        # 根据监听相位计算每根波形线的高度。
        for index in range(bar_count):
            distance = abs(index - (bar_count - 1) / 2)
            base_height = 52 - distance * 10
            wave = (math.sin(self.frequency_phase + index * 0.8) + 1) * 5
            bar_height = base_height + wave
            bar_x = center_x + (index - 3) * 11
            painter.drawLine(
                QPointF(bar_x, center_y - bar_height / 2),
                QPointF(bar_x, center_y + bar_height / 2),
            )
