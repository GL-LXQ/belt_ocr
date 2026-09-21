"""通过 python -m ui 启动桌面预览。"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# 将后端模块目录加入搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication

from src.service.machine_service import MachineService
from src.repo.machine_repo import MachineRepo
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

    # 从项目配置读取业务库路径并初始化机器表。
    configuration_path = Path(__file__).resolve().parents[1] / "config.example.json"
    settings = json.loads(configuration_path.read_text(encoding="utf-8"))
    database_path = configuration_path.parent / settings["database_path"]
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    # 创建数据访问对象和机器业务服务。
    machine_repo = MachineRepo(database_path)
    machine_service = MachineService(machine_repo)

    # 创建并显示桌面主窗口。
    window = MainWindow(machine_service)
    window.show()

    # 运行事件循环，等待窗口关闭。
    exit_code = application.exec()

    # 安排界面资源释放并返回退出状态码。
    window.deleteLater()
    application.sendPostedEvents()
    return exit_code


if __name__ == "__main__":
    sys.exit(run_desktop_preview())
