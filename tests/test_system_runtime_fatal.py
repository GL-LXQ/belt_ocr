"""验证运行时故障登记和退出资源释放。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config_util import AppConfig
from system_runtime import SystemRuntime


@pytest.mark.asyncio
async def test_fatal_error_stops_signals_and_schedules_shutdown_once(
    tmp_path: Path,
) -> None:
    """确认致命故障关闭信号入口并只安排一次退出任务。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 原始故障已保存，信号入口关闭且退出任务只执行一次
    """
    # 建立仅包含退出任务替身的运行时。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    runtime = SystemRuntime(config)
    runtime.accepting_signals = True
    runtime._shutdown_system_and_release_resources = AsyncMock()
    storage_error = OSError("证据图片写入失败")

    # 报告同一轮中的两次故障并等待既有退出任务。
    runtime.handle_fatal_error(storage_error)
    shutdown_task = runtime.shutdown_task
    runtime.handle_fatal_error(RuntimeError("后续故障"))
    await shutdown_task

    # 核对首次故障和信号入口状态。
    assert runtime.failure is storage_error
    assert runtime.failure_event.is_set()
    assert not runtime.accepting_signals
    assert runtime.shutdown_task is shutdown_task
    runtime._shutdown_system_and_release_resources.assert_awaited_once()

    # 新的测量信号不再进入运行时。
    with pytest.raises(RuntimeError, match="当前未接收信号"):
        await runtime.handle_start("1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disconnect_error", "previous_error"),
    [
        (RuntimeError("Modbus 关闭失败"), None),
        (RuntimeError("Modbus 关闭失败"), OSError("已有测量故障")),
        (None, None),
    ],
)
async def test_shutdown_releases_resources_after_modbus_disconnect(
    tmp_path: Path,
    disconnect_error: RuntimeError | None,
    previous_error: Exception | None,
) -> None:
    """确认 Modbus 关闭结果不阻断相机和数据库资源释放。

    Args:
        tmp_path: pytest 提供的临时目录。
        disconnect_error: 本次 Modbus 关闭时抛出的异常。
        previous_error: 退出前已经登记的故障。

    Returns:
        返回示例：
            None  # 后续资源已释放且首次故障保持不变
    """
    # 初始化持有运行库实例锁的 Runtime 和退出资源。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    runtime = SystemRuntime(config)
    runtime.database.initialize()
    runtime.modbus_client = SimpleNamespace(
        disconnect=AsyncMock(side_effect=disconnect_error),
    )
    runtime.camera_sdk = SimpleNamespace(close=Mock())

    # 按是否已有故障启动退出流程。
    if previous_error is not None:
        runtime.handle_fatal_error(previous_error)
    await runtime.stop()

    # 核对 Modbus、相机、数据库和实例锁的清理结果。
    runtime.modbus_client.disconnect.assert_awaited_once()
    runtime.camera_sdk.close.assert_called_once()
    assert runtime.database.anchor_connection is None
    assert runtime.database.lock_file is None
    assert runtime.database.lock_acquired is False
    assert runtime.state_changed.is_set()

    # 核对首次故障身份和故障通知状态。
    expected_error = previous_error if previous_error is not None else disconnect_error
    assert runtime.failure is expected_error
    assert runtime.failure_event.is_set() == (expected_error is not None)


@pytest.mark.asyncio
async def test_io_read_error_precedes_close_error(tmp_path: Path) -> None:
    """确认 IO 读取故障先于退出时的 Modbus 关闭故障保存。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 读取故障已保留，退出资源已释放
    """
    # 建立会在首次 IO 读取时报错的运行时。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        io_machine_channels={"1": 0},
    )
    runtime = SystemRuntime(config)
    runtime.database.initialize()
    machine = SimpleNamespace(
        discard_pending_events=Mock(),
        release_resources=AsyncMock(),
    )
    runtime.machines = {"1": machine}
    read_error = TypeError("IO 读取程序错误")
    disconnect_error = RuntimeError("Modbus 关闭失败")
    runtime.modbus_client = SimpleNamespace(
        read_discrete_inputs=AsyncMock(side_effect=read_error),
        disconnect=AsyncMock(side_effect=disconnect_error),
    )
    runtime.camera_sdk = SimpleNamespace(close=Mock())

    # 运行 IO 监听并等待已安排的退出流程完成。
    worker_task = asyncio.create_task(
        runtime.run_worker("Modbus IO", runtime.listen_io)
    )
    runtime.worker_tasks.append(worker_task)
    await worker_task
    await runtime.stop()

    # 核对读取故障优先保留且后续资源全部释放。
    assert runtime.failure is read_error
    runtime.modbus_client.disconnect.assert_awaited_once()
    runtime.camera_sdk.close.assert_called_once()
    assert runtime.database.anchor_connection is None
    assert runtime.database.lock_file is None
    assert runtime.database.lock_acquired is False
