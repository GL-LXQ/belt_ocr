"""定义事件、采集结果和测量状态。"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4
from datetime import datetime, timezone

from camera.hikrobot_sdk import CameraFrame

from enums import OCRState, FrequencyState, SessionState, EventType


@dataclass(frozen=True)
class RuntimeEvent:
    """送入机器事件队列的一条业务事件。"""

    event_type: EventType  # 事件类型
    machine_id: str  # 事件归属的机器编号
    session_id: str | None = None  # 事件所属的测量周期编号，机器级事件为空
    payload: Any = None  # 事件携带的业务数据
    event_id: str = field(default_factory=lambda: uuid4().hex)  # 本次事件的唯一编号
    acknowledgement: asyncio.Future[None] | None = None  # 本次事件的处理回执

    # 事件创建时的 UTC 时间。
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # 事件路由入队时的 UTC 时间。
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    received_monotonic: float = field(default_factory=time.monotonic)  # 事件路由入队时的单调时间


# 事件路由入口的函数类型，接收事件并异步返回。
PublishEvent = Callable[[RuntimeEvent], Awaitable[None]]


@dataclass(frozen=True)
class CapturedFrame:
    """为相机原始帧登记测量周期和图片身份。"""

    session_id: str  # 本帧所属的测量周期编号
    capture_id: str  # 本轮采集编号，用于生成图片编号
    camera_serial: str  # 拍到本帧的相机序列号
    frame_id: str  # 本帧在本轮内的唯一图片编号
    captured_at: str  # 本帧接收时刻换算出的 UTC 时间
    captured_monotonic: float  # 本帧接收时的单调时间
    camera_frame: CameraFrame = field(repr=False)  # 本帧的原始图像及像素格式


@dataclass(frozen=True)
class FrequencyMeasurement:
    """频率仪交付的一条有效频率读数。"""

    session_id: str  # 读数所属的测量周期编号
    frequency_meter_serial: str  # 交付本读数的频率仪序列号
    value_hz: float  # 读数频率，单位赫兹


@dataclass(frozen=True)
class OCRResult:
    """保存本轮最终文字、证据图片或待复核原始帧。"""

    ordered_lines: tuple[str, ...]  # 最终文字的顺序列表
    normalized_lines: tuple[str, ...]  # 最终文字的去空格列表
    selected_frames: tuple[CapturedFrame, ...]  # 最终选中的内存图片
    line_frame_ids: tuple[tuple[str, ...], ...]  # 每条文字对应的证据图片编号
    review_frames: tuple[CapturedFrame, ...] = ()  # 待复核时保留的全部原始帧
    review_reason: str | None = None  # 待复核原因


@dataclass(frozen=True)
class CaptureResult:
    """保存成功采集的全部原始帧和统计。"""

    frames: tuple[CameraFrame, ...] = field(default=(), repr=False)  # 本轮全部原始帧
    statistics: dict = field(default_factory=dict)  # 本轮采集统计


@dataclass
class MeasurementSession:
    """一台机器一轮测量的全部现场数据。"""

    session_id: str  # 本轮测量周期编号
    machine_id: str  # 本轮归属的机器编号
    camera_serial: str  # 本轮使用的相机序列号
    frequency_meter_serial: str  # 本轮使用的频率仪序列号
    capture_id: str  # 本轮采集编号，用于生成图片编号
    start_time: str  # 本轮创建时的 UTC 时间
    capture_start_time: float  # 本轮相机采集图片的起始时间，受理 START 时记录，单调时钟秒数
    capture_stop_time: float | None = None  # 本轮相机采集图片的停止截止时间，关闭或中断时记录，单调时钟秒数
    finish_time: str | None = None  # 本轮结算时的 UTC 时间
    state: SessionState = SessionState.RUNNING  # 本轮任务的整体处理状态
    capture_summary: dict = field(default_factory=dict)  # 本轮相机采集汇总，包含帧数、耗时和错误信息
    ocr_state: OCRState = OCRState.WAITING  # 本轮文字识别状态
    ocr_result: OCRResult | None = None  # 本轮终选后的文字与图片
    frequency_window_sealed: bool = False  # 本轮频率列表是否已封闭
    measurement_frequencies: list[FrequencyMeasurement] = field(default_factory=list)  # 本轮收到的频率明细
    frequency_state: FrequencyState = FrequencyState.RUNNING  # 本轮频率采集状态
    final_frequency: FrequencyMeasurement | None = None  # 本轮结算选定的最终频率
    errors: list[str] = field(default_factory=list)  # 本轮累计的错误原因
