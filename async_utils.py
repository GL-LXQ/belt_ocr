"""提供异步业务中复用的阻塞操作执行工具。"""

import asyncio
import logging


logger = logging.getLogger(__name__)


async def run_blocking_operation(operation, *arguments, **keyword_arguments):
    """在线程中执行阻塞操作，取消时等待文件和事务释放。

    Args:
        operation: 在线程中执行的同步函数。
        arguments: 传给同步函数的位置参数。
        keyword_arguments: 传给同步函数的关键字参数。

    Returns:
        object: 同步函数返回的原始结果。
        返回示例：
            None  # 同步函数没有返回数据
    """
    # 在线程中启动阻塞操作，并避免外层取消直接中断资源处理。
    task = asyncio.create_task(asyncio.to_thread(
        operation, *arguments, **keyword_arguments,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 等待已经开始的文件或事务操作结束。
        try:
            await task
        except Exception:
            logger.exception("释放阻塞操作时发生异常")
        raise
