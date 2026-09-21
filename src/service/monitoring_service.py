"""在后台线程运行监测服务并向界面发送相机连接结果。"""

import asyncio
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app import App
from config_util import load_configuration


class MonitoringService(QThread):
    """管理一次监测服务的启动、等待和资源释放。"""

    # 相机连接状态信号，参数依次为机器编号、连接状态和失败原因。
    camera_state_changed_signal = Signal(str, str, str)

    def __init__(self, configuration_directory: Path):
        """保存配置路径并创建跨线程停止通知。

        Args:
            configuration_directory: 公共 YAML 配置目录。

        Returns:
            None  # 后台线程已准备，尚未启动
        """
        super().__init__()
        self.configuration_directory = configuration_directory
        self.stop_requested = threading.Event()
        self.failure_message = ""

    def run(self):
        """运行后台事件循环并保存失败原因。

        Args:
            无。

        Returns:
            None  # 事件循环结束，QThread 随后发送 finished 信号
        """
        # 在线程中运行监测主流程，将异常交给界面展示。
        try:
            asyncio.run(self.run_monitoring())
        except Exception as error:
            self.failure_message = str(error)

    async def run_monitoring(self):
        """读取机器、连接相机、等待停止并统一释放资源。

        Args:
            无。

        Returns:
            None  # 监测结束，相机及数据库资源已释放
        """
        # 从业务库构建机器配置并创建后台处理器。
        config = load_configuration(self.configuration_directory)
        application = App(config)
        try:
            # 连接相机并等待停止通知，不发送模拟启停信号。
            await application.start(self.camera_state_changed_signal.emit)
            while not self.stop_requested.is_set() and application.failure is None:
                await asyncio.sleep(0.1)
        finally:
            # 等待已有后台流程退出并释放相机和数据库。
            await application.stop()

        # 将运行或资源释放故障交给线程入口。
        if application.failure is not None:
            raise application.failure
