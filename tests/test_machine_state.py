"""验证机器状态配置的枚举转换和字符串序列化。"""

import json
from pathlib import Path

import pytest

from config_util import load_config, read_configuration_settings
from configuration_support import create_machine_database, write_configuration_files
from enums import MachineState
from database import serialize_value


@pytest.mark.parametrize("configured_state", [None, "CLOSED", "OPEN", "UNKNOWN"])
def test_load_machine_state_preserves_json_values(tmp_path: Path, configured_state: str | None) -> None:
    """验证配置读取为枚举，序列化仍输出原有字符串。

    Args:
        tmp_path: 测试临时目录。
        configured_state: 配置状态字符串，None 表示省略配置键。

    Returns:
        None: 完成断言，无返回数据。
        返回示例：
            None  # 无返回数据
    """
    # 使用示例配置准备不同的机器初始状态。
    example_directory = Path(__file__).resolve().parents[1] / "config"
    settings = read_configuration_settings(example_directory)
    settings["database_path"] = "measurements.sqlite3"
    if configured_state is None:
        settings.pop("initial_machine_state")
    else:
        settings["initial_machine_state"] = configured_state

    # 保存配置并通过正式入口读取。
    configuration_directory = tmp_path
    write_configuration_files(configuration_directory, settings)
    # 建好业务库并写入本测试所需机器，运行配置只读取公共参数。
    database_path = tmp_path / settings["database_path"]
    create_machine_database(database_path, [{
        "machine_name": "测试机器",
        "camera_serial": "CAM001",
        "frequency_meter_serial": "FREQ001",
    }])
    config = load_config(configuration_directory)

    # 检查枚举类型和持久化后的字符串内容。
    expected_state = configured_state or "CLOSED"
    assert config.initial_machine_state is MachineState(expected_state)
    payload = json.loads(json.dumps(serialize_value(config)))
    assert payload["initial_machine_state"] == expected_state


def test_load_config_rejects_invalid_machine_state(tmp_path: Path) -> None:
    """验证未知的机器状态字符串无法通过配置读取。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        None: 完成断言，无返回数据。
        返回示例：
            None  # 无返回数据
    """
    # 创建包含非法状态的配置文件。
    example_directory = Path(__file__).resolve().parents[1] / "config"
    settings = read_configuration_settings(example_directory)
    settings["database_path"] = "measurements.sqlite3"
    settings["initial_machine_state"] = "INVALID"
    configuration_directory = tmp_path
    write_configuration_files(configuration_directory, settings)

    # 确认配置入口拒绝非法状态。
    with pytest.raises(ValueError):
        load_config(configuration_directory)
