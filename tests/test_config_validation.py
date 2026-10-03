"""验证运行配置在启动前拒绝非有限数值和无效队列容量。"""

from dataclasses import replace
from pathlib import Path

import pytest

from config_util import AppConfig, load_config


@pytest.mark.parametrize(
    ("section", "parameter", "yaml_value"),
    [
        ("camera", "capture_window_ms", ".nan"),
        ("camera", "camera_timeout_ms", ".inf"),
        ("frequency", "frequency_interval_ms", ".nan"),
        ("ocr", "ocr_lock_wait_timeout_ms", ".inf"),
        ("ocr", "ocr_result_timeout_ms", ".nan"),
        ("machine", "max_cycle_open_ms", ".inf"),
        ("machine", "event_queue_capacity", ".nan"),
        ("machine", "event_queue_capacity", ".inf"),
        ("application", "shutdown_timeout_ms", ".nan"),
        ("frequency", "minimum_frequency_hz", ".nan"),
        ("frequency", "maximum_frequency_hz", ".inf"),
        ("camera", "camera_exposure_time_us", ".nan"),
        ("camera", "camera_gain", ".inf"),
        ("io", "modbus_poll_interval_ms", ".nan"),
        ("io", "modbus_reconnect_interval_ms", ".inf"),
        ("io", "modbus_timeout_seconds", ".nan"),
        ("io", "modbus_timeout_seconds", ".inf"),
        ("io", "modbus_timeout_seconds", "-.inf"),
        ("io", "modbus_baudrate", ".nan"),
        ("io", "modbus_input_address", ".nan"),
        ("io", "modbus_stopbits", ".inf"),
        ("io", "modbus_bytesize", ".nan"),
        ("io", "modbus_unit_id", ".inf"),
    ],
)
def test_configuration_rejects_nonfinite_yaml_values(
    tmp_path: Path,
    section: str,
    parameter: str,
    yaml_value: str,
) -> None:
    """确认 YAML 非有限数值在加载时被拒绝并指出配置字段。

    Args:
        tmp_path: pytest 提供的临时目录。
        section: 配置所属业务段落。
        parameter: 被校验的数值配置字段。
        yaml_value: YAML 支持的非有限数值文本。

    Returns:
        返回示例：
            None  # 加载配置抛出包含字段名称的 ValueError
    """
    # 写入必填路径和需要校验的 YAML 数值。
    configuration_text = (
        "application:\n  database_path: data.sqlite3\n  evidence_directory: evidence\n"
        "camera:\n  mvs_development_directory: sdk\n"
        "ocr: {}\nfrequency: {}\nmachine: {}\nio: {}\n"
    )
    if f"{section}: {{}}" in configuration_text:
        configuration_text = configuration_text.replace(f"{section}: {{}}", f"{section}:\n  {parameter}: {yaml_value}")
    else:
        configuration_text = configuration_text.replace(f"{section}:\n", f"{section}:\n  {parameter}: {yaml_value}\n")
    (tmp_path / "config.yaml").write_text(configuration_text, encoding="utf-8")

    # 校验发生在运行对象创建之前。
    with pytest.raises(ValueError, match=parameter):
        load_config(tmp_path)


@pytest.mark.parametrize("capacity", [0, -1, 1.5, 128.0, True, "128"])
def test_configuration_requires_positive_integer_queue_capacity(tmp_path: Path, capacity: object) -> None:
    """确认队列容量只接受正整数。

    Args:
        tmp_path: pytest 提供的临时目录。
        capacity: 无效的队列容量。

    Returns:
        返回示例：
            None  # 无效容量被拒绝，不进入 asyncio.Queue
    """
    configuration = AppConfig(
        database_path=tmp_path / "data.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        event_queue_capacity=capacity,
    )
    with pytest.raises(ValueError, match="event_queue_capacity"):
        configuration.validate()


@pytest.mark.parametrize("value", ["100", True, None])
def test_configuration_rejects_non_numeric_timeout(tmp_path: Path, value: object) -> None:
    """确认超时配置不接受字符串、布尔值或空值。

    Args:
        tmp_path: pytest 提供的临时目录。
        value: 无效的超时配置值。

    Returns:
        返回示例：
            None  # 无效数值类型抛出包含字段名称的 ValueError
    """
    configuration = AppConfig(
        database_path=tmp_path / "data.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        ocr_result_timeout_ms=value,
    )
    with pytest.raises(ValueError, match="ocr_result_timeout_ms"):
        configuration.validate()


def test_configuration_keeps_valid_numeric_values(tmp_path: Path) -> None:
    """确认有效整数、小数及允许的空值和零值保持原样。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 有效配置通过校验且数值不被转换
    """
    # 保留数值型超时的小数形式，以及增益和输入地址的零值。
    configuration = AppConfig(
        database_path=tmp_path / "data.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        capture_window_ms=1000.0,
        camera_exposure_time_us=80,
        camera_gain=0,
        minimum_frequency_hz=0.01,
        maximum_frequency_hz=10000,
        modbus_timeout_seconds=0.5,
        modbus_input_address=0,
        event_queue_capacity=1,
    )
    configuration.validate()
    assert configuration.capture_window_ms == 1000.0
    assert isinstance(configuration.capture_window_ms, float)
    assert configuration.event_queue_capacity == 1
    assert configuration.camera_gain == 0
    assert configuration.modbus_timeout_seconds == 0.5

    # 未指定的可选相机数值仍由设备决定。
    replace(configuration, camera_exposure_time_us=None, camera_gain=None).validate()


@pytest.mark.parametrize("capacity", [1, 2, 3])
def test_max_inflight_cycles_default_and_yaml_loading(tmp_path: Path, capacity: int):
    """确认每机周期容量默认二且可从 machine 配置读取正整数。

    Args:
        tmp_path: 临时配置目录。
        capacity: 合法的周期容量。

    Returns:
        返回示例：
            None  # 默认值、读取和配置校验均正确
    """
    # 写入本轮临时配置，保留未指定容量时的默认值。
    configuration = AppConfig(
        database_path=tmp_path / "data.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    assert configuration.max_inflight_cycles == 2
    configuration_text = (
        "application:\n  database_path: data.sqlite3\n  evidence_directory: evidence\n"
        "camera:\n  mvs_development_directory: sdk\n"
        f"machine:\n  max_inflight_cycles: {capacity}\n"
    )
    (tmp_path / "config.yaml").write_text(configuration_text, encoding="utf-8")
    loaded = load_config(tmp_path)
    assert loaded.max_inflight_cycles == capacity
    loaded.validate()


@pytest.mark.parametrize("capacity", [True, False, 1.5, 2.0, 0, -1, None, "2"])
def test_max_inflight_cycles_rejects_invalid_capacity(tmp_path: Path, capacity):
    """确认周期容量拒绝布尔、小数和非正整数。

    Args:
        tmp_path: 临时配置目录。
        capacity: 非法容量配置。

    Returns:
        返回示例：
            None  # 非法容量均在创建运行时前被拒绝
    """
    configuration = AppConfig(
        database_path=tmp_path / "data.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        max_inflight_cycles=capacity,
    )
    with pytest.raises(ValueError, match="max_inflight_cycles"):
        configuration.validate()
