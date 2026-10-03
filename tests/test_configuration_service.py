"""验证配置草稿校验、原子保存和临时机器记录的绑定规则。"""

import os
import sqlite3
import tempfile
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from config_util import load_config, validate_io_configuration
from src.repo.machine_repo import MachineRepo
from src.service.configuration_service import ConfigurationService, ConfigurationServiceError
from src.service.machine_service import MachineService


@pytest.fixture
def configuration_document() -> dict:
    """准备包含六个业务段落和扩展信息的配置文档。

    Args:
        无外部参数。

    Returns:
        返回示例：
            dict()  # 包含路径、运行参数、通道映射和扩展段落的测试文档
    """
    return {
        "application": {
            "database_path": "measurements.sqlite3",
            "recovery_database_path": None,
            "evidence_directory": "evidence",
            "shutdown_timeout_ms": 9750,
        },
        "camera": {
            "mvs_development_directory": "sdk",
            "mvs_dll_directory": None,
            "capture_window_ms": 900,
            "camera_timeout_ms": 40,
            "camera_pixel_format": "Mono8",
            "camera_exposure_time_us": None,
            "camera_gain": None,
            "camera_line_selector": None,
            "camera_line_mode": None,
            "camera_line_source": None,
            "camera_strobe_enabled": False,
        },
        "ocr": {
            "ocr_lock_wait_timeout_ms": 9000,
            "ocr_result_timeout_ms": 25000,
        },
        "frequency": {
            "frequency_interval_ms": 125,
            "minimum_frequency_hz": 0.1,
            "maximum_frequency_hz": 5000.0,
        },
        "machine": {
            "max_cycle_open_ms": 55000,
            "event_queue_capacity": 96,
        },
        "io": {
            "modbus_serial_port": "COM8",
            "modbus_baudrate": 115200,
            "modbus_parity": "E",
            "modbus_stopbits": 1,
            "modbus_bytesize": 8,
            "modbus_unit_id": 1,
            "modbus_timeout_seconds": 2.5,
            "modbus_input_address": 4,
            "modbus_poll_interval_ms": 75,
            "modbus_reconnect_interval_ms": 1250,
            "io_machine_channels": {
                1: 0,
                "99": 5,
            },
        },
        "site_extension": {
            "label": "保留现场扩展",
            "options": [None, False, 3],
        },
    }


@pytest.fixture
def configuration_path(tmp_path: Path, configuration_document: dict) -> Path:
    """将完整测试配置写入临时目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        configuration_document: 包含六个段落的测试文档。

    Returns:
        返回示例：
            Path("/tmp/test/config.yaml")  # 本次测试独占的配置文件
    """
    configuration_path = tmp_path / "config.yaml"
    configuration_path.write_text(yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8")
    return configuration_path


@pytest.fixture
def machine_service(tmp_path: Path) -> MachineService:
    """在临时业务库中创建一台启用机器和一台停用机器。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            MachineService(...)  # 仅读写临时机器表的真实业务服务
    """
    # 在临时数据库建立真实机器表。
    database_path = tmp_path / "measurements.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)

    # 使用业务入口分别创建启用和停用记录。
    service = MachineService(MachineRepo(database_path))
    service.create_machine("一号测试机", "CAM001", "METER001", enabled=True)
    service.create_machine("二号测试机", "CAM002", "METER002", enabled=False)
    return service


def test_read_returns_resolved_paths_without_changing_document(configuration_path: Path) -> None:
    """确认读取只返回界面数据，不修改路径文本、空值或磁盘字节。

    Args:
        configuration_path: 临时配置文件。

    Returns:
        返回示例：
            None  # 路径显示值已解析，原文和配置空值保持不变
    """
    original_bytes = configuration_path.read_bytes()
    service = ConfigurationService(configuration_path.parent)
    settings = service.read_configuration()

    # 必填路径按配置目录解析，可选空路径继续保持空值。
    assert Path(settings["database_path"]) == configuration_path.parent / "measurements.sqlite3"
    assert Path(settings["evidence_directory"]) == configuration_path.parent / "evidence"
    assert Path(settings["mvs_development_directory"]) == configuration_path.parent / "sdk"
    assert settings["recovery_database_path"] is None
    assert settings["mvs_dll_directory"] is None

    # 数字机器编号统一用于表单，未知顶层扩展不混入扁平参数。
    assert settings["io_machine_channels"] == {"1": 0, "99": 5}
    assert "site_extension" not in settings
    assert configuration_path.read_bytes() == original_bytes


def test_save_preserves_paths_nulls_hidden_fields_and_extensions(
    configuration_path: Path,
    configuration_document: dict,
    machine_service: MachineService,
) -> None:
    """确认保存只改可编辑字段，六个段落及未公开内容完整保留。

    Args:
        configuration_path: 临时配置文件。
        configuration_document: 保存前的嵌套测试文档。
        machine_service: 读取临时机器表的真实业务服务。

    Returns:
        返回示例：
            None  # 可编辑字段保存成功，路径与未编辑数据保持原值
    """
    # 修改多个段落的公开字段，并绑定真实停用机器。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_exposure_time_us"] = 88.5
    draft["camera_gain"] = 0.0
    draft["minimum_frequency_hz"] = 0.2
    draft["ocr_result_timeout_ms"] = 21000
    draft["max_cycle_open_ms"] = 52000
    draft["io_machine_channels"]["2"] = 3
    saved_settings = service.save_configuration(draft, machine_service.list_machines()["machines"])

    # 对完整文档比较，防止保存覆盖未公开的设置。
    expected_document = deepcopy(configuration_document)
    expected_document["camera"]["camera_exposure_time_us"] = 88.5
    expected_document["camera"]["camera_gain"] = 0.0
    expected_document["frequency"]["minimum_frequency_hz"] = 0.2
    expected_document["ocr"]["ocr_result_timeout_ms"] = 21000
    expected_document["machine"]["max_cycle_open_ms"] = 52000
    expected_document["io"]["io_machine_channels"] = {"1": 0, "2": 3, "99": 5}
    saved_document = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    saved_channels = saved_document["io"]["io_machine_channels"]
    saved_document["io"]["io_machine_channels"] = {
        str(machine_id): channel for machine_id, channel in saved_channels.items()
    }
    assert saved_document == expected_document

    # 保存后的返回值可重新加载，且不与提交草稿共享可变映射。
    assert saved_settings["camera_gain"] == 0.0
    assert saved_settings["io_machine_channels"] == {"1": 0, "2": 3, "99": 5}
    draft["io_machine_channels"]["1"] = 8
    assert saved_settings["io_machine_channels"]["1"] == 0
    assert load_config(configuration_path.parent).ocr_result_timeout_ms == 21000


def test_partial_document_uses_runtime_defaults_without_writing_them(tmp_path: Path) -> None:
    """确认部分配置沿用运行默认值，保存不会注入整套默认字段。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 仅新增已编辑字段，未显式配置的参数仍由 AppConfig 提供
    """
    # 只提供必填路径、串口和一个相机可选空值。
    document = {
        "application": {
            "database_path": "data.sqlite3",
            "evidence_directory": "images",
        },
        "camera": {
            "mvs_development_directory": "sdk",
            "camera_gain": None,
        },
        "ocr": {},
        "frequency": {},
        "machine": {},
        "io": {
            "modbus_serial_port": "COM7",
        },
    }
    configuration_path = tmp_path / "config.yaml"
    configuration_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert load_config(tmp_path).ocr_result_timeout_ms == 30000

    # 编辑一项后，完整 YAML 只多出该项的变化。
    service = ConfigurationService(tmp_path)
    draft = service.read_configuration()
    draft["camera_gain"] = 2.5
    service.save_configuration(draft, [])
    document["camera"]["camera_gain"] = 2.5
    assert yaml.safe_load(configuration_path.read_text(encoding="utf-8")) == document
    assert load_config(tmp_path).ocr_result_timeout_ms == 30000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_path", "forged.sqlite3"),
        ("recovery_database_path", "forged-recovery.sqlite3"),
        ("evidence_directory", "forged-evidence"),
        ("mvs_development_directory", "forged-sdk"),
        ("mvs_dll_directory", "forged-dll"),
        ("camera_pixel_format", "RGB8"),
        ("capture_window_ms", 1200),
        ("camera_timeout_ms", 60),
        ("frequency_interval_ms", 150),
        ("event_queue_capacity", 128),
        ("shutdown_timeout_ms", 10000),
        ("modbus_parity", "N"),
        ("modbus_stopbits", 2),
        ("modbus_bytesize", 7),
        ("modbus_timeout_seconds", 4.0),
        ("modbus_input_address", 0),
        ("modbus_poll_interval_ms", 50),
        ("modbus_reconnect_interval_ms", 1000),
        ("invented_parameter", 10),
    ],
)
def test_forged_readonly_and_unknown_fields_never_reach_disk(
    configuration_path: Path,
    machine_service: MachineService,
    field: str,
    value: object,
) -> None:
    """确认直接调用服务也不能修改只读字段或添加未知字段。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        field: 被伪造的字段名。
        value: 被伪造的字段值。

    Returns:
        返回示例：
            None  # 服务报告对应字段错误，原配置字节保持不变
    """
    # 在正常读取结果上伪造一个非公开字段。
    original_bytes = configuration_path.read_bytes()
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft[field] = value

    # 校验和保存两种入口都必须拒绝同一草稿。
    machines = machine_service.list_machines()["machines"]
    for operation in (service.validate_configuration, service.save_configuration):
        with pytest.raises(ConfigurationServiceError) as captured_error:
            operation(draft, machines)
        assert captured_error.value.field == field
    assert configuration_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "field",
    ["modbus_baudrate", "modbus_unit_id", "ocr_lock_wait_timeout_ms", "ocr_result_timeout_ms", "max_cycle_open_ms"],
)
@pytest.mark.parametrize("value", ["100", True, 100.0, 1.5])
def test_editable_integer_fields_reject_strings_booleans_and_floats(
    configuration_path: Path,
    machine_service: MachineService,
    field: str,
    value: object,
) -> None:
    """确认整数表单字段不接受数值字符串、布尔值或小数。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        field: 整数配置字段。
        value: 无效的整数替代值。

    Returns:
        返回示例：
            None  # 字段被明确拒绝，未覆盖磁盘内容
    """
    original_bytes = configuration_path.read_bytes()
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft[field] = value

    with pytest.raises(ConfigurationServiceError) as captured_error:
        service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert captured_error.value.field == field
    assert configuration_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("camera_exposure_time_us", 0),
        ("camera_exposure_time_us", float("inf")),
        ("camera_gain", -1),
        ("camera_gain", float("nan")),
        ("minimum_frequency_hz", 0),
        ("maximum_frequency_hz", 0.01),
        ("ocr_result_timeout_ms", 0),
        ("max_cycle_open_ms", -1),
        ("modbus_serial_port", "   "),
        ("camera_strobe_enabled", "true"),
    ],
)
def test_invalid_values_report_a_field_and_preserve_original_bytes(
    configuration_path: Path,
    machine_service: MachineService,
    field: str,
    value: object,
) -> None:
    """确认无效范围、非有限数值和错误类型都不会覆盖原文件。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        field: 待验证字段。
        value: 无效配置值。

    Returns:
        返回示例：
            None  # 错误包含定位字段，磁盘字节未变
    """
    original_bytes = configuration_path.read_bytes()
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft[field] = value

    with pytest.raises(ConfigurationServiceError) as captured_error:
        service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert captured_error.value.field in {field, "minimum_frequency_hz", "maximum_frequency_hz"}
    assert configuration_path.read_bytes() == original_bytes


@pytest.mark.parametrize("field", ["camera_line_selector", "camera_line_mode", "camera_line_source"])
def test_strobe_requires_each_output_line_field(
    configuration_path: Path,
    machine_service: MachineService,
    field: str,
) -> None:
    """确认启用频闪时逐项验证线路选择、模式和信号源。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        field: 被清空的线路字段。

    Returns:
        返回示例：
            None  # 缺失线路字段被准确标记
    """
    # 先准备完整频闪配置，再独立清空其中一项。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft.update({
        "camera_strobe_enabled": True,
        "camera_line_selector": "Line2",
        "camera_line_mode": "Strobe",
        "camera_line_source": "ExposureActive",
    })
    draft[field] = " "

    with pytest.raises(ConfigurationServiceError) as captured_error:
        service.validate_configuration(draft, machine_service.list_machines()["machines"])
    assert captured_error.value.field == field


@pytest.mark.parametrize("value", [-1, True, "0", 0.0, 1.5, None])
def test_channel_values_require_nonnegative_integers_for_disabled_rows_too(
    configuration_path: Path,
    machine_service: MachineService,
    value: object,
) -> None:
    """确认停用机器也不能保存无效 DI 值或空值占位。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        value: 停用机器的无效通道值。

    Returns:
        返回示例：
            None  # 通道类型错误定位到停用机器的具体行
    """
    original_bytes = configuration_path.read_bytes()
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["io_machine_channels"]["2"] = value

    with pytest.raises(ConfigurationServiceError) as captured_error:
        service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert captured_error.value.field == "io_machine_channels.2"
    assert configuration_path.read_bytes() == original_bytes


def test_real_machine_rows_do_not_receive_guessed_channels(
    configuration_path: Path,
    machine_service: MachineService,
) -> None:
    """确认新增机器未填通道时不写默认值，启用后必须明确绑定。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 停用空行保持未配置，启用缺失行拒绝保存
    """
    # 首次保存保留已有孤立编号，不为停用机器推测通道。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    saved = service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert saved["io_machine_channels"] == {"1": 0, "99": 5}
    saved_bytes = configuration_path.read_bytes()

    # 真实数据库启用该机器后，原草稿必须报告缺少绑定。
    machine_service.update_machine(2, "二号测试机", "CAM002", "METER002", enabled=True)
    with pytest.raises(ConfigurationServiceError) as captured_error:
        service.save_configuration(saved, machine_service.list_machines()["machines"])
    assert captured_error.value.field == "io_machine_channels.2"
    assert configuration_path.read_bytes() == saved_bytes


def test_enabled_channel_uniqueness_matches_shared_runtime_validation(
    configuration_path: Path,
    machine_service: MachineService,
) -> None:
    """确认停用机器可复用通道，启用后保存和运行使用相同冲突规则。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 保存与运行校验都拒绝启用机器的重复通道
    """
    # 停用机器复用通道不会阻止保存。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["io_machine_channels"]["2"] = 0
    service.save_configuration(draft, machine_service.list_machines()["machines"])
    configuration = load_config(configuration_path.parent)
    validate_io_configuration(configuration, {"1"})

    # 启用第二台机器后，两个入口都定位到相同冲突行。
    machine_service.update_machine(2, "二号测试机", "CAM002", "METER002", enabled=True)
    with pytest.raises(ConfigurationServiceError) as service_error:
        service.validate_configuration(draft, machine_service.list_machines()["machines"])
    with pytest.raises(ValueError) as runtime_error:
        validate_io_configuration(configuration, {"1", "2"})
    assert service_error.value.field == "io_machine_channels.2"
    assert runtime_error.value.field == "io_machine_channels.2"


def test_unknown_new_machine_id_is_rejected_but_existing_id_is_editable(
    configuration_path: Path,
    machine_service: MachineService,
) -> None:
    """确认映射只接受已有编号或机器表中的真实编号。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 已有孤立映射可保留修改，伪造新编号被拒绝
    """
    # 配置文件原本存在的编号即使不在机器表中，也允许保留和编辑。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["io_machine_channels"]["99"] = 6
    service.validate_configuration(draft, machine_service.list_machines()["machines"])

    # 不在原配置及数据库中的编号不能通过手工草稿新增。
    draft["io_machine_channels"]["100"] = 7
    with pytest.raises(ConfigurationServiceError) as captured_error:
        service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert captured_error.value.field == "io_machine_channels.100"


def test_readable_but_runtime_invalid_configuration_can_be_corrected(
    configuration_path: Path,
    configuration_document: dict,
    machine_service: MachineService,
) -> None:
    """确认运行无效的可解析配置仍能读取并在修正后保存。

    Args:
        configuration_path: 临时配置文件。
        configuration_document: 原始测试配置文档。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 读取未强制运行校验，修正草稿后可正常加载运行配置
    """
    # 模拟现场已启用频闪但尚未配置线路的文件。
    configuration_document["camera"]["camera_strobe_enabled"] = True
    configuration_path.write_text(yaml.safe_dump(configuration_document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(configuration_path.parent)

    # 读取保留错误值供用户修正，关闭频闪后保存成功。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    assert draft["camera_strobe_enabled"] is True
    draft["camera_strobe_enabled"] = False
    service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert load_config(configuration_path.parent).camera_strobe_enabled is False


@pytest.mark.parametrize("configuration_text", ["camera: [", "[]\n", "application: []\n", "camera: 4\n"])
def test_structurally_invalid_yaml_reports_service_error(tmp_path: Path, configuration_text: str) -> None:
    """确认损坏 YAML 或错误段落类型返回业务错误而非底层异常。

    Args:
        tmp_path: pytest 提供的临时目录。
        configuration_text: 无法正确解析为配置文档的文本。

    Returns:
        返回示例：
            None  # 文件内容保持不变，读取抛出 ConfigurationServiceError
    """
    configuration_path = tmp_path / "config.yaml"
    configuration_path.write_text(configuration_text, encoding="utf-8")
    with pytest.raises(ConfigurationServiceError):
        ConfigurationService(tmp_path).read_configuration()
    assert configuration_path.read_text(encoding="utf-8") == configuration_text


def test_unknown_fields_inside_known_sections_are_not_silently_discarded(
    configuration_path: Path,
    configuration_document: dict,
    machine_service: MachineService,
) -> None:
    """确认已知段落中的未知键保留 AppConfig 的拒绝语义。

    Args:
        configuration_path: 临时配置文件。
        configuration_document: 原始测试配置文档。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 未知内部键不能在保存中被忽略或删除
    """
    # 在 camera 段落加入运行配置构造器不接受的字段。
    configuration_document["camera"]["unsupported_camera_parameter"] = 123
    configuration_path.write_text(yaml.safe_dump(configuration_document), encoding="utf-8")
    original_bytes = configuration_path.read_bytes()
    with pytest.raises(TypeError):
        load_config(configuration_path.parent)

    # 允许读取供诊断，但保存必须拒绝整份无效运行配置。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 1.0
    with pytest.raises(ConfigurationServiceError):
        service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert configuration_path.read_bytes() == original_bytes


def test_external_edit_blocks_save_until_configuration_is_reloaded(
    configuration_path: Path,
    machine_service: MachineService,
) -> None:
    """确认外部修改不会被旧草稿覆盖，重新读取后才能继续保存。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 冲突期间保留外部原文，刷新基线后保存成功
    """
    # 即使外部只增加注释，也必须按原始字节识别冲突。
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 1.0
    external_bytes = configuration_path.read_bytes() + "\n# 外部更新\n".encode("utf-8")
    configuration_path.write_bytes(external_bytes)

    # 连续保存旧草稿不能自动覆盖外部内容或更新服务基线。
    machines = machine_service.list_machines()["machines"]
    for attempt in range(2):
        with pytest.raises(ConfigurationServiceError):
            service.save_configuration(draft, machines)
        assert configuration_path.read_bytes() == external_bytes, attempt

    # 明确重新读取后，新的保存建立在最新文件内容上。
    refreshed_draft = service.read_configuration()
    refreshed_draft["camera_gain"] = 1.0
    assert service.save_configuration(refreshed_draft, machines)["camera_gain"] == 1.0


def test_atomic_replace_uses_same_directory_and_success_updates_baseline(
    configuration_path: Path,
    machine_service: MachineService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认保存经同目录临时文件原子替换，并在成功后更新比较基线。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 两次连续保存成功，替换来源均为同目录完整 YAML
    """
    original_replace = os.replace
    original_temporary_factory = tempfile.NamedTemporaryFile
    replace_calls = []
    temporary_files = []

    def create_tracked_temporary_file(**arguments):
        """保存实际临时文件对象，供替换时核对句柄状态。

        Args:
            arguments: 服务提供的临时文件创建参数。

        Returns:
            返回示例：
                tempfile.NamedTemporaryFile(...)  # 原始工厂创建的临时文件对象
        """
        temporary_file = original_temporary_factory(**arguments)
        temporary_files.append(temporary_file)
        return temporary_file

    def replace_configuration(source: str | Path, destination: str | Path) -> None:
        """核对临时文件和完整内容后执行真实原子替换。

        Args:
            source: 已写好的临时 YAML 文件。
            destination: 原配置文件路径。

        Returns:
            返回示例：
                None  # 完整临时文件已替换目标文件
        """
        source_path = Path(source)
        assert source_path.parent == configuration_path.parent
        assert Path(destination) == configuration_path
        assert source_path != configuration_path
        assert isinstance(yaml.safe_load(source_path.read_text(encoding="utf-8")), dict)
        assert temporary_files and all(temporary_file.closed for temporary_file in temporary_files)
        replace_calls.append(source_path)
        original_replace(source, destination)

    # 捕获原子替换，不改变真实临时文件保存流程。
    monkeypatch.setattr("src.service.configuration_service.tempfile.NamedTemporaryFile", create_tracked_temporary_file)
    monkeypatch.setattr("src.service.configuration_service.os.replace", replace_configuration)
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 1.0
    first_saved = service.save_configuration(draft, machine_service.list_machines()["machines"])

    # 不额外读取磁盘，直接使用成功返回的基线再次保存。
    first_saved["camera_gain"] = 2.0
    second_saved = service.save_configuration(first_saved, machine_service.list_machines()["machines"])
    assert second_saved["camera_gain"] == 2.0
    assert len(replace_calls) == 2
    assert all(not temporary_path.exists() for temporary_path in replace_calls)


@pytest.mark.parametrize("failure_target", ["tempfile.NamedTemporaryFile", "os.replace", "os.fsync"])
def test_disk_failures_preserve_original_remove_temporary_file_and_allow_retry(
    configuration_path: Path,
    machine_service: MachineService,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    """确认替换或刷盘失败时原文不变，临时文件清理后仍可重试。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        monkeypatch: pytest 提供的属性替换工具。
        failure_target: 被注入磁盘异常的操作。

    Returns:
        返回示例：
            None  # 失败未更新基线且未遗留临时文件，移除故障后保存成功
    """
    original_bytes = configuration_path.read_bytes()
    original_paths = set(configuration_path.parent.iterdir())
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 3.0
    machines = machine_service.list_machines()["machines"]

    # 在实际保存阶段注入异常，并验证原文件及目录内容未改变。
    with monkeypatch.context() as patched:
        patched.setattr(f"src.service.configuration_service.{failure_target}", Mock(side_effect=OSError("磁盘故障")))
        with pytest.raises(ConfigurationServiceError):
            service.save_configuration(draft, machines)
    assert configuration_path.read_bytes() == original_bytes
    assert set(configuration_path.parent.iterdir()) == original_paths

    # 失败后仍使用旧基线，重试不需要丢弃用户草稿。
    saved_settings = service.save_configuration(draft, machines)
    assert saved_settings["camera_gain"] == 3.0


def test_failed_temporary_write_closes_file_and_preserves_baseline(
    configuration_path: Path,
    machine_service: MachineService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认临时文件写入失败也关闭句柄、删除临时文件并保留基线。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 写失败未改变原文或基线，也未遗留已打开文件
    """
    # 记录实际临时文件，再只替换其写入方法。
    original_factory = tempfile.NamedTemporaryFile
    temporary_files = []
    original_bytes = configuration_path.read_bytes()
    original_paths = set(configuration_path.parent.iterdir())

    def create_unwritable_temporary_file(**arguments):
        """创建真实临时文件并让写入抛出测试磁盘异常。

        Args:
            arguments: 服务提供的临时文件创建参数。

        Returns:
            返回示例：
                tempfile.NamedTemporaryFile(...)  # 写入失败但仍能正常关闭的临时文件
        """
        temporary_file = original_factory(**arguments)
        temporary_file.write = Mock(side_effect=OSError("临时文件写入失败"))
        temporary_files.append(temporary_file)
        return temporary_file

    monkeypatch.setattr(
        "src.service.configuration_service.tempfile.NamedTemporaryFile",
        create_unwritable_temporary_file,
    )
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 5.0
    with pytest.raises(ConfigurationServiceError, match="临时文件写入失败"):
        service.save_configuration(draft, machine_service.list_machines()["machines"])

    # 句柄已关闭，原文与基线均保持保存前的状态。
    assert temporary_files and all(temporary_file.closed for temporary_file in temporary_files)
    assert configuration_path.read_bytes() == original_bytes
    assert service.source_bytes == original_bytes
    assert service.settings["camera_gain"] is None
    assert set(configuration_path.parent.iterdir()) == original_paths


def test_cleanup_failure_does_not_replace_the_original_save_error(
    configuration_path: Path,
    machine_service: MachineService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """确认临时文件清理失败只记录日志，不覆盖主要保存异常。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        monkeypatch: pytest 提供的属性替换工具。
        caplog: pytest 提供的日志捕获工具。

    Returns:
        返回示例：
            None  # 替换错误继续展示，原文和基线未变，清理故障已记录
    """
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 6.0
    original_bytes = configuration_path.read_bytes()

    # 同时模拟原子替换失败和随后临时文件清理失败。
    with monkeypatch.context() as patched:
        patched.setattr("src.service.configuration_service.os.replace", Mock(side_effect=OSError("主要替换错误")))
        patched.setattr(Path, "unlink", Mock(side_effect=OSError("次要清理错误")))
        with pytest.raises(ConfigurationServiceError, match="主要替换错误") as captured_error:
            service.save_configuration(draft, machine_service.list_machines()["machines"])
    assert "次要清理错误" not in str(captured_error.value)
    assert "临时配置文件清理失败" in caplog.text
    assert configuration_path.read_bytes() == original_bytes
    assert service.source_bytes == original_bytes
    assert service.settings["camera_gain"] is None

    # 只清理本测试故意留下的临时文件。
    temporary_paths = list(configuration_path.parent.glob(".config-*.tmp"))
    assert len(temporary_paths) == 1
    for temporary_path in temporary_paths:
        temporary_path.unlink()


def test_editing_aliased_sections_does_not_change_unknown_extension_values(
    configuration_path: Path,
    configuration_document: dict,
    machine_service: MachineService,
) -> None:
    """确认 YAML 共享锚点不会让配置编辑连带修改扩展段落。

    Args:
        configuration_path: 临时配置文件。
        configuration_document: 原始测试配置文档。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 相机和 DI 配置已更新，扩展快照仍保留原值
    """
    # 让未知扩展分别共享完整 camera 段落和嵌套 DI 映射。
    configuration_document["vendor_camera_snapshot"] = configuration_document["camera"]
    configuration_document["vendor_channels_snapshot"] = configuration_document["io"]["io_machine_channels"]
    configuration_path.write_text(yaml.safe_dump(configuration_document), encoding="utf-8")
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 7.0
    draft["io_machine_channels"]["1"] = 8
    service.save_configuration(draft, machine_service.list_machines()["machines"])

    # 保存后的扩展值与已修改的业务段落独立。
    saved_document = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    assert saved_document["camera"]["camera_gain"] == 7.0
    assert saved_document["io"]["io_machine_channels"][1] == 8
    assert saved_document["vendor_camera_snapshot"] == configuration_document["vendor_camera_snapshot"]
    assert saved_document["vendor_channels_snapshot"] == configuration_document["vendor_channels_snapshot"]


def test_explicit_float_to_integer_corrections_are_written_despite_numeric_equality(
    configuration_path: Path,
    configuration_document: dict,
    machine_service: MachineService,
) -> None:
    """确认数值相同的小数转整数修正也会落盘并建立新基线。

    Args:
        configuration_path: 临时配置文件。
        configuration_document: 原始测试配置文档。
        machine_service: 临时机器业务服务。

    Returns:
        返回示例：
            None  # 超时和 DI 通道的类型修正不会被数值相等判断吞掉
    """
    # 准备两个数值合法但类型不符合保存要求的字段。
    configuration_document["ocr"]["ocr_result_timeout_ms"] = 1000.0
    configuration_document["io"]["io_machine_channels"][1] = 0.0
    configuration_path.write_text(yaml.safe_dump(configuration_document), encoding="utf-8")
    original_bytes = configuration_path.read_bytes()
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()

    # 明确修正整数类型，保持两个字段的数值不变。
    draft["ocr_result_timeout_ms"] = 1000
    draft["io_machine_channels"]["1"] = 0
    saved = service.save_configuration(draft, machine_service.list_machines()["machines"])
    saved_document = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    assert type(saved["ocr_result_timeout_ms"]) is int
    assert type(saved["io_machine_channels"]["1"]) is int
    assert type(saved_document["ocr"]["ocr_result_timeout_ms"]) is int
    assert type(saved_document["io"]["io_machine_channels"][1]) is int
    assert configuration_path.read_bytes() != original_bytes


def test_external_change_during_staging_is_checked_before_replace(
    configuration_path: Path,
    machine_service: MachineService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认临时文件写完后再次检查原文件，防止覆盖保存过程中的外部编辑。

    Args:
        configuration_path: 临时配置文件。
        machine_service: 临时机器业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 保存阶段的外部编辑被保留，临时文件已删除
    """
    service = ConfigurationService(configuration_path.parent)
    draft = service.read_configuration()
    draft["camera_gain"] = 4.0
    external_bytes = configuration_path.read_bytes() + "\n# 保存时发生的外部编辑\n".encode("utf-8")
    original_paths = set(configuration_path.parent.iterdir())
    original_fsync = os.fsync

    def edit_original_after_flush(file_descriptor: int) -> None:
        """完成临时文件刷盘后模拟外部程序修改原配置。

        Args:
            file_descriptor: 临时文件的已打开描述符。

        Returns:
            返回示例：
                None  # 临时文件已刷盘，原配置包含外部变更
        """
        original_fsync(file_descriptor)
        configuration_path.write_bytes(external_bytes)

    monkeypatch.setattr("src.service.configuration_service.os.fsync", edit_original_after_flush)
    replace_configuration = Mock()
    monkeypatch.setattr("src.service.configuration_service.os.replace", replace_configuration)
    with pytest.raises(ConfigurationServiceError):
        service.save_configuration(draft, machine_service.list_machines()["machines"])
    replace_configuration.assert_not_called()
    assert configuration_path.read_bytes() == external_bytes
    assert set(configuration_path.parent.iterdir()) == original_paths
