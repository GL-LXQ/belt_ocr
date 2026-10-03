"""为系统配置草稿读取现有参数，不校验运行条件或写入文件。"""

from pathlib import Path

import yaml

from src.config_util import read_configuration_settings


def read_configuration_preview(configuration_directory: Path) -> tuple[dict, str]:
    """读取页面所需的实际配置，并将文件和结构错误转换为提示。

    Args:
        configuration_directory: 包含 config.yaml 的配置目录。

    Returns:
        返回示例：
            (
                {"camera_gain": None},  # 保留实际字段、数值和空值的配置字典
                "",  # 成功时没有错误提示
            )
            (
                {},  # 读取失败时不使用猜测的默认配置
                "未找到配置文件，请检查配置目录。",  # 可在页面展示的错误提示
            )
    """
    # 复用非运行校验的读取入口，保留尚未填写完整的相机参数。
    try:
        settings = read_configuration_settings(configuration_directory)
    except FileNotFoundError:
        return (
            {},
            "未找到配置文件，请检查配置目录后重新读取。",
        )
    except (OSError, UnicodeError) as error:
        return (
            {},
            f"无法读取配置文件，请检查文件和访问权限。\n{error}",
        )
    except (yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as error:
        return (
            {},
            f"配置文件格式或字段结构异常，请检查现有配置后重新读取。\n{error}",
        )

    # 仅返回读取结果，不调用运行校验或任何配置保存接口。
    return (
        settings,
        "",
    )
