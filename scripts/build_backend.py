"""在当前操作系统构建 Tauri 使用的独立 Python sidecar。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "desktop" / "src-tauri" / "backend-runtime"


def check_environment(*, require_ocr: bool) -> list[str]:
    """检查打包解释器、核心依赖与可选 OCR 依赖。

    Args:
        require_ocr: 是否要求安装 Paddle 原生运行时。

    Returns:
        完整返回示例：
        ["需要 Python 3.12。"]  # 阻止构建的问题；空列表表示静态检查通过
    """
    errors: list[str] = []
    if sys.version_info[:2] != (3, 12):
        errors.append("需要 Python 3.12，请使用项目 .venv 的解释器。")

    # 只检查模块是否存在，不加载模型、不连接 SDK，也不读取现场配置。
    modules = ["PyInstaller", "fastapi", "uvicorn", "pydantic", "yaml", "numpy", "cv2", "serial", "pymodbus"]
    if require_ocr:
        modules.extend(["paddle", "paddleocr", "paddlex"])
    for module in modules:
        if importlib.util.find_spec(module) is None:
            errors.append(f"缺少 {module}；请先安装到当前解释器环境。")
    if not (ROOT / "src" / "api" / "__main__.py").is_file():
        errors.append("缺少 src/api/__main__.py 入口。")
    return errors


def build_backend() -> int:
    """检查依赖并将单目录 sidecar 发布到 Tauri 资源暂存目录。

    Args:
        无。

    Returns:
        完整返回示例：
        0  # 静态检查或构建成功；失败返回非零值
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查构建依赖，不打包或读取现场数据")
    parser.add_argument("--development-without-ocr", action="store_true", help="仅供 API 构建验证，产物不能启动现场监测")
    arguments = parser.parse_args()
    errors = check_environment(require_ocr=not arguments.development_without_ocr)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if arguments.check:
        print("构建依赖静态检查通过；这不验证模型、GPU、MVS、打包后的原生库或真实硬件。")
        return 0

    # 保留既有暂存产物，不自动覆盖操作者未确认的目录。
    if STAGING.exists():
        print(f"目标已存在：{STAGING}；请先将旧构建移到其他位置。", file=sys.stderr)
        return 1
    build_root = ROOT / "build" / "backend"
    build_root.mkdir(parents=True, exist_ok=True)
    entry = build_root / "entry.py"
    entry.write_text("from src.api.__main__ import main\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n", encoding="utf-8")

    # 单目录模式保留一个 Python 服务进程及其相邻依赖，避免单文件解包引导进程。
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "beltvision-backend", "--distpath", str(build_root / "dist"),
        "--workpath", str(build_root / "work"), "--specpath", str(build_root),
        "--paths", str(ROOT), "--collect-submodules", "src", "--collect-all", "uvicorn",
        "--collect-all", "cv2", "--collect-all", "numpy", "--collect-all", "pymodbus",
        "--collect-submodules", "serial", "--exclude-module", "PySide6", "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6", "--exclude-module", "qfluentwidgets", "--hidden-import", "ui.evidence_order",
    ]
    if arguments.development_without_ocr:
        command.extend(["--exclude-module", "paddle", "--exclude-module", "paddleocr", "--exclude-module", "paddlex"])
    else:
        for package in ("paddle", "paddleocr", "paddlex"):
            command.extend(["--collect-all", package])
    command.append(str(entry))
    subprocess.run(command, cwd=ROOT, check=True)

    # Tauri 按固定资源路径打包整个目录，源码配置、数据库和证据均不随包复制。
    built = build_root / "dist" / "beltvision-backend"
    binary_name = "beltvision-backend.exe" if sys.platform == "win32" else "beltvision-backend"
    if not (built / binary_name).is_file():
        raise RuntimeError("PyInstaller 未生成预期的 sidecar 可执行文件。")
    shutil.copytree(built, STAGING)
    manifest = {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "ocr_included": not arguments.development_without_ocr,
        "entrypoint": binary_name,
    }
    (STAGING / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {STAGING}。必须在目标机器验证 Paddle/MVS 和完整启停后，才能用于现场。")
    return 0


if __name__ == "__main__":
    raise SystemExit(build_backend())
