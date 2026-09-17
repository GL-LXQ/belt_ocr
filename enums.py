"""集中定义业务枚举。"""

from enum import Enum


class MachineState(str, Enum):
    """定义机器的现场状态。"""

    # 机器已关闭，满足接收条件时可受理下一次启动事件。
    CLOSED = "CLOSED"

    # 机器正在运行，程序初始读到此状态时等待本轮关闭，不创建半轮测量。
    OPEN = "OPEN"

    # 机器状态尚未确认，暂停接收启动，等待有效关闭或现场状态同步。
    UNKNOWN = "UNKNOWN"
