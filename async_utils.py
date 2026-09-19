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
    cancelled = False
    while True:
        try:
            # 重复取消只登记状态，线程实际结束后才允许调用方释放资源。
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                logger.exception("释放阻塞操作时发生异常")
                raise asyncio.CancelledError
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result
