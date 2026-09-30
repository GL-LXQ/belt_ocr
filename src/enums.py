"""集中定义业务枚举。"""

from enum import Enum


class MachineOverallStatus(str, Enum):
    """定义实时监测展示的机器整体可用状态。"""

    OFFLINE = "offline"
    ONLINE = "online"
    FAULT = "fault"


class SessionState(str, Enum):
    """定义本次测量任务的处理状态，与机器启停状态分开。"""

    RUNNING = "RUNNING"  # 正在采集或等待 OCR 和频率处理完成
    SAVING_RESULT = "SAVING_RESULT"  # 正在保存证据图片和测量记录
    COMMITTED = "COMMITTED"  # 本轮结果已确认提交数据库
    FAILED = "FAILED"  # 本轮处理、中断或数据库提交失败


class OCRState(str, Enum):
    """定义整个 Session 的 OCR 处理状态，不表示单张图片的识别结果。"""

    WAITING = "WAITING"  # 本 Session 等待最终 OCR 结果
    RUNNING = "RUNNING"  # 本 Session 正在处理 OCR，尚未确定最终结果
    COMPLETED = "COMPLETED"  # 本 Session 的 OCR 处理已返回正常或待复核结果
    FAILED = "FAILED"  # 本 Session 的 OCR 已确定失败
    TIMED_OUT = "TIMED_OUT"  # 本 Session 未在规定时间内完成 OCR


class FrequencyState(str, Enum):
    """定义整个 Session 的频率采集状态，不表示单次频率测量的结果。"""

    RUNNING = "RUNNING"  # 本 Session 正在采集频率，尚未确定最终结果
    SUCCESS = "SUCCESS"  # 本 Session 已封闭频率列表并确定有效的最终频率
    FAILED = "FAILED"  # 本 Session 缺少有效频率、频率采集故障或周期中断


class ProgressStage(str, Enum):
    """定义实时监测页面展示的本轮处理阶段。"""

    SESSION_START = "session_start"  # 本轮启动
    IMAGE_CAPTURE = "image_capture"  # 图像采集
    FREQUENCY_COLLECTION = "frequency_collection"  # 频率采集
    CHARACTER_RECOGNITION = "character_recognition"  # 字符识别
    EVIDENCE_STORAGE = "evidence_storage"  # 证据入库


class ProgressStatus(str, Enum):
    """定义本轮处理阶段已经产生的状态。"""

    RUNNING = "running"  # 正在处理
    SUCCESS = "success"  # 处理成功
    FAILED = "failed"  # 处理失败


class EventType(str, Enum):
    """定义机器与测量周期的业务事件类型。"""

    MACHINE_STARTED = "MachineStarted"  # 机器启动
    MACHINE_CLOSED = "MachineClosed"  # 机器正常关闭
    SHUTDOWN = "Shutdown"  # 应用退出
    IO_INTERRUPTED = "IOInterrupted"  # IO 读取中断
    CAPTURE_COMPLETED = "CaptureCompleted"  # 整轮采集结果
    CAPTURE_FAILED = "CaptureFailed"  # 相机采集设备故障
    OCR_COMPLETED = "OCRCompleted"  # 整轮识别结果
    OCR_FAILED = "OCRFailed"  # 整轮文字识别失败
    OCR_LOCK_WAIT_TIMEOUT = "OCRLockWaitTimeout"  # 等待共享 OCR 锁超时
    OCR_TIMEOUT = "OCRTimeout"  # 文字识别超时
    FREQUENCY_MEASURED = "FrequencyMeasured"  # 收到有效频率测量
    CYCLE_TIMEOUT = "CycleTimeout"  # 测量周期超时
