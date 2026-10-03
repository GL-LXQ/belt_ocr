"""读取 MVS 相机和测量业务配置。"""

from dataclasses import dataclass, field
import math
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MachineConfig:
    """一台机器的身份与采集参数。"""

    machine_id: str  # 业务库机器表自增编号的字符串形式
    machine_name: str  # 业务库机器表中的机器名称
    camera_serial: str  # 绑定的相机序列号
    frequency_meter_serial: str  # 绑定的频率仪序列号
    camera_pixel_format: str | None = None  # 相机像素格式，未配置时由相机决定
    camera_exposure_time_us: float | None = None  # 相机曝光时间，单位微秒
    camera_gain: float | None = None  # 相机增益
    camera_line_selector: str | None = None  # 相机输出线路
    camera_line_mode: str | None = None  # 相机线路模式
    camera_line_source: str | None = None  # 相机线路信号源
    camera_strobe_enabled: bool | None = None  # 是否启用相机频闪输出


@dataclass(frozen=True)
class AppConfig:
    """一次运行的公共参数与路径配置。"""

    database_path: Path  # 业务库文件路径
    evidence_directory: Path  # 证据图片根目录
    mvs_development_directory: Path  # MVS SDK 开发目录
    capture_window_ms: int = 1000  # 单轮采集窗口，毫秒
    camera_timeout_ms: int = 50  # 单次取帧超时，毫秒
    camera_pixel_format: str | None = None  # 公共相机像素格式
    camera_exposure_time_us: float | None = None  # 公共相机曝光时间，微秒
    camera_gain: float | None = None  # 公共相机增益
    camera_line_selector: str | None = None  # 公共相机输出线路
    camera_line_mode: str | None = None  # 公共相机线路模式
    camera_line_source: str | None = None  # 公共相机线路信号源
    camera_strobe_enabled: bool | None = None  # 是否启用公共相机频闪输出
    frequency_interval_ms: int = 100  # 频率读取间隔配置
    minimum_frequency_hz: float = 0.01  # 有效频率下限
    maximum_frequency_hz: float = 10000.0  # 有效频率上限
    ocr_lock_wait_timeout_ms: int = 10000  # 等待共享 OCR 处理资源的期限，毫秒
    ocr_result_timeout_ms: int = 30000  # OCR 处理期限，毫秒
    max_cycle_open_ms: int = 60000  # 周期关闭期限，毫秒
    event_queue_capacity: int = 128  # 单机事件队列容量
    shutdown_timeout_ms: int = 10000  # 退出收尾期限，毫秒
    recovery_database_path: Path | None = None  # 运行库路径覆盖值
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
            "ocr_lock_wait_timeout_ms", "ocr_result_timeout_ms",
            "max_cycle_open_ms", "event_queue_capacity",
            "shutdown_timeout_ms",
        )

        # 数值配置只接受整数或有限小数，可选相机参数允许留空。
        numeric_parameters = positive_parameters + (
            "minimum_frequency_hz", "maximum_frequency_hz",
            "camera_exposure_time_us", "camera_gain",
            "modbus_poll_interval_ms", "modbus_reconnect_interval_ms",
            "modbus_timeout_seconds", "modbus_baudrate", "modbus_input_address",
            "modbus_stopbits", "modbus_bytesize", "modbus_unit_id",
        )
        for parameter in numeric_parameters:
            value = getattr(self, parameter)
            if parameter in ("camera_exposure_time_us", "camera_gain") and value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise ValueError(f"{parameter} 必须是有限数值。")

        # 队列容量必须使用整数，不能由小数隐式决定容量。
        if not isinstance(self.event_queue_capacity, int):
            raise ValueError("event_queue_capacity 必须是正整数。")

        # 逐项检查上述参数大于零。
        for parameter in positive_parameters:
            if getattr(self, parameter) <= 0:
                raise ValueError(f"{parameter} 必须大于零。")

        # 检查频率范围是递增的正数区间。
        if not 0 < self.minimum_frequency_hz < self.maximum_frequency_hz:
            raise ValueError("频率范围必须是递增的正数范围。")

        # 校验手动曝光时间和增益的数值范围。
        if (
            self.camera_exposure_time_us is not None
            and self.camera_exposure_time_us <= 0
        ):
            raise ValueError("camera_exposure_time_us 必须大于零。")
        if self.camera_gain is not None and self.camera_gain < 0:
            raise ValueError("camera_gain 不能小于零。")

        # 启用频闪时检查线路选择、模式和信号源是否已填写。
        if self.camera_strobe_enabled is True:
            for parameter_name in (
                "camera_line_selector", "camera_line_mode", "camera_line_source"
            ):
                parameter_value = getattr(self, parameter_name)
                if not isinstance(parameter_value, str) or not parameter_value.strip():
                    raise ValueError(f"启用相机频闪时必须配置 {parameter_name}。")

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
    """读取单一配置文件的六个业务段落，并转换路径和 DI 通道映射键。

    Args:
        configuration_directory: 包含 config.yaml 的配置目录。

    Returns:
        返回示例（可选参数按 YAML 实际内容返回）：
            {
                "database_path": Path("D:/belt_ocr/runtime/measurements.sqlite3"),  # 业务库绝对路径
                "recovery_database_path": Path("D:/belt_ocr/runtime/measurements.recovery.sqlite3"),  # 运行库绝对路径
                "evidence_directory": Path("D:/belt_ocr/runtime/evidence"),  # 图片绝对目录
                "shutdown_timeout_ms": 10000,  # 退出期限，毫秒
                "mvs_development_directory": Path("D:/app/HIK/MVS/Development"),  # SDK 目录
                "mvs_dll_directory": None,  # 使用默认动态库目录
                "capture_window_ms": 1000,  # 采集窗口，毫秒
                "camera_timeout_ms": 50,  # 取帧超时，毫秒
                "camera_pixel_format": "Mono8",  # 相机像素格式
                "camera_exposure_time_us": 80.0,  # 相机曝光时间，微秒
                "camera_gain": 0.0,  # 相机增益
                "camera_line_selector": None,  # 相机输出线路待现场填写
                "camera_line_mode": "Strobe",  # 相机线路模式
                "camera_line_source": None,  # 相机线路信号源待现场填写
                "camera_strobe_enabled": True,  # 相机频闪输出使能
                "ocr_lock_wait_timeout_ms": 10000,  # 等待共享 OCR 处理资源的期限，毫秒
                "ocr_result_timeout_ms": 30000,  # OCR 处理期限，毫秒
                "frequency_interval_ms": 100,  # 频率读取间隔配置
                "minimum_frequency_hz": 0.01,  # 最低有效频率
                "maximum_frequency_hz": 10000.0,  # 最高有效频率
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

    return prepare_configuration_settings(section_settings, configuration_directory)


def prepare_configuration_settings(section_settings: dict, configuration_directory: Path) -> dict:
    """从六段配置构造运行参数，路径只在返回值中解析。

    Args:
        section_settings: 原始 YAML 业务段落。
        configuration_directory: 相对路径的基准目录。

    Returns:
        返回示例：
            {
                "database_path": Path("/config/data.sqlite3"),  # 解析后的业务库路径
                "io_machine_channels": {  # 机器到通道的映射
                    "1": 0,  # 一号机器的通道
                },
            }
    """
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

    # 统一 DI 通道映射的机器编号为字符串，兼容 YAML 中的数字键。
    channel_items = (settings.get("io_machine_channels") or {}).items()
    settings["io_machine_channels"] = {str(machine_id): channel for machine_id, channel in channel_items}

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
                camera_pixel_format="Mono8",  # 相机像素格式
                camera_exposure_time_us=80.0,  # 相机曝光时间，微秒
                camera_gain=0.0,  # 相机增益
                camera_line_selector="现场线路值",  # 相机输出线路
                camera_line_mode="Strobe",  # 相机线路模式
                camera_line_source="现场信号源值",  # 相机线路信号源
                camera_strobe_enabled=True,  # 相机频闪输出使能
                frequency_interval_ms=100,  # 频率读取间隔配置
                minimum_frequency_hz=0.01,  # 最低有效频率
                maximum_frequency_hz=10000.0,  # 最高有效频率
                ocr_lock_wait_timeout_ms=10000,  # 等待共享 OCR 处理资源的期限
                ocr_result_timeout_ms=30000,  # OCR 处理期限
                max_cycle_open_ms=60000,  # 周期关闭期限
                event_queue_capacity=128,  # 单机事件队列容量
                shutdown_timeout_ms=10000,  # 退出收尾期限
                recovery_database_path=None,  # 运行库路径覆盖值
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


class ConfigurationValidationError(ValueError):
    """保存配置校验错误对应的表单字段。"""

    def __init__(self, message: str, field: str) -> None:
        """记录可读提示和需要修正的字段。

        Args:
            message: 可直接展示的错误说明。
            field: 配置键或 io_machine_channels.机器编号。

        Returns:
            返回示例：
                None  # 错误说明和字段已记录
        """
        super().__init__(message)
        self.field = field


def validate_io_configuration(config: AppConfig, enabled_machine_ids: set[str]) -> None:
    """校验启用机器的串口与 DI 绑定，供保存和启动共同使用。

    Args:
        config: 待校验的公共运行配置。
        enabled_machine_ids: 实际启用机器编号集合。

    Returns:
        返回示例：
            None  # 串口已填写，启用机器均绑定唯一的非负整数通道
    """
    # 拒绝未填写或空白的 IO 串口。
    if not isinstance(config.modbus_serial_port, str) or not config.modbus_serial_port.strip():
        raise ConfigurationValidationError("未配置 Modbus RTU 串口。", "modbus_serial_port")

    # 为第一台缺少绑定的启用机器返回可定位字段。
    channel_mapping = config.io_machine_channels
    missing_machine_ids = enabled_machine_ids - channel_mapping.keys()
    if missing_machine_ids:
        missing_ids = sorted(missing_machine_ids)
        raise ConfigurationValidationError(
            f"启用机器未配置 DI 通道：{', '.join(missing_ids)}。",
            f"io_machine_channels.{missing_ids[0]}",
        )

    # 逐台检查启用机器的通道为非负整数且不重复。
    used_channels = set()
    for machine_id in sorted(enabled_machine_ids):
        channel = channel_mapping[machine_id]
        field = f"io_machine_channels.{machine_id}"
        if type(channel) is not int or channel < 0:
            raise ConfigurationValidationError("DI 通道必须是大于等于零的整数。", field)
        if channel in used_channels:
            raise ConfigurationValidationError("启用机器不能绑定相同的 DI 通道。", field)
        used_channels.add(channel)
