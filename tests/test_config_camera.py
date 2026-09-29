"""验证公共相机参数读取和必要配置校验。"""

from dataclasses import replace
from pathlib import Path

import pytest

from config_util import AppConfig, load_config


@pytest.mark.parametrize(
    ("parameter_name", "parameter_value", "error_field"),
    [
        ("camera_exposure_time_us", 0.0, "camera_exposure_time_us"),
        ("camera_exposure_time_us", -1.0, "camera_exposure_time_us"),
        ("camera_gain", -1.0, "camera_gain"),
        ("camera_line_selector", None, "camera_line_selector"),
        ("camera_line_source", "  ", "camera_line_source"),
        ("camera_line_mode", "", "camera_line_mode"),
    ],
)
def test_camera_configuration_rejects_invalid_values(
    tmp_path: Path,
    parameter_name: str,
    parameter_value: object,
    error_field: str,
) -> None:
    """确认无效曝光、增益和频闪线路配置被拒绝。

    Args:
        tmp_path: pytest 提供的临时目录。
        parameter_name: 要替换的配置字段名称。
        parameter_value: 要写入的无效值。
        error_field: 预期错误文本中的配置字段。

    Returns:
        返回示例：
            None  # 无效配置抛出对应字段的 ValueError
    """
    # 建立有效基线配置并替换目标字段。
    configuration = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        camera_exposure_time_us=80.0,
        camera_gain=0.0,
        camera_line_selector="Line2",
        camera_line_mode="Strobe",
        camera_line_source="ExposureStartActive",
        camera_strobe_enabled=True,
    )
    invalid_configuration = replace(configuration, **{parameter_name: parameter_value})

    # 校验错误信息指出具体配置字段。
    with pytest.raises(ValueError, match=error_field):
        invalid_configuration.validate()


def test_camera_configuration_loads_public_parameters(tmp_path: Path) -> None:
    """确认现场配置填写线路值后生成完整公共相机参数。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # YAML 中的公共相机参数已进入 AppConfig
    """
    # 读取仓库配置并填入测试用线路枚举值。
    source_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    configuration_text = source_path.read_text(encoding="utf-8")
    configuration_text = configuration_text.replace(
        "camera_line_selector: null", "camera_line_selector: Line2"
    )
    configuration_text = configuration_text.replace(
        "camera_line_source: null", "camera_line_source: ExposureStartActive"
    )
    (tmp_path / "config.yaml").write_text(configuration_text, encoding="utf-8")

    # 加载配置并核对所有公共相机参数。
    configuration = load_config(tmp_path)
    assert configuration.camera_pixel_format == "Mono8"
    assert configuration.camera_exposure_time_us == 80.0
    assert configuration.camera_gain == 0.0
    assert configuration.camera_line_selector == "Line2"
    assert configuration.camera_line_mode == "Strobe"
    assert configuration.camera_line_source == "ExposureStartActive"
    assert configuration.camera_strobe_enabled is True


def test_camera_configuration_requires_site_line_values() -> None:
    """确认未填写现场线路枚举值的仓库配置不能启用频闪。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 缺失输出线路时加载配置直接抛出 ValueError
    """
    # 读取现场模板并核对缺失值的报错。
    configuration_directory = Path(__file__).resolve().parents[1] / "config"
    with pytest.raises(ValueError, match="camera_line_selector"):
        load_config(configuration_directory)
