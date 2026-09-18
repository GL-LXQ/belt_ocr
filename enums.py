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


class SessionState(str, Enum):
    """定义本次测量任务的处理状态，与机器启停状态分开。"""

    RUNNING = "RUNNING"  # 正在采集或等待 OCR、频率及证据处理完成
    WAITING_COMMIT_DB = "WAITING_COMMIT_DB"  # 结果完整，等待或正在提交数据库
    COMMITTED = "COMMITTED"  # 本轮结果已确认提交数据库
    FAILED = "FAILED"  # 本轮处理、中断或数据库提交失败


class OCRState(str, Enum):
    """定义整个 Session 的 OCR 处理状态，不表示单个图片或批次的识别结果。"""

    WAITING = "WAITING"  # 本 Session 等待最终 OCR 结果
    RUNNING = "RUNNING"  # 本 Session 正在处理 OCR，尚未确定最终结果
    SUCCESS = "SUCCESS"  # 本 Session 的最终 OCR 结果已确认成功
    FAILED = "FAILED"  # 本 Session 的 OCR 已确定失败
    TIMED_OUT = "TIMED_OUT"  # 本 Session 未在规定时间内完成 OCR


class FrequencyState(str, Enum):
    """定义整个 Session 的频率采集状态，不表示单次频率测量的结果。"""

    RUNNING = "RUNNING"  # 本 Session 正在采集频率，尚未确定最终结果
    SUCCESS = "SUCCESS"  # 本 Session 已封闭频率列表并确定有效的最终频率
    FAILED = "FAILED"  # 本 Session 缺少有效频率、频率采集故障或周期中断
