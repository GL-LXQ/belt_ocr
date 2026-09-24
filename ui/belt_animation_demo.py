from __future__ import annotations

import math
import sys
from enum import Enum, auto

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class MachineState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()


class BeltMachineCard(QWidget):
    """实时监测页面中的单台机器动画卡片。"""

    def __init__(self, machine_name: str = "1# 皮带机") -> None:
        super().__init__()

        self.machine_name = machine_name
        self.machine_state = MachineState.STOPPED

        self.capturing = False
        self.frequency_listening = False
        self.frequency = 0.0

        # 0 = 收缩状态，1 = 完全展开。
        self.extension = 0.0

        # 皮带内部箭头运动偏移。
        self.belt_offset = 0.0

        # 扫描线位置。
        self.scan_progress = 0.0

        # 频率动画相位。
        self.frequency_phase = 0.0

        self.setMinimumSize(620, 520)

        # 启动 / 停止展开动画。
        self.extension_animation = QVariantAnimation(self)

        self.extension_animation.setDuration(650)

        self.extension_animation.setEasingCurve(
            QEasingCurve.Type.InOutCubic
        )

        self.extension_animation.valueChanged.connect(
            self._on_extension_changed
        )

        self.extension_animation.finished.connect(
            self._on_extension_finished
        )

        # 持续动画刷新。
        self.animation_timer = QTimer(self)

        self.animation_timer.setInterval(30)

        self.animation_timer.timeout.connect(
            self._advance_animation
        )

        self.animation_timer.start()

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------

    def start_machine(self) -> None:
        """收到机器启动信号。"""

        if self.machine_state in (
            MachineState.STARTING,
            MachineState.RUNNING,
        ):
            return

        self.machine_state = MachineState.STARTING

        self.extension_animation.stop()

        self.extension_animation.setStartValue(
            self.extension
        )

        self.extension_animation.setEndValue(
            1.0
        )

        self.extension_animation.start()

        self.update()

    def stop_machine(self) -> None:
        """收到机器停止信号。"""

        if self.machine_state in (
            MachineState.STOPPED,
            MachineState.STOPPING,
        ):
            return

        self.machine_state = MachineState.STOPPING

        self.capturing = False

        self.frequency_listening = False

        self.frequency = 0.0

        self.extension_animation.stop()

        self.extension_animation.setStartValue(
            self.extension
        )

        self.extension_animation.setEndValue(
            0.0
        )

        self.extension_animation.start()

        self.update()

    def start_capture(self) -> None:
        """开始相机采集动画。"""

        self.capturing = True

        self.scan_progress = 0.0

        self.update()

    def stop_capture(self) -> None:
        """停止相机采集动画。"""

        self.capturing = False

        self.update()

    def set_frequency_listening(
        self,
        listening: bool,
    ) -> None:
        """设置是否正在监听频率。"""

        self.frequency_listening = listening

        self.update()

    def set_frequency(
        self,
        frequency: float,
    ) -> None:
        """设置真实频率显示值。"""

        self.frequency = frequency

        self.update()

    # ------------------------------------------------------------------
    # 动画
    # ------------------------------------------------------------------

    def _on_extension_changed(
        self,
        value,
    ) -> None:
        self.extension = float(value)

        self.update()

    def _on_extension_finished(self) -> None:
        if self.machine_state == MachineState.STARTING:
            self.machine_state = MachineState.RUNNING

        elif self.machine_state == MachineState.STOPPING:
            self.machine_state = MachineState.STOPPED

        self.update()

    def _advance_animation(self) -> None:
        if self.machine_state == MachineState.RUNNING:
            self.belt_offset += 2.8

            if self.belt_offset >= 80:
                self.belt_offset = 0

        if self.capturing:
            self.scan_progress += 0.025

            if self.scan_progress > 1:
                self.scan_progress = 0

        if self.frequency_listening:
            self.frequency_phase += 0.15

        self.update()

    # ------------------------------------------------------------------
    # 绘图
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)

        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing
        )

        painter.fillRect(
            self.rect(),
            QColor("#EEF4FB"),
        )

        self._draw_card(painter)

        self._draw_header(painter)

        self._draw_camera(painter)

        belt_path = self._draw_belt(painter)

        self._draw_rollers(painter)

        self._draw_running_arrows(painter)

        self._draw_capture_animation(
            painter,
            belt_path,
        )

        self._draw_frequency_animation(painter)

        self._draw_info_boxes(painter)

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------

    def _draw_card(
        self,
        painter: QPainter,
    ) -> None:
        rect = QRectF(
            12,
            12,
            self.width() - 24,
            self.height() - 24,
        )

        gradient = QLinearGradient(
            rect.topLeft(),
            rect.bottomLeft(),
        )

        gradient.setColorAt(
            0,
            QColor("#F9FBFE"),
        )

        gradient.setColorAt(
            1,
            QColor("#F3F7FC"),
        )

        painter.setBrush(
            gradient
        )

        painter.setPen(
            QPen(
                QColor("#D5E1EF"),
                1.5,
            )
        )

        painter.drawRoundedRect(
            rect,
            18,
            18,
        )

    # ------------------------------------------------------------------
    # 标题和状态
    # ------------------------------------------------------------------

    def _draw_header(
        self,
        painter: QPainter,
    ) -> None:
        font = QFont()

        font.setPixelSize(25)

        font.setBold(True)

        painter.setFont(
            font
        )

        painter.setPen(
            QColor("#0F172A")
        )

        painter.drawText(
            QRectF(
                50,
                42,
                260,
                40,
            ),
            Qt.AlignmentFlag.AlignVCenter,
            self.machine_name,
        )

        status_text, status_color = (
            self._get_status()
        )

        circle_x = self.width() - 160

        circle_y = 62

        painter.setPen(
            Qt.PenStyle.NoPen
        )

        painter.setBrush(
            status_color
        )

        painter.drawEllipse(
            QPointF(
                circle_x,
                circle_y,
            ),
            10,
            10,
        )

        status_font = QFont()

        status_font.setPixelSize(20)

        status_font.setBold(True)

        painter.setFont(
            status_font
        )

        painter.setPen(
            status_color
        )

        painter.drawText(
            QRectF(
                circle_x + 20,
                43,
                110,
                38,
            ),
            Qt.AlignmentFlag.AlignVCenter,
            status_text,
        )

    def _get_status(self):
        if self.machine_state == MachineState.STARTING:
            return (
                "启动中",
                QColor("#2387F7"),
            )

        if self.machine_state == MachineState.RUNNING:
            return (
                "运行中",
                QColor("#16B364"),
            )

        if self.machine_state == MachineState.STOPPING:
            return (
                "停止中",
                QColor("#F79009"),
            )

        return (
            "停止中",
            QColor("#98A2B3"),
        )

    # ------------------------------------------------------------------
    # 相机
    # ------------------------------------------------------------------

    def _draw_camera(
        self,
        painter: QPainter,
    ) -> None:
        camera_x = self.width() / 2

        camera_y = 112

        # 相机机身。
        body_rect = QRectF(
            camera_x - 18,
            camera_y,
            36,
            34,
        )

        gradient = QLinearGradient(
            body_rect.topLeft(),
            body_rect.bottomRight(),
        )

        gradient.setColorAt(
            0,
            QColor("#8793A1"),
        )

        gradient.setColorAt(
            1,
            QColor("#3C4754"),
        )

        painter.setBrush(
            gradient
        )

        painter.setPen(
            QPen(
                QColor("#1F2937"),
                2,
            )
        )

        painter.drawRoundedRect(
            body_rect,
            3,
            3,
        )

        # 镜头。
        painter.setBrush(
            QColor("#536170")
        )

        painter.drawRect(
            QRectF(
                camera_x - 9,
                camera_y + 34,
                18,
                20,
            )
        )

        # 相机文字。
        font = QFont()

        font.setPixelSize(17)

        painter.setFont(
            font
        )

        painter.setPen(
            QColor("#64748B")
        )

        painter.drawText(
            QRectF(
                camera_x + 28,
                camera_y + 20,
                100,
                30,
            ),
            Qt.AlignmentFlag.AlignVCenter,
            "相机",
        )

    # ------------------------------------------------------------------
    # 皮带
    # ------------------------------------------------------------------

    def _belt_geometry(self):
        center_x = self.width() / 2

        center_y = 285

        # 停止状态距离比较短。
        min_distance = 220

        # 运行状态距离较长。
        max_distance = 430

        distance = (
            min_distance
            + (max_distance - min_distance)
            * self.extension
        )

        left_x = (
            center_x
            - distance / 2
        )

        right_x = (
            center_x
            + distance / 2
        )

        # 左边滚轴始终较小。
        left_radius = 22

        # 右边滚轴始终较大。
        right_radius = 53

        return (
            left_x,
            right_x,
            center_y,
            left_radius,
            right_radius,
        )

    def _create_belt_path(self):
        (
            left_x,
            right_x,
            center_y,
            left_radius,
            right_radius,
        ) = self._belt_geometry()

        distance = (
            right_x - left_x
        )

        delta_radius = (
            right_radius
            - left_radius
        )

        ratio = (
            delta_radius
            / distance
        )

        ratio = max(
            -0.99,
            min(
                0.99,
                ratio,
            ),
        )

        height_ratio = math.sqrt(
            1 - ratio * ratio
        )

        top_left = QPointF(
            left_x - left_radius * ratio,
            center_y
            - left_radius * height_ratio,
        )

        top_right = QPointF(
            right_x - right_radius * ratio,
            center_y
            - right_radius * height_ratio,
        )

        bottom_left = QPointF(
            left_x - left_radius * ratio,
            center_y
            + left_radius * height_ratio,
        )

        path = QPainterPath()

        path.moveTo(
            top_left
        )

        path.lineTo(
            top_right
        )

        right_rect = QRectF(
            right_x - right_radius,
            center_y - right_radius,
            right_radius * 2,
            right_radius * 2,
        )

        angle = math.degrees(
            math.acos(
                -ratio
            )
        )

        path.arcTo(
            right_rect,
            angle,
            -2 * angle,
        )

        path.lineTo(
            bottom_left
        )

        left_rect = QRectF(
            left_x - left_radius,
            center_y - left_radius,
            left_radius * 2,
            left_radius * 2,
        )

        path.arcTo(
            left_rect,
            -angle,
            2 * angle - 360,
        )

        path.closeSubpath()

        return path

    def _draw_belt(
        self,
        painter: QPainter,
    ):
        path = self._create_belt_path()

        # 皮带内部保持深色橡胶质感。
        gradient = QLinearGradient(
            0,
            220,
            0,
            340,
        )

        gradient.setColorAt(
            0,
            QColor("#3D4854"),
        )

        gradient.setColorAt(
            0.5,
            QColor("#293540"),
        )

        gradient.setColorAt(
            1,
            QColor("#202A34"),
        )

        painter.setBrush(
            gradient
        )

        # 皮带外缘使用更亮的蓝灰色。
        # 与内部深色区域形成明显区别。
        painter.setPen(
            QPen(
                QColor("#70879C"),
                6,
            )
        )

        painter.drawPath(
            path
        )

        return path

    # ------------------------------------------------------------------
    # 滚轴
    # ------------------------------------------------------------------

    def _draw_rollers(
        self,
        painter: QPainter,
    ) -> None:
        (
            left_x,
            right_x,
            center_y,
            left_radius,
            right_radius,
        ) = self._belt_geometry()

        self._draw_single_roller(
            painter,
            QPointF(
                left_x,
                center_y,
            ),
            left_radius,
        )

        self._draw_single_roller(
            painter,
            QPointF(
                right_x,
                center_y,
            ),
            right_radius,
        )

    def _draw_single_roller(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
    ) -> None:
        gradient = QRadialGradient(
            center,
            radius,
            QPointF(
                center.x()
                - radius * 0.3,
                center.y()
                - radius * 0.3,
            ),
        )

        gradient.setColorAt(
            0,
            QColor("#D8DCE1"),
        )

        gradient.setColorAt(
            0.55,
            QColor("#9EA6AF"),
        )

        gradient.setColorAt(
            1,
            QColor("#606A75"),
        )

        painter.setBrush(
            gradient
        )

        painter.setPen(
            QPen(
                QColor("#18212B"),
                2.5,
            )
        )

        painter.drawEllipse(
            center,
            radius,
            radius,
        )

        painter.setBrush(
            QColor("#E6E9ED")
        )

        painter.setPen(
            Qt.PenStyle.NoPen
        )

        painter.drawEllipse(
            center,
            radius * 0.18,
            radius * 0.18,
        )

    # ------------------------------------------------------------------
    # 皮带运行箭头
    # ------------------------------------------------------------------

    def _draw_running_arrows(
        self,
        painter: QPainter,
    ) -> None:
        if self.machine_state != MachineState.RUNNING:
            return

        (
            left_x,
            right_x,
            center_y,
            left_radius,
            right_radius,
        ) = self._belt_geometry()

        start_x = (
            left_x
            + left_radius
            + 25
        )

        end_x = (
            right_x
            - right_radius
            - 20
        )

        width = (
            end_x - start_x
        )

        if width <= 0:
            return

        painter.setPen(
            QPen(
                QColor(
                    93,
                    140,
                    180,
                    130,
                ),
                6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )

        spacing = 65

        count = int(
            width / spacing
        ) + 2

        offset = (
            self.belt_offset
            % spacing
        )

        for index in range(count):
            x = (
                start_x
                + index * spacing
                + offset
            )

            if x > end_x:
                continue

            self._draw_chevron(
                painter,
                x,
                center_y,
            )

    def _draw_chevron(
        self,
        painter: QPainter,
        x: float,
        y: float,
    ) -> None:
        painter.drawLine(
            QPointF(
                x - 10,
                y - 10,
            ),
            QPointF(
                x,
                y,
            ),
        )

        painter.drawLine(
            QPointF(
                x,
                y,
            ),
            QPointF(
                x - 10,
                y + 10,
            ),
        )

    # ------------------------------------------------------------------
    # 相机采集
    # ------------------------------------------------------------------

    def _draw_capture_animation(
        self,
        painter: QPainter,
        belt_path: QPainterPath,
    ) -> None:
        if not self.capturing:
            return

        center_x = (
            self.width() / 2
        )

        camera_bottom = 166

        belt_top = 235

        # 相机蓝色光束。
        beam = QPainterPath()

        beam.moveTo(
            center_x - 8,
            camera_bottom,
        )

        beam.lineTo(
            center_x + 8,
            camera_bottom,
        )

        beam.lineTo(
            center_x + 55,
            belt_top + 65,
        )

        beam.lineTo(
            center_x - 55,
            belt_top + 65,
        )

        beam.closeSubpath()

        beam_gradient = QLinearGradient(
            0,
            camera_bottom,
            0,
            belt_top + 70,
        )

        beam_gradient.setColorAt(
            0,
            QColor(
                45,
                148,
                255,
                90,
            ),
        )

        beam_gradient.setColorAt(
            1,
            QColor(
                45,
                148,
                255,
                5,
            ),
        )

        painter.setPen(
            Qt.PenStyle.NoPen
        )

        painter.setBrush(
            beam_gradient
        )

        painter.drawPath(
            beam
        )

        # 扫描线在小范围内左右移动。
        scan_x = (
            center_x
            - 45
            + self.scan_progress * 90
        )

        painter.save()

        painter.setClipPath(
            belt_path
        )

        glow_pen = QPen(
            QColor(
                70,
                160,
                255,
                80,
            ),
            13,
        )

        painter.setPen(
            glow_pen
        )

        painter.drawLine(
            QPointF(
                scan_x,
                225,
            ),
            QPointF(
                scan_x,
                345,
            ),
        )

        painter.setPen(
            QPen(
                QColor("#2F98FF"),
                4,
            )
        )

        painter.drawLine(
            QPointF(
                scan_x,
                225,
            ),
            QPointF(
                scan_x,
                345,
            ),
        )

        painter.restore()

    # ------------------------------------------------------------------
    # 频率监听
    # ------------------------------------------------------------------

    def _draw_frequency_animation(
        self,
        painter: QPainter,
    ) -> None:
        if not self.frequency_listening:
            return

        center_x = (
            self.width() / 2 + 145
        )

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

        for index in range(bar_count):
            distance = abs(
                index
                - (bar_count - 1) / 2
            )

            base_height = (
                52
                - distance * 10
            )

            wave = (
                math.sin(
                    self.frequency_phase
                    + index * 0.8
                )
                + 1
            ) * 5

            height = (
                base_height
                + wave
            )

            x = (
                center_x
                + (index - 3) * 11
            )

            painter.drawLine(
                QPointF(
                    x,
                    center_y - height / 2,
                ),
                QPointF(
                    x,
                    center_y + height / 2,
                ),
            )

    # ------------------------------------------------------------------
    # 底部状态框
    # ------------------------------------------------------------------

    def _draw_info_boxes(
        self,
        painter: QPainter,
    ) -> None:
        margin = 38

        gap = 18

        box_width = (
            self.width()
            - margin * 2
            - gap
        ) / 2

        y = (
            self.height()
            - 135
        )

        box_height = 92

        left_rect = QRectF(
            margin,
            y,
            box_width,
            box_height,
        )

        right_rect = QRectF(
            margin
            + box_width
            + gap,
            y,
            box_width,
            box_height,
        )

        painter.setPen(
            QPen(
                QColor("#EDF1F6"),
                1,
            )
        )

        painter.setBrush(
            QColor(
                255,
                255,
                255,
                210,
            )
        )

        painter.drawRoundedRect(
            left_rect,
            14,
            14,
        )

        painter.drawRoundedRect(
            right_rect,
            14,
            14,
        )

        title_font = QFont()

        title_font.setPixelSize(17)

        title_font.setBold(True)

        painter.setFont(
            title_font
        )

        painter.setPen(
            QColor("#475569")
        )

        painter.drawText(
            QRectF(
                left_rect.x() + 20,
                left_rect.y() + 15,
                left_rect.width() - 40,
                25,
            ),
            "采集状态",
        )

        painter.drawText(
            QRectF(
                right_rect.x() + 20,
                right_rect.y() + 15,
                right_rect.width() - 40,
                25,
            ),
            "频率",
        )

        value_font = QFont()

        value_font.setPixelSize(19)

        value_font.setBold(True)

        painter.setFont(
            value_font
        )

        if self.capturing:
            capture_text = "正在采集..."

            capture_color = QColor(
                "#1677FF"
            )

        else:
            capture_text = "-"

            capture_color = QColor(
                "#475569"
            )

        painter.setPen(
            capture_color
        )

        painter.drawText(
            QRectF(
                left_rect.x() + 20,
                left_rect.y() + 52,
                left_rect.width() - 40,
                28,
            ),
            capture_text,
        )

        if self.frequency > 0:
            frequency_text = (
                f"{self.frequency:.1f} Hz"
            )

            frequency_color = QColor(
                "#12A05C"
            )

        else:
            frequency_text = "0 Hz"

            frequency_color = QColor(
                "#475569"
            )

        painter.setPen(
            frequency_color
        )

        painter.drawText(
            QRectF(
                right_rect.x() + 20,
                right_rect.y() + 52,
                right_rect.width() - 40,
                28,
            ),
            frequency_text,
        )


class DemoWindow(QWidget):
    """只用于模拟信号和预览效果。"""

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle(
            "皮带机实时监测动画"
        )

        self.resize(
            720,
            650,
        )

        self.setStyleSheet(
            """
            QWidget {
                background: #EEF4FB;
            }

            QPushButton {
                min-height: 36px;
                padding: 4px 14px;

                background: white;

                border:
                    1px solid #CBD5E1;

                border-radius:
                    7px;

                color:
                    #334155;

                font-size:
                    14px;
            }

            QPushButton:hover {
                background:
                    #F8FAFC;
            }
            """
        )

        self.machine_card = BeltMachineCard(
            "1# 皮带机"
        )

        start_button = QPushButton(
            "启动"
        )

        stop_button = QPushButton(
            "停止"
        )

        capture_button = QPushButton(
            "开始采集"
        )

        capture_stop_button = QPushButton(
            "停止采集"
        )

        frequency_button = QPushButton(
            "频率监听"
        )

        frequency_stop_button = QPushButton(
            "停止监听"
        )

        start_button.clicked.connect(
            self.machine_card.start_machine
        )

        stop_button.clicked.connect(
            self.machine_card.stop_machine
        )

        capture_button.clicked.connect(
            self.machine_card.start_capture
        )

        capture_stop_button.clicked.connect(
            self.machine_card.stop_capture
        )

        frequency_button.clicked.connect(
            self._start_frequency
        )

        frequency_stop_button.clicked.connect(
            lambda:
            self.machine_card.set_frequency_listening(
                False
            )
        )

        buttons = QHBoxLayout()

        buttons.addWidget(
            start_button
        )

        buttons.addWidget(
            stop_button
        )

        buttons.addWidget(
            capture_button
        )

        buttons.addWidget(
            capture_stop_button
        )

        buttons.addWidget(
            frequency_button
        )

        buttons.addWidget(
            frequency_stop_button
        )

        layout = QVBoxLayout(
            self
        )

        layout.setContentsMargins(
            20,
            20,
            20,
            20,
        )

        layout.addWidget(
            self.machine_card,
            1,
        )

        layout.addLayout(
            buttons
        )

    def _start_frequency(self) -> None:
        self.machine_card.set_frequency_listening(
            True
        )

        # 模拟后端真正传入的数值。
        self.machine_card.set_frequency(
            12.5
        )


if __name__ == "__main__":
    app = QApplication(
        sys.argv
    )

    window = DemoWindow()

    window.show()

    sys.exit(
        app.exec()
    )