
import math
import sys
from enum import Enum, auto

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class BeltState(Enum):
    STOPPED = auto()
    RUNNING = auto()
    FAULT = auto()


class BeltConveyorWidget(QWidget):
    """
    单文件可运行的皮带机动画 Demo。

    效果：
    - 深灰金属皮带
    - 左右滚轮
    - 启动时从中间展开
    - 停止时向中间收拢
    - 运行时皮带纹理移动、滚轮旋转
    - 纸箱/标签沿皮带移动
    - 故障状态覆盖红色故障提示

    集成到现有项目时，主要保留 paintEvent() 和几个状态控制方法即可。
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setMinimumSize(720, 300)

        self.state = BeltState.STOPPED

        # 0.0 ~ 1.0：皮带展开程度
        self.expand_progress = 0.42
        self.expand_target = 0.42

        # 动画相位
        self.belt_phase = 0.0
        self.roller_phase = 0.0
        self.package_phase = 0.18

        self.timer = QTimer(self)
        self.timer.setInterval(16)  # ~60 FPS
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    # ---------- 对外控制 ----------

    def start_belt(self):
        self.state = BeltState.RUNNING
        self.expand_target = 1.0

    def stop_belt(self):
        self.state = BeltState.STOPPED
        self.expand_target = 0.42

    def set_fault(self):
        self.state = BeltState.FAULT
        # 故障时保留当前展开宽度，不强制收缩
        self.expand_target = max(self.expand_progress, 0.72)

    def reset_fault(self):
        self.state = BeltState.STOPPED
        self.expand_target = 0.42

    # ---------- 动画 ----------

    def _tick(self):
        # 平滑展开 / 收拢
        delta = self.expand_target - self.expand_progress
        if abs(delta) > 0.001:
            self.expand_progress += delta * 0.10
        else:
            self.expand_progress = self.expand_target

        if self.state == BeltState.RUNNING:
            self.belt_phase = (self.belt_phase + 2.8) % 1000.0
            self.roller_phase = (self.roller_phase + 0.10) % (2 * math.pi)
            self.package_phase += 0.0029
            if self.package_phase > 0.88:
                self.package_phase = 0.10

        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()

        # 背景
        painter.fillRect(self.rect(), QColor("#F5F7FA"))

        # 标题
        painter.setPen(QColor("#182230"))
        title_font = QFont("Microsoft YaHei")
        title_font.setPointSize(12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(24, 34, "工业皮带动画 Demo")

        painter.setPen(QColor("#667085"))
        sub_font = QFont("Microsoft YaHei")
        sub_font.setPointSize(9)
        painter.setFont(sub_font)
        painter.drawText(24, 56, "运行时滚轮、皮带纹理和工件同步运动")

        # 卡片
        card = QRectF(22, 76, w - 44, h - 98)
        painter.setPen(QPen(QColor("#E5E9F0"), 1))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(card, 16, 16)

        # 皮带占据卡片中部
        cx = card.center().x()
        belt_max_width = max(320.0, card.width() - 100.0)
        belt_width = 255.0 + (belt_max_width - 255.0) * self.expand_progress
        belt_left = cx - belt_width / 2
        belt_top = card.top() + 62
        belt_height = 82.0

        # 轻微阴影
        shadow_rect = QRectF(belt_left + 8, belt_top + 14, belt_width - 16, belt_height + 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 20))
        painter.drawRoundedRect(shadow_rect, 26, 26)

        # 外壳
        outer = QRectF(belt_left, belt_top, belt_width, belt_height)
        outer_grad = QLinearGradient(0, belt_top, 0, belt_top + belt_height)
        outer_grad.setColorAt(0.0, QColor("#44515E"))
        outer_grad.setColorAt(0.34, QColor("#313A44"))
        outer_grad.setColorAt(0.72, QColor("#222A31"))
        outer_grad.setColorAt(1.0, QColor("#171D22"))

        painter.setPen(QPen(QColor("#161C22"), 1.6))
        painter.setBrush(outer_grad)
        painter.drawRoundedRect(outer, belt_height / 2, belt_height / 2)

        # 上层皮带面
        top_surface = QRectF(
            belt_left + 20,
            belt_top + 9,
            belt_width - 40,
            belt_height * 0.43,
        )
        belt_grad = QLinearGradient(0, top_surface.top(), 0, top_surface.bottom())
        belt_grad.setColorAt(0.0, QColor("#34414D"))
        belt_grad.setColorAt(0.45, QColor("#202932"))
        belt_grad.setColorAt(1.0, QColor("#11171C"))

        painter.setPen(QPen(QColor("#66717C"), 1))
        painter.setBrush(belt_grad)
        painter.drawRoundedRect(top_surface, 14, 14)

        # 皮带上沿高光
        highlight = QLinearGradient(
            top_surface.left(), 0, top_surface.right(), 0
        )
        highlight.setColorAt(0.0, QColor(255, 255, 255, 12))
        highlight.setColorAt(0.5, QColor(255, 255, 255, 55))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 10))
        painter.setPen(QPen(highlight, 1.4))
        painter.drawLine(
            QPointF(top_surface.left() + 15, top_surface.top() + 4),
            QPointF(top_surface.right() - 15, top_surface.top() + 4),
        )

        # 运行中的斜向纹理
        painter.save()
        belt_clip = QPainterPath()
        belt_clip.addRoundedRect(top_surface, 14, 14)
        painter.setClipPath(belt_clip)

        stripe_pen = QPen(QColor(255, 255, 255, 16), 9)
        stripe_pen.setCapStyle(Qt.FlatCap)
        painter.setPen(stripe_pen)

        spacing = 46
        offset = int(self.belt_phase) % spacing
        x = top_surface.left() - 100 + offset
        while x < top_surface.right() + 100:
            painter.drawLine(
                QPointF(x, top_surface.top() - 10),
                QPointF(x + 34, top_surface.bottom() + 10),
            )
            x += spacing
        painter.restore()

        # 下部金属边框
        rail_y = belt_top + belt_height * 0.66
        painter.setPen(QPen(QColor("#77818B"), 1.2))
        painter.drawLine(
            QPointF(belt_left + 34, rail_y),
            QPointF(belt_left + belt_width - 34, rail_y),
        )

        painter.setPen(QPen(QColor("#11161B"), 2.2))
        painter.drawLine(
            QPointF(belt_left + 34, rail_y + 7),
            QPointF(belt_left + belt_width - 34, rail_y + 7),
        )

        # 小铆钉
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#9099A2"))
        rivet_count = max(4, int(belt_width / 90))
        for i in range(rivet_count):
            t = i / max(1, rivet_count - 1)
            x = belt_left + 58 + t * (belt_width - 116)
            painter.drawEllipse(QPointF(x, rail_y + 5), 2.2, 2.2)

        # 左右滚轮
        roller_radius = 20.0
        left_center = QPointF(belt_left + 26, belt_top + belt_height / 2 + 5)
        right_center = QPointF(belt_left + belt_width - 26, belt_top + belt_height / 2 + 5)

        self._draw_roller(painter, left_center, roller_radius)
        self._draw_roller(painter, right_center, roller_radius)

        # 纸箱 / 标签块
        if belt_width > 360:
            self._draw_package(
                painter,
                top_surface,
                belt_left,
                belt_width,
            )

        # 状态胶囊
        self._draw_state_badge(painter, card)

        # 故障覆盖
        if self.state == BeltState.FAULT:
            overlay = QRectF(
                card.center().x() - 86,
                belt_top + 23,
                172,
                38,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(80, 90, 102, 230))
            painter.drawRoundedRect(overlay, 19, 19)

            painter.setPen(QColor("#FFFFFF"))
            fault_font = QFont("Microsoft YaHei")
            fault_font.setPointSize(10)
            fault_font.setBold(True)
            painter.setFont(fault_font)
            painter.drawText(overlay, Qt.AlignCenter, "●  无图像信号")

    def _draw_roller(self, painter, center, radius):
        # 外圈
        outer = QLinearGradient(
            center.x() - radius,
            center.y() - radius,
            center.x() + radius,
            center.y() + radius,
        )
        outer.setColorAt(0.0, QColor("#C8D0D8"))
        outer.setColorAt(0.35, QColor("#8B96A1"))
        outer.setColorAt(0.72, QColor("#59636D"))
        outer.setColorAt(1.0, QColor("#E2E6EA"))

        painter.setPen(QPen(QColor("#10161B"), 1.8))
        painter.setBrush(outer)
        painter.drawEllipse(center, radius, radius)

        # 内圈
        painter.setPen(QPen(QColor("#5A6570"), 1))
        painter.setBrush(QColor("#D9DEE3"))
        painter.drawEllipse(center, radius * 0.54, radius * 0.54)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#343D45"))
        painter.drawEllipse(center, radius * 0.20, radius * 0.20)

        # 旋转刻度
        painter.setPen(QPen(QColor("#46515B"), 2.2))
        spoke_r1 = radius * 0.58
        spoke_r2 = radius * 0.82
        for i in range(4):
            a = self.roller_phase + i * math.pi / 2
            p1 = QPointF(
                center.x() + math.cos(a) * spoke_r1,
                center.y() + math.sin(a) * spoke_r1,
            )
            p2 = QPointF(
                center.x() + math.cos(a) * spoke_r2,
                center.y() + math.sin(a) * spoke_r2,
            )
            painter.drawLine(p1, p2)

    def _draw_package(self, painter, top_surface, belt_left, belt_width):
        usable = max(40.0, belt_width - 210.0)
        x = belt_left + 92 + usable * self.package_phase
        y = top_surface.top() - 8

        box_w = 82
        box_h = 40
        box = QRectF(x - box_w / 2, y - box_h / 2, box_w, box_h)

        # 阴影
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 26))
        painter.drawRoundedRect(box.translated(3, 4), 5, 5)

        # 正面
        box_grad = QLinearGradient(box.left(), box.top(), box.left(), box.bottom())
        box_grad.setColorAt(0.0, QColor("#D9B783"))
        box_grad.setColorAt(1.0, QColor("#B78951"))

        painter.setPen(QPen(QColor("#8A673E"), 1))
        painter.setBrush(box_grad)
        painter.drawRoundedRect(box, 5, 5)

        # 顶面亮边
        painter.setPen(QPen(QColor("#F1D19E"), 1))
        painter.drawLine(
            QPointF(box.left() + 4, box.top() + 5),
            QPointF(box.right() - 4, box.top() + 5),
        )

        # 标签
        label_rect = QRectF(box.left() + 16, box.top() + 11, box.width() - 32, 18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#D2AA73"))
        painter.drawRoundedRect(label_rect, 3, 3)

        painter.setPen(QColor("#1F252B"))
        font = QFont("Consolas")
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(label_rect, Qt.AlignCenter, "HB320")

    def _draw_state_badge(self, painter, card):
        if self.state == BeltState.RUNNING:
            text = "运行中"
            fg = QColor("#178647")
            bg = QColor("#E7F7EC")
        elif self.state == BeltState.FAULT:
            text = "故障"
            fg = QColor("#D92D20")
            bg = QColor("#FEECEB")
        else:
            text = "已停止"
            fg = QColor("#667085")
            bg = QColor("#F2F4F7")

        badge = QRectF(card.right() - 92, card.top() + 18, 68, 28)

        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(badge, 14, 14)

        painter.setPen(fg)
        font = QFont("Microsoft YaHei")
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(badge, Qt.AlignCenter, text)


class DemoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Belt OCR - Conveyor Animation Demo")
        self.resize(920, 470)

        root = QWidget()
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.belt = BeltConveyorWidget()
        layout.addWidget(self.belt, 1)

        controls = QHBoxLayout()
        controls.setSpacing(10)

        start_btn = QPushButton("启动")
        stop_btn = QPushButton("停止")
        fault_btn = QPushButton("模拟故障")
        reset_btn = QPushButton("复位")

        start_btn.clicked.connect(self.belt.start_belt)
        stop_btn.clicked.connect(self.belt.stop_belt)
        fault_btn.clicked.connect(self.belt.set_fault)
        reset_btn.clicked.connect(self.belt.reset_fault)

        for btn in (start_btn, stop_btn, fault_btn, reset_btn):
            btn.setMinimumHeight(36)
            controls.addWidget(btn)

        controls.addStretch(1)

        tip = QLabel("提示：启动后皮带展开并运动；停止后向中间收拢。")
        tip.setStyleSheet("color:#667085;")
        controls.addWidget(tip)

        layout.addLayout(controls)

        self.setStyleSheet("""
            QMainWindow {
                background: #F5F7FA;
            }

            QPushButton {
                min-width: 86px;
                padding: 7px 14px;
                border: 1px solid #D0D5DD;
                border-radius: 8px;
                background: #FFFFFF;
                color: #344054;
                font-family: "Microsoft YaHei";
                font-size: 13px;
            }

            QPushButton:hover {
                background: #F9FAFB;
                border-color: #98A2B3;
            }

            QPushButton:pressed {
                background: #F2F4F7;
            }
        """)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = DemoWindow()
    window.show()

    sys.exit(app.exec())
