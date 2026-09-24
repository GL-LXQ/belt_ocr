"""验证运行时收到致命故障后停止受理信号并安排退出。"""

from pathlib import Path
from unittest.mock import AsyncMock

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
