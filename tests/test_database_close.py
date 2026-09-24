"""验证数据库连接和实例锁的退出清理。"""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config_util import AppConfig
from database import Database


def create_initialized_database(temporary_directory: Path) -> Database:
    """创建已取得实例锁的测试数据库。

    Args:
        temporary_directory: 测试数据库所在目录。

    Returns:
        返回示例：
            Database(...)  # 已打开运行库并持有实例锁的数据库
    """
    # 创建测试配置并初始化数据库资源。
    config = AppConfig(
        database_path=temporary_directory / "measurements.sqlite3",
        evidence_directory=temporary_directory / "evidence",
        mvs_development_directory=temporary_directory,
    )
    database = Database(config)
    database.initialize()
    return database


def patch_unlock_failure(
    monkeypatch: pytest.MonkeyPatch, unlock_error: OSError
) -> None:
    """使当前平台的显式解锁操作抛出指定异常。

    Args:
        monkeypatch: pytest 提供的属性替换工具。
        unlock_error: 显式解锁时抛出的异常。

    Returns:
        返回示例：
            None  # 当前平台的解锁函数已替换
    """
    # 替换当前平台的实例锁解锁函数。
    if os.name == "nt":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", Mock(side_effect=unlock_error))
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", Mock(side_effect=unlock_error))


@pytest.mark.parametrize("unlock_fails", [False, True])
def test_anchor_close_failure_still_releases_instance_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unlock_fails: bool,
) -> None:
    """确认运行库连接关闭失败后仍释放实例锁并抛出首次异常。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        unlock_fails: 显式解锁操作是否同时失败。

    Returns:
        返回示例：
            None  # 实例锁已释放，原连接关闭异常已抛出
    """
    # 初始化真实实例锁并替换运行库连接的关闭入口。
    database = create_initialized_database(tmp_path)
    database.anchor_connection.close()
    anchor_error = RuntimeError("运行库连接关闭失败")
    failed_connection = SimpleNamespace(close=Mock(side_effect=anchor_error))
    database.anchor_connection = failed_connection
    lock_file = database.lock_file

    # 执行关闭并核对首次异常身份。
    with monkeypatch.context() as lock_patch:
        if unlock_fails:
            patch_unlock_failure(lock_patch, OSError("显式解锁失败"))
        with pytest.raises(RuntimeError) as raised_error:
            database.close()
    assert raised_error.value is anchor_error
    failed_connection.close.assert_called_once()

    # 核对旧锁已释放且同路径实例可以再次初始化。
    assert lock_file.closed
    assert database.lock_file is None
    assert database.lock_acquired is False
    next_database = create_initialized_database(tmp_path)
    next_database.close()


def test_unlock_failure_still_closes_lock_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认显式解锁失败后继续关闭锁文件并抛出解锁异常。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 锁文件已关闭，解锁异常已抛出
    """
    # 初始化真实实例锁并让显式解锁失败。
    database = create_initialized_database(tmp_path)
    lock_file = database.lock_file
    unlock_error = OSError("显式解锁失败")

    # 关闭数据库并核对锁文件仍被关闭。
    with monkeypatch.context() as lock_patch:
        patch_unlock_failure(lock_patch, unlock_error)
        with pytest.raises(OSError) as raised_error:
            database.close()
    assert raised_error.value is unlock_error
    assert database.anchor_connection is None
    assert lock_file.closed
    assert database.lock_file is None
    assert database.lock_acquired is False


def test_close_releases_connection_and_instance_lock(tmp_path: Path) -> None:
    """确认正常关闭清空运行库连接和实例锁状态。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 运行库连接和实例锁均已释放
    """
    # 初始化数据库并保存待核对的资源引用。
    database = create_initialized_database(tmp_path)
    anchor_connection = database.anchor_connection
    lock_file = database.lock_file

    # 关闭数据库并核对全部资源状态。
    database.close()
    assert database.anchor_connection is None
    assert database.lock_file is None
    assert database.lock_acquired is False
    assert lock_file.closed
    with pytest.raises(sqlite3.ProgrammingError):
        anchor_connection.execute("SELECT 1")
