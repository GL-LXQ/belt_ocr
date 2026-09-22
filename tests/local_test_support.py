"""为自写用例提供独立的配置、机器库和相机替身。"""

import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from config_util import AppConfig
from camera.hikrobot_sdk import CameraFrame
from repo.machine_repo import MachineRepo


def build_config(directory: Path, **overrides) -> AppConfig:
    """按测试目录组装公共运行配置。

    Args:
        directory: 测试临时目录。
        **overrides: 需要覆盖的运行参数，例如 capture_window_ms。

    Returns:
        返回示例：
            AppConfig(
                database_path=Path("tmp/data/measurements.sqlite3"),  # 业务库文件
                evidence_directory=Path("tmp/evidence"),  # 图片目录
                mvs_development_directory=Path("tmp/sdk"),  # SDK 目录占位
                shutdown_timeout_ms=2000,  # 缩短退出等待
            )
    """
    config = AppConfig(
        database_path=directory / "data" / "measurements.sqlite3",
        evidence_directory=directory / "evidence",
        mvs_development_directory=directory / "sdk",
        shutdown_timeout_ms=2000,
        modbus_serial_port="COM-TEST",
        io_machine_channels={str(machine_number): machine_number - 1 for machine_number in range(1, 11)},
    )
    return replace(config, **overrides)


def create_machine_database(database_path: Path, machines: list[dict]) -> list[int]:
    """建好机器表并按顺序写入测试机器。

    Args:
        database_path: 测试业务库文件路径。
        machines: 逐台机器的字段，支持 machine_name、camera_serial、frequency_meter_serial 和 enabled。

    Returns:
        返回示例：
            [1, 2]  # 新增机器的自增编号，顺序与传入列表一致
    """
    # 创建业务库目录并建好机器表。
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)

    # 按传入顺序写入机器并返回自增编号。
    machine_repo = MachineRepo(database_path)
    return [
        machine_repo.insert(
            machine["machine_name"],
            machine["camera_serial"],
            machine["frequency_meter_serial"],
            machine.get("enabled", True),
        )
        for machine in machines
    ]


class FakeCameraDevice:
    """按固定间隔交付整轮帧的测试相机设备。"""

    def __init__(self, serial: str) -> None:
        """登记序列号并初始化设备状态。

        Args:
            serial: 相机序列号。

        Returns:
            返回示例：
                None  # 设备未关闭、未取流
        """
        # 保存序列号、设备状态和取流占用锁。
        self.serial = serial
        self.closed = False
        self.faulted = False
        self.grabbing = False
        self.received_frame_count = 0
        self.capture_lock = threading.Lock()

    def start_grabbing(self) -> None:
        """标记设备已进入取流状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 设备已取流
        """
        self.grabbing = True

    def read_frame(self, stop_requested: threading.Event, timeout_ms: int) -> CameraFrame | None:
        """等待一个取帧间隔后交付一帧，停止通知置位时返回 None。

        Args:
            stop_requested: 本轮停止信号。
            timeout_ms: 单次取帧超时毫秒数，测试忽略。

        Returns:
            返回示例：
                CameraFrame(
                    camera_serial="CAM-A",  # 相机序列号
                    frame_number=1,  # 帧编号
                    device_timestamp=100,  # 设备时间戳
                    host_timestamp=200,  # 主机时间戳
                    received_monotonic=1.5,  # 接收单调时间
                    width=2,  # 图像宽度
                    height=2,  # 图像高度
                    pixel_type=17301505,  # Mono8 像素格式
                    lost_packet_count=0,  # 丢包数
                    data=b"\x10\x20\x30\x40",  # 独立图像字节
                )
        """
        # 停止通知已置位时不再取帧。
        if stop_requested.is_set():
            return None

        # 模拟取帧耗时并累计帧数。
        time.sleep(0.005)
        self.received_frame_count += 1
        return CameraFrame(
            camera_serial=self.serial,
            frame_number=self.received_frame_count,
            device_timestamp=self.received_frame_count * 100,
            host_timestamp=self.received_frame_count * 200,
            received_monotonic=time.monotonic(),
            width=2,
            height=2,
            pixel_type=17301505,
            lost_packet_count=0,
            data=b"\x10\x20\x30\x40",
        )

    def stop_grabbing(self) -> None:
        """标记设备已停止取流。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 设备已停流
        """
        self.grabbing = False

    def encode_image(self, frame: CameraFrame) -> bytes:
        """返回带图像字节的测试 BMP 内容。

        Args:
            frame: 待编码的原始帧。

        Returns:
            返回示例：
                b"BM..."  # 带测试图像字节的 BMP 内容
        """
        # 用 BMP 文件头占位和原始图像字节拼出测试图片。
        return b"BM" + bytes(52) + frame.data

    def close(self) -> None:
        """标记设备已关闭。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 设备已关闭
        """
        self.closed = True


class FakeMvsSdk:
    """按序列号打开测试相机，并在关闭时释放全部设备。"""

    def __init__(self, *arguments) -> None:
        """创建空的相机集合和关闭状态。

        Args:
            arguments: 应用传入的 SDK 目录参数，测试忽略。

        Returns:
            返回示例：
                None  # 相机集合与关闭状态已就绪
        """
        self.cameras: dict[str, FakeCameraDevice] = {}
        self.closed = False

    def open_camera(
        self,
        serial: str,
        pixel_format: str | None = None,
        exposure_time_us: float | None = None,
        gain: float | None = None,
    ) -> FakeCameraDevice:
        """按序列号建立并登记测试相机。

        Args:
            serial: 相机序列号。
            pixel_format: 应用传入的像素格式，测试忽略。
            exposure_time_us: 应用传入的曝光时间，测试忽略。
            gain: 应用传入的增益，测试忽略。

        Returns:
            返回示例：
                FakeCameraDevice  # 已登记到相机集合的测试设备
        """
        # 建立测试设备并登记到相机集合。
        camera = FakeCameraDevice(serial)
        self.cameras[serial] = camera
        return camera

    def close(self) -> None:
        """关闭全部测试相机并标记 SDK 已关闭。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 全部设备已关闭
        """
        # 逐台关闭相机，再标记 SDK 已关闭。
        for camera in self.cameras.values():
            camera.close()
        self.closed = True
