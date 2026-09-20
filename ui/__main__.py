"""通过 python -m ui 启动桌面预览。"""

import sys

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def run_desktop_preview() -> int:
    """初始化应用、显示主窗口并运行到窗口关闭。

    Args:
        无。

    Returns:
        返回示例：
            0  # 正常退出时的进程状态码
    """
    # 初始化应用实例和产品信息。
    application = QApplication(sys.argv)
    application.setApplicationName("BeltVision")
    application.setApplicationVersion("1.0.0")

    # 创建并显示桌面主窗口。
    window = MainWindow()
    window.show()

    # 运行事件循环，等待窗口关闭。
    exit_code = application.exec()

    # 安排界面资源释放并返回退出状态码。
    window.deleteLater()
    application.sendPostedEvents()
    return exit_code


if __name__ == "__main__":
    sys.exit(run_desktop_preview())
