"""用配置目录无关的进程锁保护现场设备所有权。"""

import os
from pathlib import Path
import tempfile
from typing import BinaryIO


class HardwareOwnershipError(RuntimeError):
    """表示另一个监测实例仍然占用现场设备。"""


class HardwareOwnershipLock:
    """在整个监测生命周期内保持同一个操作系统文件锁。"""

    def __init__(self) -> None:
        """准备同一用户会话共用的设备锁路径。

        Args:
            无外部参数。

        Returns:
            None  # 尚未持有设备锁
        """
        self.path = Path(tempfile.gettempdir()) / "beltvision-hardware-owner.lock"
        self.file: BinaryIO | None = None

    def acquire(self) -> None:
        """非阻塞取得设备所有权，重复获取同一实例时保持原锁。

        Args:
            无外部参数。

        Returns:
            None  # 当前实例独占设备，竞争失败时抛出 HardwareOwnershipError
        """
        if self.file is not None:
            return
        lock_file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)

        # 对所有配置目录使用同一个排他锁。
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            lock_file.close()
            raise HardwareOwnershipError(
                "其他监测实例正在占用设备，请完全停止后再操作。",
            ) from error
        self.file = lock_file

    def release(self) -> None:
        """关闭锁文件并释放设备所有权，允许重复调用。

        Args:
            无外部参数。

        Returns:
            None  # 当前实例不再占用设备锁
        """
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self) -> "HardwareOwnershipLock":
        """在配置写入或启动准备期间取得设备锁。

        Args:
            无外部参数。

        Returns:
            HardwareOwnershipLock()  # 已加锁的当前实例
        """
        self.acquire()
        return self

    def __exit__(self, error_type, error, traceback) -> None:
        """在受保护操作结束后释放设备锁。

        Args:
            error_type: 上下文抛出的异常类型。
            error: 上下文抛出的异常实例。
            traceback: 上下文抛出的异常栈。

        Returns:
            None  # 原异常继续传播，设备锁已释放
        """
        self.release()
