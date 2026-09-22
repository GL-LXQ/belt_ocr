"""提供异步业务中复用的阻塞操作执行工具。"""

import asyncio


async def run_blocking_operation(operation, *arguments, **keyword_arguments):
    """在线程中执行阻塞操作，取消时等待文件和事务释放。

    Args:
        operation: 在线程中执行的同步函数。
        arguments: 传给同步函数的位置参数。
        keyword_arguments: 传给同步函数的关键字参数。

    Returns:
        返回示例：
            None  # 同步函数无返回值时透传 None
            "runtime/measurements.sqlite3"  # 同步函数返回字符串时透传该结果
    """
    # 在线程中启动阻塞操作。
    task = asyncio.create_task(asyncio.to_thread(operation, *arguments, **keyword_arguments))

    # 登记是否收到过取消请求。
    cancelled = False
    while True:
        try:
            # 等待线程结果，取消不会中断线程执行。
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            # 线程自身已被取消时直接抛出。
            if task.cancelled():
                raise

            # 线程仍在运行时记录取消状态并继续等待。
            cancelled = True
        except Exception:
            # 此前收到过取消时重新登记取消请求。
            if cancelled:
                asyncio.current_task().cancel()
            raise

    # 线程结束后补抛此前收到的取消。
    if cancelled:
        raise asyncio.CancelledError

    # 透传同步函数的结果。
    return result
