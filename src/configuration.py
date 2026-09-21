"""读取 MVS 相机和测量业务配置。"""

import json
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from contextlib import closing

from repo.machine_repo import MachineRepo

from enums import MachineState


@dataclass(frozen=True)
class MachineConfiguration:
    machine_id: str
    camera_serial: str
    frequency_meter_serial: str
    simulated_frequencies_hz: tuple[float, ...] = ()
    camera_pixel_format: str | None = None
    camera_exposure_time_us: float | None = None
    camera_gain: float | None = None


@dataclass(frozen=True)
class MeasurementConfiguration:
    machines: tuple[MachineConfiguration, ...]
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
    configuration_version: str = "simulation-v1"
    recovery_database_path: Path | None = None
    maintenance_interval_ms: int = 250
    max_persistent_records: int = 1000
    minimum_free_disk_bytes: int = 104857600
    initial_machine_state: MachineState = MachineState.CLOSED
    mvs_dll_directory: Path | None = None

    @property
    def recovery_path(self) -> Path:
        if self.recovery_database_path is not None:
            return self.recovery_database_path
        return self.database_path.with_suffix(".recovery.sqlite3")

    def validate(self) -> None:
        """检查机器绑定和运行参数。"""
        # 检查机器、相机和频率来源的唯一性。
        if not self.machines:
            raise ValueError("没有启用的机器，请先在机器管理页添加并启用机器。")
        for attribute in ("machine_id", "camera_serial", "frequency_meter_serial"):
            identifiers = [getattr(machine, attribute) for machine in self.machines]
            if not all(identifiers) or len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{attribute} 必须非空且不能重复。")

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
        if self.initial_machine_state not in set(MachineState):
            raise ValueError("初始机器状态必须是 CLOSED、OPEN 或 UNKNOWN。")


def load_configuration(configuration_path: Path) -> MeasurementConfiguration:
    """读取公共配置和数据库中的启用机器，构建后台配置。

    Args:
        configuration_path: 公共 JSON 配置文件路径。

    Returns:
        MeasurementConfiguration(
            machines=(MachineConfiguration(
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
            configuration_version="simulation-v1",  # 配置版本
            recovery_database_path=None,  # 运行库路径覆盖值
            maintenance_interval_ms=250,  # 容量检查间隔
            max_persistent_records=1000,  # 存储积压上限
            minimum_free_disk_bytes=104857600,  # 最低剩余空间
            initial_machine_state=MachineState.CLOSED,  # 初始现场状态配置
            mvs_dll_directory=None,  # SDK 动态库搜索目录
        )  # 路径转换为绝对路径，公共参数按实际配置返回
    """
    # 读取 JSON 配置并以配置文件所在目录解析路径。
    configuration_path = configuration_path.resolve()
    configuration_directory = configuration_path.parent
    with configuration_path.open(encoding="utf-8") as configuration_file:
        settings = json.load(configuration_file)

    # 机器清单统一从业务库读取，配置文件只提供公共参数。
    settings.pop("machines", None)
    database_path = (configuration_directory / settings["database_path"]).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    enabled_machines = MachineRepo(database_path).list_enabled()
    machine_configurations = [
        MachineConfiguration(
            machine_id=str(machine["id"]),
            frequency_meter_serial=machine["frequency_meter_serial"],
            camera_serial=machine["camera_serial"],
        )
        for machine in enabled_machines
    ]

    # 将配置中的机器初始状态转换为枚举。
    settings["initial_machine_state"] = MachineState(settings.get("initial_machine_state", MachineState.CLOSED))

    # 创建输出路径并校验完整配置。
    for path_name in ("database_path", "evidence_directory", "mvs_development_directory"):
        settings[path_name] = (configuration_directory / settings[path_name]).resolve()
    if settings.get("recovery_database_path"):
        settings["recovery_database_path"] = (
            configuration_directory / settings["recovery_database_path"]
        ).resolve()
    if settings.get("mvs_dll_directory"):
        settings["mvs_dll_directory"] = (configuration_directory / settings["mvs_dll_directory"]).resolve()
    configuration = MeasurementConfiguration(machines=tuple(machine_configurations), **settings)
    configuration.validate()
    return configuration
