"""读取 MVS 相机和测量业务配置。"""

import json
from dataclasses import dataclass
from pathlib import Path

from enums import MachineState


@dataclass(frozen=True)
class MachineConfiguration:
    machine_id: str
    camera_id: str
    frequency_source_id: str
    simulated_frequencies_hz: tuple[float, ...] = ()
    camera_serial: str = ""
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
    max_pending_sessions_per_machine: int = 20
    shutdown_timeout_ms: int = 10000
    configuration_version: str = "simulation-v1"
    recovery_database_path: Path | None = None
    maintenance_interval_ms: int = 250
    max_persistent_records: int = 1000
    minimum_free_disk_bytes: int = 104857600
    event_max_age_ms: int = 30000
    initial_machine_state: MachineState = MachineState.CLOSED
    mvs_dll_directory: Path | None = None

    @property
    def recovery_path(self) -> Path:
        if self.recovery_database_path is not None:
            return self.recovery_database_path
        return self.database_path.with_suffix(".recovery.sqlite3")

    def validate(self) -> None:
        """检查设备绑定和运行参数。"""
        # 检查机器、相机和频率来源的唯一性。
        if not self.machines:
            raise ValueError("至少配置一台机器。")
        for attribute in ("machine_id", "camera_id", "frequency_source_id"):
            identifiers = [getattr(machine, attribute) for machine in self.machines]
            if not all(identifiers) or len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{attribute} 必须非空且不能重复。")

        # 检查已填写的真实相机序列号是否重复。
        serials = [machine.camera_serial for machine in self.machines if machine.camera_serial]
        if len(serials) != len(set(serials)):
            raise ValueError("已配置的 camera_serial 不能重复。")

        # 检查等待期限、采集间隔和队列容量。
        positive_parameters = (
            "capture_window_ms", "camera_timeout_ms",
            "frequency_interval_ms",
            "ocr_result_timeout_ms", "max_cycle_open_ms", "event_queue_capacity",
            "storage_queue_capacity",
            "max_pending_sessions_per_machine",
            "shutdown_timeout_ms",
            "maintenance_interval_ms",
            "max_persistent_records",
            "event_max_age_ms",
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
    """读取配置文件并解析相对路径。"""
    # 读取 JSON 配置并以配置文件所在目录解析路径。
    configuration_path = configuration_path.resolve()
    configuration_directory = configuration_path.parent
    with configuration_path.open(encoding="utf-8") as configuration_file:
        settings = json.load(configuration_file)

    # 创建各机器的相机绑定及 OCR、频率配置。
    machines = []
    for machine_settings in settings.pop("machines"):
        machine_settings["simulated_frequencies_hz"] = tuple(
            machine_settings.get("simulated_frequencies_hz", ())
        )
        machines.append(MachineConfiguration(**machine_settings))

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
    configuration = MeasurementConfiguration(machines=tuple(machines), **settings)
    configuration.validate()
    return configuration
