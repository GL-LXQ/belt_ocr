"""生成测试使用的分组 YAML 配置。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from database import serialize_value
from repo.machine_repo import MachineRepo

import yaml


def write_configuration_files(configuration_directory: Path, settings: dict) -> None:
    """按项目示例的字段分组，将给定参数拆段写入单一测试配置文件。

    Args:
        configuration_directory: 测试配置输出目录。
        settings: 待写入的公共参数；机器测试数据由调用方单独保存。

    Returns:
        None  # config.yaml 已写入指定目录
    """
    # 将路径和枚举转换为 YAML 可直接写入的基础值。
    settings = json.loads(json.dumps(serialize_value(settings)))

    # 创建测试配置目录，读取项目示例中的段落结构。
    configuration_directory.mkdir(parents=True, exist_ok=True)
    example_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    example_sections = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    # 仅写入本次测试提供的参数，省略项使用运行配置默认值。
    output_sections = {
        section_name: {name: settings[name] for name in section if name in settings}
        for section_name, section in example_sections.items()
    }
    output_sections = {name: section for name, section in output_sections.items() if section}
    output_path = configuration_directory / "config.yaml"
    output_path.write_text(yaml.safe_dump(output_sections, allow_unicode=True), encoding="utf-8")


def create_machine_database(database_path: Path, machines: list[dict]) -> list[int]:
    """创建业务库机器表并写入测试机器记录。

    Args:
        database_path: 测试业务库文件路径。
        machines: 逐台机器的字段，支持 machine_name、camera_serial、frequency_meter_serial、enabled 和 remark。

    Returns:
        返回示例：
            [1, 2]  # 新增机器的自增编号，顺序与传入的机器列表一致
    """
    # 创建业务库目录并建好机器表。
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)

    # 按传入顺序写入机器并返回自增编号。
    machine_repo = MachineRepo(database_path)
    return [
        machine_repo.insert(
            machine["machine_name"],
            machine["camera_serial"],
            machine["frequency_meter_serial"],
            machine.get("enabled", True),
            machine.get("remark"),
        )
        for machine in machines
    ]
