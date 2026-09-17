"""读取模拟采集和业务流程配置。"""

import json
from dataclasses import dataclass
from pathlib import Path

from enums import MachineState


@dataclass(frozen=True)
class MachineConfiguration:
    machine_id: str
    camera_id: str
    frequency_source_id: str
    image_directory: Path
    simulated_lines: tuple[str, ...]
    simulated_frequencies_hz: tuple[float, ...]


@dataclass(frozen=True)
class MeasurementConfiguration:
    machines: tuple[MachineConfiguration, ...]
    database_path: Path
    evidence_directory: Path
    capture_window_ms: int = 1000
    frame_interval_ms: int = 100
    max_frames_per_session: int = 5
    simulated_ocr_delay_ms: int = 500
    frequency_interval_ms: int = 100
    frequency_delivery_delay_ms: int = 0
    frequency_drain_timeout_ms: int = 2000
    minimum_frequency_hz: float = 0.01
    maximum_frequency_hz: float = 10000.0
    ocr_result_timeout_ms: int = 30000
    max_cycle_open_ms: int = 60000
    event_queue_capacity: int = 128
    ocr_queue_capacity: int = 32
    storage_queue_capacity: int = 32
    max_pending_sessions_per_machine: int = 20
    storage_retry_attempts: int = 3
    storage_retry_delay_ms: int = 100
    shutdown_timeout_ms: int = 10000
    configuration_version: str = "simulation-v1"
    recovery_database_path: Path | None = None
    maintenance_interval_ms: int = 250
    storage_retry_interval_ms: int = 1000
    max_persistent_records: int = 1000
    minimum_free_disk_bytes: int = 104857600
    ocr_retry_attempts: int = 3
    ocr_job_timeout_ms: int = 5000
    worker_restart_attempts: int = 3
    event_max_age_ms: int = 30000
    initial_machine_state: MachineState = MachineState.CLOSED

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

        # 检查等待期限、采集间隔和队列容量。
        positive_parameters = (
            "capture_window_ms", "frame_interval_ms", "max_frames_per_session",
            "frequency_interval_ms", "frequency_drain_timeout_ms",
            "ocr_result_timeout_ms", "max_cycle_open_ms", "event_queue_capacity",
            "ocr_queue_capacity", "storage_queue_capacity",
            "max_pending_sessions_per_machine", "storage_retry_attempts",
            "shutdown_timeout_ms",
            "maintenance_interval_ms", "storage_retry_interval_ms",
            "max_persistent_records", "ocr_retry_attempts", "ocr_job_timeout_ms",
            "worker_restart_attempts", "event_max_age_ms",
        )
        for parameter in positive_parameters:
            if getattr(self, parameter) <= 0:
                raise ValueError(f"{parameter} 必须大于零。")
        for parameter in (
            "simulated_ocr_delay_ms", "frequency_delivery_delay_ms",
            "storage_retry_delay_ms",
        ):
            if getattr(self, parameter) < 0:
                raise ValueError(f"{parameter} 不能为负数。")
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

    # 创建每台机器的独立模拟输入配置。
    machines = []
    for machine_settings in settings.pop("machines"):
        machine_settings["image_directory"] = (
            configuration_directory / machine_settings["image_directory"]
        ).resolve()
        machine_settings["simulated_lines"] = tuple(machine_settings["simulated_lines"])
        machine_settings["simulated_frequencies_hz"] = tuple(
            machine_settings["simulated_frequencies_hz"]
        )
        machines.append(MachineConfiguration(**machine_settings))

    # 将配置中的机器初始状态转换为枚举。
    settings["initial_machine_state"] = MachineState(settings.get("initial_machine_state", MachineState.CLOSED))

    # 创建输出路径并校验完整配置。
    for path_name in ("database_path", "evidence_directory"):
        settings[path_name] = (configuration_directory / settings[path_name]).resolve()
    if settings.get("recovery_database_path"):
        settings["recovery_database_path"] = (
            configuration_directory / settings["recovery_database_path"]
        ).resolve()
    configuration = MeasurementConfiguration(machines=tuple(machines), **settings)
    configuration.validate()
    return configuration
