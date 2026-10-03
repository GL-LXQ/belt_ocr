"""验证打包预检不会读取现场配置或悄悄接受缺失的原生 OCR 依赖。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_backend.py"
SPEC = importlib.util.spec_from_file_location("build_backend", MODULE_PATH)
build_backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_backend)


def test_build_preflight_requires_python_312(monkeypatch, tmp_path):
    """验证解释器版本不满足时明确拒绝打包。

    Args:
        monkeypatch: pytest 属性替换工具。
        tmp_path: pytest 临时目录。

    Returns:
        完整返回示例：
        None  # 断言通过时结束
    """
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "__main__.py").touch()
    monkeypatch.setattr(build_backend, "ROOT", tmp_path)
    monkeypatch.setattr(build_backend.sys, "version_info", (3, 10))
    monkeypatch.setattr(build_backend.importlib.util, "find_spec", lambda name: object())
    assert build_backend.check_environment(require_ocr=False) == ["需要 Python 3.12，请使用项目 .venv 的解释器。"]


def test_build_preflight_ocr_is_required_unless_explicitly_development(monkeypatch, tmp_path):
    """验证正式包要求 Paddle，开发验证包需要显式选择。

    Args:
        monkeypatch: pytest 属性替换工具。
        tmp_path: pytest 临时目录。

    Returns:
        完整返回示例：
        None  # 断言通过时结束
    """
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "__main__.py").touch()
    monkeypatch.setattr(build_backend, "ROOT", tmp_path)
    monkeypatch.setattr(build_backend.sys, "version_info", (3, 12))
    monkeypatch.setattr(build_backend.importlib.util, "find_spec", lambda name: None if name == "paddle" else object())
    assert build_backend.check_environment(require_ocr=False) == []
    assert build_backend.check_environment(require_ocr=True) == ["缺少 paddle；请先安装到当前解释器环境。"]
