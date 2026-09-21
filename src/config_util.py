"""读取 MVS 相机和测量业务配置。"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from enums import MachineState


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
        """检查运行参数。

        Args:
            无。

        Returns:
            None  # 配置符合运行要求，非法配置抛出 ValueError
        """
        # 检查等待期限、采集间隔和队列容量。
        positive_parameters = (
            "capture_window_ms", "camera_timeout_ms",
            "frequency_interval_ms",
            "ocr_result_timeout_ms", "max_cycle_open_ms", "event_queue_capacity",
            "storage_queue_capacity",
            "shutdown_timeout_ms",
        )
        for parameter in positive_parameters:
            if getattr(self, parameter) <= 0:
                raise ValueError(f"{parameter} 必须大于零。")
        if not 0 < self.minimum_frequency_hz < self.maximum_frequency_hz:
            raise ValueError("频率范围必须是递增的正数范围。")
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


def load_config(configuration_directory: Path) -> AppConfig:
    """读取配置文件的公共参数，构建并校验运行配置。

    Args:
        configuration_directory: 包含 config.yaml 的配置目录。

    Returns:
        返回示例（可选参数按 YAML 实际内容返回）：
            AppConfig(
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
                initial_machine_state=MachineState.CLOSED,  # 初始现场状态配置
                mvs_dll_directory=None,  # SDK 动态库搜索目录
            )  # 路径转换为绝对路径，公共参数按实际配置返回
    """
    # 读取 YAML 公共参数并组装运行配置。
    config = AppConfig(**read_configuration_settings(configuration_directory))

    # 校验运行参数后返回配置对象。
    config.validate()
    return config
