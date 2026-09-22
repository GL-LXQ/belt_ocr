"""读取 MVS 相机和测量业务配置。"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from enums import MachineState


@dataclass(frozen=True)
class MachineConfig:
    """一台机器的身份与采集参数。"""

    machine_id: str  # 业务库机器表自增编号的字符串形式
    camera_serial: str  # 绑定的相机序列号
    frequency_meter_serial: str  # 绑定的频率仪序列号
    simulated_frequencies_hz: tuple[float, ...] = ()  # 联调用的模拟频率读数
    camera_pixel_format: str | None = None  # 相机像素格式，未配置时由相机决定
    camera_exposure_time_us: float | None = None  # 相机曝光时间，单位微秒
    camera_gain: float | None = None  # 相机增益


@dataclass(frozen=True)
class AppConfig:
    """一次运行的公共参数与路径配置。"""

    database_path: Path  # 业务库文件路径
    evidence_directory: Path  # 证据图片根目录
    mvs_development_directory: Path  # MVS SDK 开发目录
    capture_window_ms: int = 1000  # 单轮采集窗口，毫秒
    camera_timeout_ms: int = 50  # 单次取帧超时，毫秒
    frequency_interval_ms: int = 100  # 频率读取间隔配置
    minimum_frequency_hz: float = 0.01  # 有效频率下限
    maximum_frequency_hz: float = 10000.0  # 有效频率上限
    ocr_result_timeout_ms: int = 30000  # 整轮识别期限，毫秒
    max_cycle_open_ms: int = 60000  # 周期关闭期限，毫秒
    event_queue_capacity: int = 128  # 单机事件队列容量
    storage_queue_capacity: int = 32  # 共享存储队列容量
    shutdown_timeout_ms: int = 10000  # 退出收尾期限，毫秒
    recovery_database_path: Path | None = None  # 运行库路径覆盖值
    initial_machine_state: MachineState = MachineState.CLOSED  # 启动时的现场状态
    mvs_dll_directory: Path | None = None  # SDK 动态库搜索目录
    modbus_serial_port: str | None = None  # Modbus RTU 串口名称
    modbus_baudrate: int = 9600  # Modbus RTU 波特率
    modbus_parity: str = "N"  # Modbus RTU 校验方式
    modbus_stopbits: int = 1  # Modbus RTU 停止位数量
    modbus_bytesize: int = 8  # Modbus RTU 数据位数量
    modbus_unit_id: int = 1  # Modbus 设备地址
    modbus_timeout_seconds: float = 3.0  # Modbus 请求超时时间
    modbus_input_address: int = 0  # DI 起始地址
    modbus_poll_interval_ms: int = 50  # DI 轮询间隔
    modbus_reconnect_interval_ms: int = 1000  # DI 重连等待间隔
    io_machine_channels: dict[str, int] = field(default_factory=dict)  # 机器编号到 DI 索引的映射

    @property
    def recovery_path(self) -> Path:
        """取得显式配置或按业务库名称生成的运行库路径。

        Args:
            无外部参数。

        Returns:
            返回示例：
                Path("runtime/measurements.recovery.sqlite3")  # 运行库路径
        """
        # 有显式配置时直接使用配置路径。
        if self.recovery_database_path is not None:
            return self.recovery_database_path

        # 未配置时按业务库文件名追加 recovery 后缀。
        return self.database_path.with_suffix(".recovery.sqlite3")

    def validate(self) -> None:
        """检查运行参数。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 配置符合运行要求，非法配置抛出 ValueError
        """
        # 列出必须为正数的期限、间隔和容量参数名。
        positive_parameters = (
            "capture_window_ms", "camera_timeout_ms",
            "frequency_interval_ms",
            "ocr_result_timeout_ms", "max_cycle_open_ms", "event_queue_capacity",
            "storage_queue_capacity",
            "shutdown_timeout_ms",
        )

        # 逐项检查上述参数大于零。
        for parameter in positive_parameters:
            if getattr(self, parameter) <= 0:
                raise ValueError(f"{parameter} 必须大于零。")

        # 检查频率范围是递增的正数区间。
        if not 0 < self.minimum_frequency_hz < self.maximum_frequency_hz:
            raise ValueError("频率范围必须是递增的正数范围。")

        # 检查运行库与业务库不是同一个文件。
        if self.recovery_path.resolve() == self.database_path.resolve():
            raise ValueError("恢复库与最终结果库必须使用不同文件。")

        if self.modbus_poll_interval_ms <= 0:
            raise ValueError("modbus_poll_interval_ms 必须大于零。")
        if self.modbus_reconnect_interval_ms <= 0:
            raise ValueError("modbus_reconnect_interval_ms 必须大于零。")
        if self.modbus_timeout_seconds <= 0:
            raise ValueError("modbus_timeout_seconds 必须大于零。")
        if self.modbus_baudrate <= 0:
            raise ValueError("modbus_baudrate 必须大于零。")
        if self.modbus_input_address < 0:
            raise ValueError("modbus_input_address 不能小于零。")


def read_configuration_settings(configuration_directory: Path) -> dict:
    """读取单一配置文件的六个业务段落并转换路径和初始机器状态。

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
                "modbus_serial_port": None,  # Modbus RTU 串口名称
                "modbus_baudrate": 9600,  # Modbus RTU 波特率
                "modbus_parity": "N",  # Modbus RTU 校验方式
                "modbus_stopbits": 1,  # Modbus RTU 停止位数量
                "modbus_bytesize": 8,  # Modbus RTU 数据位数量
                "modbus_unit_id": 1,  # Modbus 设备地址
                "modbus_timeout_seconds": 3.0,  # Modbus 请求超时时间
                "modbus_input_address": 0,  # DI 起始地址
                "modbus_poll_interval_ms": 50,  # DI 轮询间隔
                "modbus_reconnect_interval_ms": 1000,  # DI 重连等待间隔
                "io_machine_channels": {},  # 机器编号到 DI 索引的映射
            }
    """
    # 解析配置目录并读取单一 YAML 文件。
    configuration_directory = configuration_directory.resolve()
    with (configuration_directory / "config.yaml").open(encoding="utf-8") as config_file:
        section_settings = yaml.safe_load(config_file)

    # 按固定段落顺序合并公共参数，并记录每个键的来源段落。
    settings = {}
    source_sections: dict[str, str] = {}
    for section_name in ("application", "camera", "ocr", "frequency", "machine", "io"):
        for name, value in (section_settings.get(section_name) or {}).items():
            # 同名键跨段落重复时抛出异常并报出两个来源段。
            if name in source_sections:
                raise ValueError(f"配置项 {name} 同时出现在配置段 {source_sections[name]} 和 {section_name}。")
            source_sections[name] = section_name
            settings[name] = value

    # 将必填路径转换为相对配置目录的绝对路径。
    for path_name in ("database_path", "evidence_directory", "mvs_development_directory"):
        settings[path_name] = (configuration_directory / settings[path_name]).resolve()

    # 转换已配置的可选路径。
    for path_name in ("recovery_database_path", "mvs_dll_directory"):
        if settings.get(path_name):
            settings[path_name] = (configuration_directory / settings[path_name]).resolve()

    # 转换初始机器状态，未配置时使用已关闭。
    settings["initial_machine_state"] = MachineState(settings.get("initial_machine_state", MachineState.CLOSED))

    # 返回合并后的扁平参数字典。
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
                modbus_serial_port=None,  # Modbus RTU 串口名称
                modbus_baudrate=9600,  # Modbus RTU 波特率
                modbus_parity="N",  # Modbus RTU 校验方式
                modbus_stopbits=1,  # Modbus RTU 停止位数量
                modbus_bytesize=8,  # Modbus RTU 数据位数量
                modbus_unit_id=1,  # Modbus 设备地址
                modbus_timeout_seconds=3.0,  # Modbus 请求超时时间
                modbus_input_address=0,  # DI 起始地址
                modbus_poll_interval_ms=50,  # DI 轮询间隔
                modbus_reconnect_interval_ms=1000,  # DI 重连等待间隔
                io_machine_channels={},  # 机器编号到 DI 索引的映射
            )  # 路径转换为绝对路径，公共参数按实际配置返回
    """
    # 读取 YAML 公共参数并组装运行配置。
    config = AppConfig(**read_configuration_settings(configuration_directory))

    # 校验运行参数后返回配置对象。
    config.validate()
    return config
