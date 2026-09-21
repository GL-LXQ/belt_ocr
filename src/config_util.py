"""读取 MVS 相机和测量业务配置。"""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

import yaml

from enums import MachineState
from repo.machine_repo import MachineRepo


@dataclass(frozen=True)
class MachineConfig:
    machine_id: str
    camera_serial: str
    frequency_meter_serial: str
    simulated_frequencies_hz: tuple[float, ...] = ()
    camera_pixel_format: str | None = None
    camera_exposure_time_us: float | None = None
    camera_gain: float | None = None


@dataclass(frozen=True)
class AppConfig:
    machines: tuple[MachineConfig, ...]
    database_path: Path
    evidence_directory: Path
    mvs_development_directory: Path
    capture_window_ms: int = 1000
    camera_timeout_ms: int = 50
    frequency_interval_ms: int = 100
    minimum_frequency_hz: float = 0.01
    maximum_frequency_hz: float = 10000.0
    ocr_result_timeout_ms: int = 30000
    max_cycle_open_ms: int = 60000
    event_queue_capacity: int = 128
    storage_queue_capacity: int = 32
    shutdown_timeout_ms: int = 10000
    recovery_database_path: Path | None = None
    maintenance_interval_ms: int = 250
    max_persistent_records: int = 1000
    minimum_free_disk_bytes: int = 104857600
    initial_machine_state: MachineState = MachineState.CLOSED
    mvs_dll_directory: Path | None = None

    @property
    def recovery_path(self) -> Path:
        """取得显式配置或按业务库名称生成的运行库路径。

        Args:
            无。

        Returns:
            Path("runtime/measurements.recovery.sqlite3")  # 运行库路径
        """
        if self.recovery_database_path is not None:
            return self.recovery_database_path
        return self.database_path.with_suffix(".recovery.sqlite3")

    def validate(self) -> None:
        """检查机器绑定和运行参数。

        Args:
            无。

        Returns:
            None  # 配置符合运行要求，非法配置抛出 ValueError
        """
        # 检查至少有一台启用机器，并检查相机和频率仪序列号非空。
        if not self.machines:
            raise ValueError("没有启用的机器，请先在机器管理页添加并启用机器。")
        for attribute in ("camera_serial", "frequency_meter_serial"):
            if not all(getattr(machine, attribute) for machine in self.machines):
                raise ValueError(f"{attribute} 必须非空。")

        # 检查等待期限、采集间隔和队列容量。
        positive_parameters = (
            "capture_window_ms", "camera_timeout_ms",
            "frequency_interval_ms",
            "ocr_result_timeout_ms", "max_cycle_open_ms", "event_queue_capacity",
            "storage_queue_capacity",
            "shutdown_timeout_ms",
            "maintenance_interval_ms",
            "max_persistent_records",
        )
        for parameter in positive_parameters:
            if getattr(self, parameter) <= 0:
                raise ValueError(f"{parameter} 必须大于零。")
        if not 0 < self.minimum_frequency_hz < self.maximum_frequency_hz:
            raise ValueError("频率范围必须是递增的正数范围。")
        if self.minimum_free_disk_bytes < 0:
            raise ValueError("磁盘保留空间不能为负数。")
        if self.recovery_path.resolve() == self.database_path.resolve():
            raise ValueError("恢复库与最终结果库必须使用不同文件。")


def read_configuration_settings(configuration_directory: Path) -> dict:
    """读取单一配置文件的五个业务段落并转换路径和初始机器状态。

    Args:
        configuration_directory: 包含 config.yaml 的配置目录。

    Returns:
        返回示例（可选参数按 YAML 实际内容返回）：
            {
                "database_path": Path("D:/belt_ocr/runtime/measurements.sqlite3"),  # 业务库绝对路径
                "recovery_database_path": Path("D:/belt_ocr/runtime/measurements.recovery.sqlite3"),  # 运行库绝对路径
                "evidence_directory": Path("D:/belt_ocr/runtime/evidence"),  # 图片绝对目录
                "storage_queue_capacity": 32,  # 存储队列容量
                "shutdown_timeout_ms": 10000,  # 退出期限，毫秒
                "maintenance_interval_ms": 250,  # 容量检查间隔，毫秒
                "max_persistent_records": 1000,  # 存储积压上限
                "minimum_free_disk_bytes": 104857600,  # 最低磁盘空间，字节
                "mvs_development_directory": Path("D:/app/HIK/MVS/Development"),  # SDK 目录
                "mvs_dll_directory": None,  # 使用默认动态库目录
                "capture_window_ms": 1000,  # 采集窗口，毫秒
                "camera_timeout_ms": 50,  # 取帧超时，毫秒
                "ocr_result_timeout_ms": 30000,  # 整轮识别期限，毫秒
                "frequency_interval_ms": 100,  # 频率读取间隔配置
                "minimum_frequency_hz": 0.01,  # 最低有效频率
                "maximum_frequency_hz": 10000.0,  # 最高有效频率
                "initial_machine_state": MachineState.CLOSED,  # 初始机器状态
                "max_cycle_open_ms": 60000,  # 周期关闭期限，毫秒
                "event_queue_capacity": 128,  # 单机事件队列容量
            }
    """
    # 读取单一配置 YAML，按固定段落顺序合并公共参数，拒绝跨段落重复配置键。
    configuration_directory = configuration_directory.resolve()
    with (configuration_directory / "config.yaml").open(encoding="utf-8") as config_file:
        section_settings = yaml.safe_load(config_file)
    settings = {}
    source_sections: dict[str, str] = {}
    for section_name in ("application", "camera", "ocr", "frequency", "machine"):
        for name, value in (section_settings.get(section_name) or {}).items():
            if name in source_sections:
                raise ValueError(f"配置项 {name} 同时出现在配置段 {source_sections[name]} 和 {section_name}。")
            source_sections[name] = section_name
            settings[name] = value

    # 将必填路径转换为相对配置目录的绝对路径。
    for path_name in ("database_path", "evidence_directory", "mvs_development_directory"):
        settings[path_name] = (configuration_directory / settings[path_name]).resolve()

    # 转换可选路径与机器初始状态。
    for path_name in ("recovery_database_path", "mvs_dll_directory"):
        if settings.get(path_name):
            settings[path_name] = (configuration_directory / settings[path_name]).resolve()
    settings["initial_machine_state"] = MachineState(settings.get("initial_machine_state", MachineState.CLOSED))
    return settings


def load_configuration(configuration_directory: Path) -> AppConfig:
    """读取公共配置和数据库中的启用机器，构建后台配置。

    Args:
        configuration_directory: 包含 config.yaml 的配置目录。

    Returns:
        AppConfig(
            machines=(MachineConfig(
                machine_id="1",  # 数据库机器编号
                frequency_meter_serial="FREQ001",  # 频率仪序列号
                camera_serial="CAM001",  # 用于连接的相机序列号
                simulated_frequencies_hz=(),  # 不生成模拟频率
                camera_pixel_format=None,  # 保留相机像素格式
                camera_exposure_time_us=None,  # 保留相机曝光
                camera_gain=None,  # 保留相机增益
            ),),
            database_path=Path("runtime/measurements.sqlite3"),  # 业务库绝对路径
            evidence_directory=Path("runtime/evidence"),  # 图片目录绝对路径
            mvs_development_directory=Path("D:/app/HIK/MVS/Development"),  # SDK 路径
            capture_window_ms=1000,  # 采集窗口
            camera_timeout_ms=50,  # 单次取帧超时
            frequency_interval_ms=100,  # 频率读取间隔配置
            minimum_frequency_hz=0.01,  # 最低有效频率
            maximum_frequency_hz=10000.0,  # 最高有效频率
            ocr_result_timeout_ms=30000,  # 整轮识别期限
            max_cycle_open_ms=60000,  # 周期关闭期限
            event_queue_capacity=128,  # 单机事件队列容量
            storage_queue_capacity=32,  # 存储队列容量
            shutdown_timeout_ms=10000,  # 退出收尾期限
            recovery_database_path=None,  # 运行库路径覆盖值
            maintenance_interval_ms=250,  # 容量检查间隔
            max_persistent_records=1000,  # 存储积压上限
            minimum_free_disk_bytes=104857600,  # 最低剩余空间
            initial_machine_state=MachineState.CLOSED,  # 初始现场状态配置
            mvs_dll_directory=None,  # SDK 动态库搜索目录
        )  # 路径转换为绝对路径，公共参数按实际配置返回
    """
    # 读取公共参数并取得业务库路径。
    settings = read_configuration_settings(configuration_directory)

    # 创建业务库目录并初始化机器表。
    database_path = settings["database_path"]
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)

    # 读取启用机器并整理为逐机配置对象。
    enabled_machines = MachineRepo(database_path).list_enabled()
    machine_configs = [
        MachineConfig(
            machine_id=str(machine["id"]),
            frequency_meter_serial=machine["frequency_meter_serial"],
            camera_serial=machine["camera_serial"],
        )
        for machine in enabled_machines
    ]

    # 组装机器与公共参数并校验完整运行配置。
    config = AppConfig(machines=tuple(machine_configs), **settings)
    config.validate()
    return config
