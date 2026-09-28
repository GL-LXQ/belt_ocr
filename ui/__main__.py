"""通过 python -m ui 启动桌面预览。"""

import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# 将后端模块目录加入搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication

from config_util import AppConfig, read_configuration_settings

from src.controller.controller import AppController
from src.service.abnormal_event_service import AbnormalEventService
from src.service.machine_service import MachineService
from src.service.measurement_history_service import MeasurementHistoryService
from src.repo.abnormal_event_repo import AbnormalEventRepo
from src.repo.machine_repo import MachineRepo
from src.repo.measurement_record_repo import MeasurementRecordRepo
from ui.main_window import MainWindow


def run_desktop_preview() -> int:
    """初始化应用、显示主窗口并运行到窗口关闭。

    Args:
        无。

    Returns:
        返回示例：
            0  # 正常退出时的进程状态码
    """
    # 配置桌面入口的终端日志格式。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # 初始化应用实例和产品信息。
    application = QApplication(sys.argv)
    application.setApplicationName("BeltVision")
    application.setApplicationVersion("1.0.0")

    # 从项目配置读取业务库路径并初始化机器与测量结果表。
    configuration_directory = Path(__file__).resolve().parents[1] / "config"
    settings = read_configuration_settings(configuration_directory)
    database_path = settings["database_path"]
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)

    # 从现有配置取得运行库路径并初始化异常事件表。
    recovery_database_path = AppConfig(**settings).recovery_path
    recovery_database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(recovery_database_path)) as connection, connection:
        AbnormalEventRepo.create_table(connection)

    # 创建机器、历史记录和异常事件服务。
    machine_repo = MachineRepo(database_path)
    machine_service = MachineService(machine_repo)
    measurement_history_service = MeasurementHistoryService(
        MeasurementRecordRepo(database_path)
    )
    abnormal_event_service = AbnormalEventService(
        AbnormalEventRepo(recovery_database_path)
    )

    # 组装界面业务控制器并创建主窗口。
    controller = AppController(
        machine_service,
        measurement_history_service,
        abnormal_event_service,
        configuration_directory,
    )
    window = MainWindow(controller)
    window.show()

    # 运行事件循环，等待窗口关闭。
    exit_code = application.exec()

    # 安排界面资源释放并返回退出状态码。
    window.deleteLater()
    application.sendPostedEvents()
    return exit_code


if __name__ == "__main__":
    sys.exit(run_desktop_preview())
