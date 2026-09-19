"""定义事件、采集结果和测量状态。"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4
from datetime import datetime, timezone

from mvs_sdk import CameraFrame

from enums import OCRState, FrequencyState, SessionState, EventType


@dataclass(frozen=True)
class MeasurementEvent:
    event_type: EventType
    machine_id: str
    session_id: str | None = None
    payload: Any = None
    event_id: str = field(default_factory=lambda: uuid4().hex)
    acknowledgement: asyncio.Future[None] | None = None
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    received_monotonic: float = field(default_factory=time.monotonic)


PublishEvent = Callable[[MeasurementEvent], Awaitable[None]]


@dataclass(frozen=True)
class CapturedFrame:
    session_id: str
    capture_id: str
    camera_id: str
    frame_id: str
    captured_at: str
    captured_monotonic: float
    image_data: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class FrequencyMeasurement:
    session_id: str
    frequency_source_id: str
    measurement_id: str
    source_sequence: int
    value_hz: float
    measured_at: str
    measured_monotonic: float
    received_at: str


@dataclass(frozen=True)
class OCRResult:
    ordered_lines: tuple[str, ...]
    selected_frames: tuple[CapturedFrame, ...]
    line_frame_ids: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class CaptureSummary:
    """携带本轮原始帧、采集统计、跳帧数和失败原因。"""

    capture_id: str
    frames: tuple[CameraFrame, ...] = field(default=(), repr=False)
    skipped_frame_count: int = 0
    statistics: dict = field(default_factory=dict)
    errors: tuple[str, ...] = ()


@dataclass
class BeltSession:
    session_id: str
    machine_id: str
    camera_id: str
    frequency_source_id: str
    capture_id: str
    start_time: str
    capture_start_time: float  # 本轮相机采集图片的起始时间，受理 START 时记录，单调时钟秒数
    capture_stop_time: float | None = None  # 本轮相机采集图片的停止截止时间，关闭或中断时记录，单调时钟秒数
    finish_time: str | None = None
    state: SessionState = SessionState.RUNNING  # 本轮任务的整体处理状态
    skipped_frame_count: int = 0
    capture_summary: dict = field(default_factory=dict)  # 本轮相机采集汇总，包含帧数、耗时和错误信息
    ocr_state: OCRState = OCRState.WAITING  # 本轮文字识别状态
    ocr_result: OCRResult | None = None
    frequency_window_sealed: bool = False
    measurement_frequencies: list[FrequencyMeasurement] = field(default_factory=list)
    frequency_state: FrequencyState = FrequencyState.RUNNING
    final_frequency: FrequencyMeasurement | None = None
    frozen_payload: str | None = None
    payload_hash: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        """判断本轮结果是否已确认入库。

        Args:
            无外部参数。

        Returns:
            True  # 本轮结果已确认入库
            False  # 本轮尚未入库或已失败
        """
        return self.state == SessionState.COMMITTED
