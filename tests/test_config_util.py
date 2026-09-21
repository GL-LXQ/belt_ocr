"""验证分组配置的路径解析、默认参数和桌面启动。"""

from dataclasses import fields
from pathlib import Path

import pytest

from config_util import AppConfig, load_configuration, read_configuration_settings
from configuration_support import write_configuration_files
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

    # 完整运行配置在没有启用机器时拒绝启动，保留设备表供新增机器。
    with pytest.raises(ValueError, match="没有启用的机器"):
        load_configuration(configuration_directory)
    machine_identifier = MachineRepo(database_path).insert("测试机器", "CAM001", "FREQ001")
    config = load_configuration(configuration_directory)

    # 检查默认运行参数与数据库机器绑定，不覆盖相机自身参数。
    assert config.capture_window_ms == 1000
    assert config.ocr_result_timeout_ms == 30000
    assert config.recovery_path == database_path.with_suffix(".recovery.sqlite3")
    assert config.machines[0].machine_id == str(machine_identifier)
    assert config.machines[0].camera_exposure_time_us is None
    assert config.machines[0].camera_gain is None
    assert config.machines[0].camera_pixel_format is None


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
    field_names = {field.name for field in fields(AppConfig)} - {"machines"}
    assert set(settings) == field_names


def test_duplicate_configuration_names(tmp_path: Path) -> None:
    """验证不同文件的同名参数不能静默覆盖。

    Args:
        tmp_path: 临时配置目录。

    Returns:
        None  # 重复配置错误包含文件名及参数名
    """
    # 准备配置文件，在另一业务文件中重复设置采集窗口。
    write_configuration_files(tmp_path, {"capture_window_ms": 1000})
    (tmp_path / "ocr.yaml").write_text("capture_window_ms: 2000\n", encoding="utf-8")

    # 检查配置读取拒绝同名参数覆盖。
    with pytest.raises(ValueError, match="ocr.yaml.*capture_window_ms"):
        read_configuration_settings(tmp_path)


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
