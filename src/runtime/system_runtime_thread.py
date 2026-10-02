"""在后台线程运行 SystemRuntime 并发送实时通知。"""

import asyncio
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from config_util import load_config
from runtime.system_runtime import SystemRuntime
import logging

logger = logging.getLogger(__name__)


class SystemRuntimeThread(QThread):
    """在后台线程启动、等待并停止 SystemRuntime。"""

    # 相机连接状态信号，参数依次为机器编号、连接状态和失败原因。
    camera_state_changed_signal = Signal(str, str, str)

    # 本轮进度信号，参数依次为机器编号、周期编号、处理阶段和阶段状态。
    measurement_progress_changed_signal = Signal(str, str, str, str)

    # 本轮关闭信号，参数依次为机器编号和周期编号。
    cycle_closed_signal = Signal(str, str)

    # 最终文字信号，参数依次为机器编号、周期编号、正式识别文字。
    ocr_result_changed_signal = Signal(str, str, tuple)

    # 机器整体状态信号，参数依次为机器编号和整体状态标识。
    machine_status_changed_signal = Signal(str, str)

    def __init__(self, configuration_directory: Path) -> None:
        """保存配置路径并创建跨线程停止通知。

        Args:
            configuration_directory: 公共 YAML 配置目录。

        Returns:
            None  # 后台线程已准备，尚未启动
        """
        # 初始化 Qt 线程基类。
        super().__init__()

        # 保存配置目录。
        self.configuration_directory = configuration_directory

        # 创建跨线程停止通知并初始化故障文案。
        self.stop_requested = threading.Event()
        self.failure_message = ""

    def run(self) -> None:
        """运行后台事件循环并保存失败原因。

        Args:
            无外部参数。

        Returns:
            None  # 事件循环结束，QThread 随后发送 finished 信号
        """
        # 记录本线程开始运行监测主流程。
        logger.info("监测后台线程启动")
        try:
            # 在本线程中运行监测主流程。
            asyncio.run(self.run_monitoring())
        except Exception as error:
            logger.exception("监测后台线程运行失败")
            # 保存失败原因供 Controller 读取。
            self.failure_message = str(error)

        # 记录本线程已结束，无故障时 failure 为空。
        logger.info("监测后台线程结束 failure=%s", self.failure_message)

    async def run_monitoring(self) -> None:
        """启动 Runtime、等待停止并统一释放资源。

        Args:
            无外部参数。

        Returns:
            None  # 监测结束，Runtime 资源已释放
        """
        # 读取公共配置。
        configuration = load_config(self.configuration_directory)

        # 创建 Runtime。
        system_runtime = SystemRuntime(configuration)
        try:
            # 启动 Runtime 并接入相机、进度、文字、周期关闭和机器整体状态信号。
            await system_runtime.start(
                self.camera_state_changed_signal.emit,
                self.measurement_progress_changed_signal.emit,
                self.ocr_result_changed_signal.emit,
                self.cycle_closed_signal.emit,
                self.machine_status_changed_signal.emit,
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
