"""独立预览皮带机动画组件及其控制接口。"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.belt_animation import BeltAnimationWidget


class DemoWindow(QWidget):
    """提供动画组件的手动演示窗口。"""

    def __init__(self) -> None:
        """创建动画区域和对应的手动控制按钮。

        Args:
            无。

        Returns:
            返回示例：
                None  # 演示窗口创建完成
        """
        super().__init__()
        self.setWindowTitle("皮带机实时监测动画")
        self.resize(720, 500)
        self.setStyleSheet(
            """
            QWidget { background: #EEF4FB; }
            QPushButton {
                min-height: 36px;
                padding: 4px 14px;
                background: white;
                border: 1px solid #CBD5E1;
                border-radius: 7px;
                color: #334155;
                font-size: 14px;
            }
            QPushButton:hover { background: #F8FAFC; }
            """
        )

        # 创建皮带机动画区域。
        self.animation_widget = BeltAnimationWidget()

        # 创建机器、采集和频率监听控制按钮。
        start_button = QPushButton("启动")
        stop_button = QPushButton("停止")
        capture_button = QPushButton("开始采集")
        capture_stop_button = QPushButton("停止采集")
        frequency_button = QPushButton("频率监听")
        frequency_stop_button = QPushButton("停止监听")
        start_button.clicked.connect(self.animation_widget.start_machine)
        stop_button.clicked.connect(self.animation_widget.stop_machine)
        capture_button.clicked.connect(self.animation_widget.start_capture)
        capture_stop_button.clicked.connect(self.animation_widget.stop_capture)
        frequency_button.clicked.connect(
            lambda: self.animation_widget.set_frequency_listening(True)
        )
        frequency_stop_button.clicked.connect(
            lambda: self.animation_widget.set_frequency_listening(False)
        )

        # 将动画区域和控制按钮放入窗口布局。
        button_layout = QHBoxLayout()
        for button in (
            start_button,
            stop_button,
            capture_button,
            capture_stop_button,
            frequency_button,
            frequency_stop_button,
        ):
            button_layout.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(self.animation_widget, 1)
        layout.addLayout(button_layout)


def main() -> int:
    """启动动画演示窗口。

    Args:
        无。

    Returns:
        返回示例：
            0  # Qt 应用退出码
    """
    application = QApplication(sys.argv)
    window = DemoWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
