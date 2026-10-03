"""验证设备进程锁覆盖直接 Runtime 调用和不同配置目录。"""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from config_util import AppConfig
from runtime.hardware_lock import HardwareOwnershipError, HardwareOwnershipLock
from runtime.system_runtime import SystemRuntime


@pytest.fixture
def ownership_path(tmp_path, monkeypatch):
    """让本测试的所有 Runtime 共用一个隔离的操作系统锁文件。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 替换工具。

    Returns:
        Path(...)  # 与具体数据库配置无关的共同锁路径
    """
    original = HardwareOwnershipLock.__init__
    path = tmp_path / "shared-hardware.lock"
    def initialize(lock):
        """只替换锁文件位置，保留真实文件锁逻辑。

        Args:
            lock: 当前所有权锁。

        Returns:
            None  # 测试锁路径已登记
        """
        original(lock)
        lock.path = path
    monkeypatch.setattr(HardwareOwnershipLock, "__init__", initialize)
    return path


@pytest.mark.asyncio
async def test_direct_runtime_checks_global_owner_before_database_or_device_access(tmp_path, ownership_path):
    """不同配置的直接运行入口仍共用同一设备所有权检查。

    Args:
        tmp_path: pytest 临时目录。
        ownership_path: 测试共用设备锁路径。

    Returns:
        None  # 锁竞争在创建数据库、SDK 或后台任务之前被拒绝
    """
    config = AppConfig(
        database_path=tmp_path / "other-config" / "data.sqlite3",
        evidence_directory=tmp_path / "other-evidence",
        mvs_development_directory=tmp_path,
    )
    runtime = SystemRuntime(config)
    runtime.database.initialize = Mock()
    with HardwareOwnershipLock():
        with pytest.raises(HardwareOwnershipError):
            await runtime.start()
    runtime.database.initialize.assert_not_called()
    assert runtime.camera_sdk is None
    assert runtime.worker_tasks == []
    await runtime.stop()


@pytest.mark.asyncio
async def test_start_failure_releases_global_owner_and_prevents_same_instance_restart(tmp_path, ownership_path):
    """真实 Runtime 初始化失败后释放设备锁且不允许复用已清理实例。

    Args:
        tmp_path: pytest 临时目录。
        ownership_path: 测试共用设备锁路径。

    Returns:
        None  # 新实例可以取得锁，失败实例不能再次连接设备
    """
    runtime = SystemRuntime(AppConfig(
        database_path=tmp_path / "data.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    ))
    runtime.database.initialize = Mock(side_effect=RuntimeError("隔离启动失败"))
    with pytest.raises(RuntimeError, match="隔离启动失败"):
        await runtime.start()
    with HardwareOwnershipLock():
        assert not runtime.cleanup_failed
    with pytest.raises(RuntimeError, match="新的测量应用实例"):
        await runtime.start()


def test_operating_system_lock_excludes_a_separate_process(ownership_path):
    """使用独立 Python 进程验证文件锁不是只在当前解释器中生效。

    Args:
        ownership_path: 测试共用设备锁路径。

    Returns:
        None  # 锁持有期间子进程失败，释放后子进程成功
    """
    source = (
        "from pathlib import Path; "
        "from runtime.hardware_lock import HardwareOwnershipLock; "
        "lock = HardwareOwnershipLock(); "
        f"lock.path = Path({str(ownership_path)!r}); "
        "lock.acquire(); lock.release()"
    )
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    with HardwareOwnershipLock():
        denied = subprocess.run([sys.executable, "-c", source], capture_output=True, env=environment, timeout=5)
    assert denied.returncode != 0
    assert "HardwareOwnershipError" in denied.stderr.decode()
    allowed = subprocess.run([sys.executable, "-c", source], capture_output=True, env=environment, timeout=5)
    assert allowed.returncode == 0
