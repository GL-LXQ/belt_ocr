"""验证分组配置的路径解析、默认参数和桌面启动。"""

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from app import App
from config_util import AppConfig, load_config, read_configuration_settings
from configuration_support import create_machine_database, write_configuration_files
from repo.machine_repo import MachineRepo
from test_ui_shell import application


def test_configuration_paths_and_defaults(tmp_path: Path, monkeypatch) -> None:
    """验证相对配置目录解析路径，并保留省略参数和逐机参数的默认值。

    Args:
        tmp_path: 临时目录。
        monkeypatch: 工作目录替换工具。

    Returns:
        None  # 路径、默认参数和机器绑定均符合预期
    """
    # 写入相对路径和一个绝对 SDK 路径，再切换到其他工作目录。
    configuration_directory = tmp_path / "config"
    settings = {
        "database_path": "../data/measurements.sqlite3",
        "evidence_directory": "../images",
        "mvs_development_directory": str(tmp_path / "sdk"),
        "mvs_dll_directory": "../dll",
    }
    write_configuration_files(configuration_directory, settings)
    monkeypatch.chdir(tmp_path.parent)

    # 公共读取只解析配置，不创建数据库或要求存在机器。
    public_settings = read_configuration_settings(configuration_directory)
    database_path = tmp_path / "data/measurements.sqlite3"
    assert public_settings["database_path"] == database_path
    assert public_settings["evidence_directory"] == tmp_path / "images"
    assert public_settings["mvs_development_directory"] == tmp_path / "sdk"
    assert public_settings["mvs_dll_directory"] == tmp_path / "dll"
    assert not database_path.exists()

    # 完整运行配置只读取公共参数，没有启用机器时由应用启动入口拒绝。
    config = load_config(configuration_directory)
    with pytest.raises(ValueError, match="没有启用的机器"):
        App(config)

    # 启动被拒绝时机器表已建好，可直接新增机器。
    assert MachineRepo(database_path).list_enabled() == []

    # 建好业务库并写入一台机器，应用按数据库自增编号绑定机器配置。
    created_machine_ids = create_machine_database(database_path, [{
        "machine_name": "测试机器",
        "camera_serial": "CAM001",
        "frequency_meter_serial": "FREQ001",
    }])
    machine_id = str(created_machine_ids[0])
    application = App(config)

    # 检查默认运行参数与数据库机器绑定，不覆盖相机自身参数。
    assert config.capture_window_ms == 1000
    assert config.ocr_result_timeout_ms == 30000
    assert config.recovery_path == database_path.with_suffix(".recovery.sqlite3")
    assert list(application.machine_managers) == [machine_id]
    machine = application.machine_managers[machine_id].machine
    assert machine.camera_serial == "CAM001"
    assert machine.frequency_meter_serial == "FREQ001"
    assert machine.camera_exposure_time_us is None
    assert machine.camera_gain is None
    assert machine.camera_pixel_format is None


def test_configuration_keys_match_config_fields() -> None:
    """验证每个配置键都对应配置类字段，且键只在一个文件里定义。

    Args:
        无外部参数。

    Returns:
        None  # 配置键与字段一一对应，没有多余键或多余字段
    """
    # 读取项目配置目录，取得全部配置键。
    configuration_directory = Path(__file__).resolve().parents[1] / "config"
    settings = read_configuration_settings(configuration_directory)

    # 逐项比对配置类字段，机器清单由数据库提供、不来自 YAML。
    field_names = {field.name for field in fields(AppConfig)}
    assert set(settings) == field_names


def test_duplicate_configuration_names(tmp_path: Path) -> None:
    """验证不同配置段的同名参数不能静默覆盖。

    Args:
        tmp_path: 临时配置目录。

    Returns:
        None  # 重复配置错误包含两段段名及参数名
    """
    # 准备配置文件，把采集窗口在 ocr 段重复设置一次。
    write_configuration_files(tmp_path, {"capture_window_ms": 1000})
    configuration_path = tmp_path / "config.yaml"
    sections = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    sections["ocr"] = {"capture_window_ms": 2000}
    configuration_path.write_text(yaml.safe_dump(sections, allow_unicode=True), encoding="utf-8")

    # 检查配置读取拒绝同名参数覆盖，并报出先后两个来源段。
    with pytest.raises(ValueError, match="capture_window_ms") as error_info:
        read_configuration_settings(tmp_path)
    assert "camera" in str(error_info.value)
    assert "ocr" in str(error_info.value)


def test_desktop_starts_without_machines(tmp_path: Path, monkeypatch, application) -> None:
    """验证桌面入口通过公共配置初始化空机器库并正常退出。

    Args:
        tmp_path: 临时配置及数据库目录。
        monkeypatch: 桌面入口和事件循环替换工具。
        application: 测试使用的 QApplication。

    Returns:
        None  # 桌面正常退出，空机器表已创建
    """
    from ui import __main__ as desktop_entry

    # 准备独立配置，使桌面入口读取临时数据库路径。
    settings = {
        "database_path": "measurements.sqlite3",
        "evidence_directory": "evidence",
        "mvs_development_directory": "sdk",
    }
    write_configuration_files(tmp_path, settings)
    monkeypatch.setattr(
        desktop_entry,
        "read_configuration_settings",
        lambda configuration_directory: read_configuration_settings(tmp_path),
    )

    # 复用测试应用并让事件循环立即退出，执行正式桌面初始化流程。
    monkeypatch.setattr(desktop_entry, "QApplication", lambda arguments: application)
    monkeypatch.setattr(application, "exec", lambda: 0)
    assert desktop_entry.run_desktop_preview() == 0
    assert MachineRepo(tmp_path / "measurements.sqlite3").list_enabled() == []
