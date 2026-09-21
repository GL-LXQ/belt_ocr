"""生成测试使用的分组 YAML 配置。"""

import json
from pathlib import Path

from database import serialize_value

import yaml


def write_configuration_files(configuration_directory: Path, settings: dict) -> None:
    """按项目示例的字段分组，将给定参数写入五个测试配置文件。

    Args:
        configuration_directory: 测试配置输出目录。
        settings: 待写入的公共参数；机器测试数据由调用方单独保存。

    Returns:
        None  # 五个 YAML 配置文件已写入指定目录
    """
    # 将路径和枚举转换为 YAML 可直接写入的基础值。
    settings = json.loads(json.dumps(serialize_value(settings)))

    # 创建测试配置目录，读取项目示例中的字段分组。
    configuration_directory.mkdir(parents=True, exist_ok=True)
    example_directory = Path(__file__).resolve().parents[1] / "config"
    for example_path in example_directory.glob("*.yaml"):
        example_settings = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        # 仅写入本次测试提供的参数，省略项使用运行配置默认值。
        file_settings = {
            name: settings[name]
            for name in example_settings
            if name in settings
        }
        output_path = configuration_directory / example_path.name
        output_path.write_text(yaml.safe_dump(file_settings, allow_unicode=True), encoding="utf-8")
