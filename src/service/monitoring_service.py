"""在后台线程运行监测服务并发送相机、进度、文字和周期关闭通知。"""

import asyncio
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from system_runtime import SystemRuntime
from config_util import load_config


class MonitoringService(QThread):
    """管理一次监测服务的启动、等待和资源释放。"""

    # 相机连接状态信号，参数依次为机器编号、连接状态和失败原因。
    camera_state_changed_signal = Signal(str, str, str)

    # 本轮进度信号，参数依次为机器编号、周期编号、处理阶段和阶段状态。
    measurement_progress_changed_signal = Signal(str, str, str, str)

    # 本轮关闭信号，参数依次为机器编号和周期编号。
    cycle_closed_signal = Signal(str, str)

    # 最终文字信号，参数依次为机器编号、周期编号、原文字和去空格文字。
    ocr_result_changed_signal = Signal(str, str, tuple, tuple)

    def __init__(self, configuration_directory: Path):
        """保存配置路径并创建跨线程停止通知。

        Args:
            configuration_directory: 公共 YAML 配置目录。

        Returns:
            返回示例：
                None  # 后台线程已准备，尚未启动
        """
        # 初始化 Qt 线程基类。
        super().__init__()

        # 保存配置目录。
        self.configuration_directory = configuration_directory

        # 创建跨线程停止通知并初始化故障文案。
        self.stop_requested = threading.Event()
        self.failure_message = ""

    def run(self):
        """运行后台事件循环并保存失败原因。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 事件循环结束，QThread 随后发送 finished 信号
        """
        try:
            # 在本线程中运行监测主流程。
            asyncio.run(self.run_monitoring())
        except Exception as error:
            # 保存失败原因供界面读取。
            self.failure_message = str(error)

    async def run_monitoring(self):
        """读取机器、连接相机、等待停止并统一释放资源。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 监测结束，相机及数据库资源已释放
        """
        # 读取公共配置并创建运行时对象。
        config = load_config(self.configuration_directory)
        system_runtime = SystemRuntime(config)
        try:
            # 连接相机并接入状态、进度、文字和周期关闭信号。
            await system_runtime.start(
                self.camera_state_changed_signal.emit,
                self.measurement_progress_changed_signal.emit,
                self.ocr_result_changed_signal.emit,
                self.cycle_closed_signal.emit,
            )

            # 轮询停止请求与后台故障，任一出现时结束等待。
            while not self.stop_requested.is_set() and system_runtime.failure is None:
                await asyncio.sleep(0.1)
        finally:
            # 等待已有后台流程退出并释放相机和数据库。
            await system_runtime.stop()

        # 将运行或资源释放故障交给线程入口。
        if system_runtime.failure is not None:
            raise system_runtime.failure
