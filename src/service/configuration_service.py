"""读取系统配置草稿，校验并原子保存允许编辑的字段。"""

from copy import deepcopy
from dataclasses import fields
import logging
import os
from pathlib import Path
import tempfile

import yaml

from src.config_util import (
    AppConfig,
    ConfigurationValidationError,
    prepare_configuration_settings,
    validate_io_configuration,
)


logger = logging.getLogger(__name__)


# 保存范围与系统配置页面的可编辑字段一致。
EDITABLE_SECTIONS = {
    "camera_exposure_time_us": "camera",
    "camera_gain": "camera",
    "camera_line_selector": "camera",
    "camera_line_mode": "camera",
    "camera_line_source": "camera",
    "camera_strobe_enabled": "camera",
    "modbus_serial_port": "io",
    "modbus_baudrate": "io",
    "modbus_unit_id": "io",
    "minimum_frequency_hz": "frequency",
    "maximum_frequency_hz": "frequency",
    "ocr_lock_wait_timeout_ms": "ocr",
    "ocr_result_timeout_ms": "ocr",
    "max_cycle_open_ms": "machine",
    "io_machine_channels": "io",
}
INTEGER_FIELDS = {
    "modbus_baudrate", "modbus_unit_id", "ocr_lock_wait_timeout_ms", "ocr_result_timeout_ms", "max_cycle_open_ms",
}
TEXT_FIELDS = {"camera_line_selector", "camera_line_mode", "camera_line_source", "modbus_serial_port"}


class ConfigurationServiceError(Exception):
    """表示可以在配置页面展示的读取、校验或保存错误。"""

    def __init__(self, message: str, field: str | None = None) -> None:
        """记录错误说明和可选的表单字段。

        Args:
            message: 可直接展示的错误说明。
            field: 可定位的配置键或 DI 机器字段。

        Returns:
            返回示例：
                None  # 错误说明和字段已保存
        """
        super().__init__(message)
        self.field = field


class ConfigurationService:
    """持有原始 YAML 基线，只把有效的可编辑草稿写回配置。"""

    def __init__(self, configuration_directory: Path) -> None:
        """保存配置路径，等首次读取后建立基线。

        Args:
            configuration_directory: 包含 config.yaml 的配置目录。

        Returns:
            返回示例：
                None  # 尚未读取或修改配置文件
        """
        self.configuration_directory = configuration_directory.resolve()
        self.configuration_path = self.configuration_directory / "config.yaml"
        self.source_bytes: bytes | None = None
        self.document = {}
        self.settings = {}

    def read_configuration(self) -> dict:
        """读取原始配置，不用运行校验阻挡尚待修正的字段。

        Args:
            无外部参数。

        Returns:
            返回示例：
                {
                    "camera_gain": None,  # 原始相机增益
                    "database_path": Path("/config/data.sqlite3"),  # 只读展示路径
                }
        """
        # 完整读取文件后解析，失败时不改变上次成功读取的基线。
        try:
            source_bytes = self.configuration_path.read_bytes()
            document = yaml.safe_load(source_bytes.decode("utf-8"))
            settings = prepare_configuration_settings(document, self.configuration_directory)
        except FileNotFoundError as error:
            raise ConfigurationServiceError("未找到配置文件，请检查配置目录后重新读取。") from error
        except (OSError, UnicodeError) as error:
            raise ConfigurationServiceError(f"无法读取配置文件，请检查文件和访问权限。\n{error}") from error
        except (yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise ConfigurationServiceError(f"配置文件格式或字段结构异常，请检查后重新读取。\n{error}") from error

        # 分开保存嵌套原文与展示值，页面不能直接修改保存基线。
        self.source_bytes = source_bytes
        self.document = document
        self.settings = settings
        return deepcopy(settings)

    def prepare_configuration(self, draft: dict, machines: list[dict]) -> tuple[dict, dict]:
        """合并允许编辑的草稿，并复用运行配置和启用机器校验。

        Args:
            draft: 页面提交的配置草稿。
            machines: 机器服务返回的全部有效机器记录。

        Returns:
            返回示例：
                (
                    {  # 保留原段落和值的待写文档
                        "camera": {  # 相机段落
                            "camera_gain": None,  # 沿用相机增益
                        },
                    },
                    {  # 校验后的展示参数
                        "camera_gain": None,  # 沿用相机增益
                    },
                )
        """
        # 只有读取成功后才允许提交草稿。
        if self.source_bytes is None:
            raise ConfigurationServiceError("请先读取配置后再保存。")
        if not isinstance(draft, dict):
            raise ConfigurationServiceError("配置草稿必须是字段字典。")
        document = deepcopy(self.document)

        # 拒绝伪造的只读或未知字段修改，不将展示用绝对路径写回。
        for name, value in draft.items():
            if name in EDITABLE_SECTIONS:
                continue
            if (
                name not in self.settings
                or type(value) is not type(self.settings[name])
                or value != self.settings[name]
            ):
                raise ConfigurationServiceError(f"{name} 为只读配置，不能在本页修改。", name)

        # 逐项检查输入类型，整数参数不接受布尔值或小数。
        for name, section_name in EDITABLE_SECTIONS.items():
            if name not in draft or name == "io_machine_channels":
                continue
            value = draft[name]
            if name in INTEGER_FIELDS and type(value) is not int:
                raise ConfigurationServiceError(f"{name} 必须是整数。", name)
            if name in TEXT_FIELDS and value is not None and not isinstance(value, str):
                raise ConfigurationServiceError(f"{name} 必须是文本或 null。", name)
            if name == "camera_strobe_enabled" and value is not None and type(value) is not bool:
                raise ConfigurationServiceError("频闪输出必须是开启、关闭或沿用设备。", name)

            # 保留原字段所属段落，未配置的新字段才使用固定业务段落。
            target_section = next(
                (section for section in ("application", "camera", "ocr", "frequency", "machine", "io")
                 if name in (document.get(section) or {})),
                section_name,
            )
            document[target_section] = deepcopy(document.get(target_section) or {})
            document[target_section][name] = deepcopy(value)

        # 只允许调整已有绑定或为实际机器补绑，不接受虚构机器编号。
        if "io_machine_channels" in draft:
            self.merge_channel_mapping(document, draft["io_machine_channels"], machines)
        try:
            settings = prepare_configuration_settings(document, self.configuration_directory)
            known_fields = {field.name for field in fields(AppConfig)}
            unknown_fields = [name for name in settings if name not in known_fields]
            if unknown_fields:
                names = "、".join(str(name) for name in unknown_fields)
                raise ConfigurationServiceError(f"配置文件包含未识别字段：{names}。请检查配置文件。", str(unknown_fields[0]))
            configuration = AppConfig(**settings)
            configuration.validate()
            enabled_ids = {str(machine["id"]) for machine in machines if machine["enabled"]}
            validate_io_configuration(configuration, enabled_ids)
        except ConfigurationValidationError as error:
            raise ConfigurationServiceError(str(error), error.field) from error
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            message = str(error)
            field = next((field.name for field in fields(AppConfig) if field.name in message), None)
            if "频率范围" in message:
                field = "minimum_frequency_hz"
            raise ConfigurationServiceError(f"配置校验失败：{message}", field) from error
        return (
            document,
            settings,
        )

    def merge_channel_mapping(self, document: dict, mapping: object, machines: list[dict]) -> None:
        """保留原映射键，并合并实际机器的有效 DI 通道草稿。

        Args:
            document: 待保存的嵌套 YAML 副本。
            mapping: 页面提交的机器编号到 DI 通道字典。
            machines: 机器服务返回的有效机器记录。

        Returns:
            返回示例：
                None  # 有效通道写入副本，未修改配置文件
        """
        # 只接受映射结构，并为已有 YAML 数字键保留原类型。
        if not isinstance(mapping, dict):
            raise ConfigurationServiceError("机器与 DI 绑定必须是映射。", "io_machine_channels")
        section = next(
            (name for name in ("application", "camera", "ocr", "frequency", "machine", "io")
             if "io_machine_channels" in (document.get(name) or {})),
            "io",
        )
        original = (document.get(section) or {}).get("io_machine_channels") or {}
        original_keys = {str(machine_id): machine_id for machine_id in original}
        machine_ids = {str(machine["id"]) for machine in machines}

        # 保留已有绑定，新增绑定必须对应实际机器。
        updated = deepcopy(original)
        for machine_id, channel in mapping.items():
            field = f"io_machine_channels.{machine_id}"
            if not isinstance(machine_id, str) or machine_id not in original_keys.keys() | machine_ids:
                raise ConfigurationServiceError(f"DI 绑定中的机器 {machine_id} 不存在。", field)
            if type(channel) is not int or channel < 0:
                raise ConfigurationServiceError("DI 通道必须是大于等于零的整数。", field)
            updated[original_keys.get(machine_id, machine_id)] = channel

        # 非空映射写入校验后的整数，空映射保留原来的 null 或缺省状态。
        if updated:
            document[section] = deepcopy(document.get(section) or {})
            document[section]["io_machine_channels"] = updated

    def validate_configuration(self, draft: dict, machines: list[dict]) -> None:
        """检查草稿是否可保存，不读取设备或写入文件。

        Args:
            draft: 当前页面草稿。
            machines: 机器服务返回的有效机器记录。

        Returns:
            返回示例：
                None  # 草稿通过运行配置与 DI 校验
        """
        self.prepare_configuration(draft, machines)

    def save_configuration(self, draft: dict, machines: list[dict]) -> dict:
        """校验草稿并完成同目录原子替换，成功后才更新基线。

        Args:
            draft: 当前页面草稿。
            machines: 保存时重新读取的有效机器记录。

        Returns:
            返回示例：
                {
                    "camera_gain": 1.5,  # 已保存的新基线值
                    "database_path": Path("/config/data.sqlite3"),  # 只读展示路径
                }
        """
        # 先完成校验，保留未知段落、未展示字段和原始相对路径。
        document, settings = self.prepare_configuration(draft, machines)
        temporary_path = None
        try:
            if self.configuration_path.read_bytes() != self.source_bytes:
                raise ConfigurationServiceError("配置文件已被外部修改，草稿已保留；请重新读取并核对后再保存。")
            # 序列化后比较类型，整数修正不能被数值相等吞掉。
            content = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")
            baseline_content = yaml.safe_dump(self.document, allow_unicode=True, sort_keys=False).encode("utf-8")
            if content == baseline_content:
                return deepcopy(self.settings)

            # 在同目录写完临时文件并关闭句柄，兼容 Windows 替换。
            with tempfile.NamedTemporaryFile(
                dir=self.configuration_directory,
                prefix=".config-",
                suffix=".tmp",
                mode="wb",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            # 替换前再次检查外部修改，失败时原文件保持不变。
            if self.configuration_path.read_bytes() != self.source_bytes:
                raise ConfigurationServiceError("配置文件已被外部修改，草稿已保留；请重新读取并核对后再保存。")
            os.replace(temporary_path, self.configuration_path)
            temporary_path = None
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise ConfigurationServiceError(f"配置保存失败，草稿已保留。请检查磁盘空间和文件权限。\n{error}") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("临时配置文件清理失败：%s", temporary_path, exc_info=True)

        # 原子替换成功后发布新基线，不重新读取或热更新 Runtime。
        self.source_bytes = content
        self.document = document
        self.settings = settings
        return deepcopy(settings)
