"""定义事件、采集结果和测量状态。"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4
from datetime import datetime, timezone

from enums import OCRState, FrequencyState


@dataclass(frozen=True)
class MeasurementEvent:
    event_type: str
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


PublishEvent = Callable[[MeasurementEvent], Awaitable[None]]


@dataclass(frozen=True)
class CapturedFrame:
    session_id: str
    capture_id: str
    camera_id: str
    frame_id: str
    captured_at: str
    captured_monotonic: float
    image_path: str = ""
    image_data: bytes = field(default=b"", repr=False)
    source_epoch: str = ""
    received_at: str = ""


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
    evidence_refs: tuple[str, ...]
    frame_ids: tuple[str, ...]


@dataclass(frozen=True)
class CaptureSummary:
    """携带本轮采集统计、跳帧数和失败原因。"""

    capture_id: str
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
    start_boundary: float
    close_time: str | None = None
    close_boundary: float | None = None
    finish_time: str | None = None
    cycle_state: str = "OPEN"
    capture_sealed: bool = False
    selected_frames: dict[str, CapturedFrame] = field(default_factory=dict)
    skipped_frame_count: int = 0
    capture_statistics: dict = field(default_factory=dict)
    ocr_state: OCRState = OCRState.WAITING  # 本轮文字识别状态
    ocr_result: OCRResult | None = None
    memory_frames: dict[str, CapturedFrame] = field(default_factory=dict)
    recognition_results: list[dict] = field(default_factory=list)
    pending_recognition_batches: int = 0
    text_postprocessing_started: bool = False
    frequency_window_sealed: bool = False
    measurement_frequencies: list[FrequencyMeasurement] = field(default_factory=list)
    frequency_state: FrequencyState = FrequencyState.RUNNING
    final_frequency: FrequencyMeasurement | None = None
    outcome: str = "UNDECIDED"
    commit_state: str = "NOT_READY"
    frozen_payload: str | None = None
    payload_hash: str | None = None
    errors: list[str] = field(default_factory=list)
    process_epoch: str = ""
    configuration_snapshot: dict = field(default_factory=dict)
    ocr_deadline: str = ""
    cycle_deadline: str = ""
    evidence_verified: bool = False
    evidence_validation_pending: bool = False

    @property
    def cycle_closed(self) -> bool:
        return self.cycle_state == "CLOSED"

    @property
    def finished(self) -> bool:
        return self.outcome == "COMPLETE" and self.commit_state == "COMMITTED"
