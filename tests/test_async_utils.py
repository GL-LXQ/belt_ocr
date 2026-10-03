"""验证阻塞操作在线程中的上下文和退出取消保护。"""

import asyncio
from contextvars import ContextVar
from threading import Event

import pytest

from async_utils import run_blocking_operation


@pytest.mark.asyncio
async def test_executor_preserves_context_and_keyword_arguments():
    """显式执行器 Future 仍保留 to_thread 的上下文传播和参数语义。

    Args:
        无外部参数。

    Returns:
        None  # 返回值、位置参数、命名参数和上下文变量均保持一致
    """
    value = ContextVar("test_value", default="missing")
    value.set("preserved")
    def operation(prefix, *, suffix):
        """读取当前线程上下文并组合测试结果。

        Args:
            prefix: 前缀文本。
            suffix: 后缀文本。

        Returns:
            str  # 组合后的测试结果
        """
        return prefix + value.get() + suffix
    assert await run_blocking_operation(operation, "before-", suffix="-after") == "before-preserved-after"


@pytest.mark.asyncio
async def test_group_and_repeated_cancellation_wait_for_native_thread():
    """全局任务取消和重复取消不能跳过仍在执行的阻塞工作。

    Args:
        无外部参数。

    Returns:
        None  # 真正线程结束后才补抛 CancelledError
    """
    entered, release, completed = Event(), Event(), Event()
    def operation():
        """等待测试放行后登记真实工作结束。

        Args:
            无外部参数。

        Returns:
            str  # 阻塞工作结束后的结果
        """
        entered.set()
        release.wait(3)
        completed.set()
        return "finished"
    existing = asyncio.all_tasks()
    task = asyncio.create_task(run_blocking_operation(operation))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        affected = asyncio.all_tasks() - existing
        for active in affected:
            active.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
