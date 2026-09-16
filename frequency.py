"""持续产生模拟新测量，按打开的窗口确定测量归属。"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import cycle
from uuid import uuid4

from configuration import MachineConfiguration, MeasurementConfiguration
from models import FrequencyMeasurement, MeasurementEvent, PublishEvent


@dataclass
class FrequencyWindow:
    session_id: str
    pending_deliveries: set[asyncio.Task[None]] = field(default_factory=set)


class SimulatedFrequency:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
    ) -> None:
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event
        self.source_epoch = uuid4().hex
        self.active_window: FrequencyWindow | None = None
        self.windows: dict[str, FrequencyWindow] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def open_window(self, session_id: str) -> None:
        """登记本轮频率窗口。"""
        window = FrequencyWindow(session_id)
        self.windows[session_id] = window
        self.active_window = window

    def seal_window(self, session_id: str) -> None:
        """关闭指定窗口并启动在途测量结算。"""
        window = self.windows.get(session_id)
        if window is None:
            return
        if self.active_window is window:
            self.active_window = None
        task = asyncio.create_task(self.drain_window(window))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        """持续模拟仪器产生的新测量。"""
        source_sequence = 0
        frequency_values = cycle(self.machine.simulated_frequencies_hz)
        while True:
            await asyncio.sleep(self.configuration.frequency_interval_ms / 1000)

            # 为每次测量分配序号并检查数值范围。
            source_sequence += 1
            value_hz = next(frequency_values, None)
            if value_hz is None or not math.isfinite(value_hz):
                continue
            if not (
                self.configuration.minimum_frequency_hz
                <= value_hz <= self.configuration.maximum_frequency_hz
            ):
                continue

            # 在测量产生时绑定 Session，无窗口的读数直接忽略。
            window = self.active_window
            if window is None:
                continue
            measured_at = datetime.now(timezone.utc).isoformat()
            measured_monotonic = asyncio.get_running_loop().time()
            task = asyncio.create_task(self.deliver_measurement(
                window.session_id, source_sequence, value_hz,
                measured_at, measured_monotonic,
            ))
            window.pending_deliveries.add(task)
            task.add_done_callback(window.pending_deliveries.discard)

    async def deliver_measurement(
        self, session_id: str, source_sequence: int, value_hz: float,
        measured_at: str, measured_monotonic: float,
    ) -> None:
        """模拟传输延迟后发布具有固定归属的测量。"""
        await asyncio.sleep(self.configuration.frequency_delivery_delay_ms / 1000)
        measurement = FrequencyMeasurement(
            session_id, self.machine.frequency_source_id,
            f"{self.source_epoch}-{source_sequence}", source_sequence,
            value_hz, measured_at, measured_monotonic,
            datetime.now(timezone.utc).isoformat(),
        )
        await self.publish_event(MeasurementEvent(
            "FrequencyMeasured", self.machine.machine_id, session_id, measurement,
        ))

    async def drain_window(self, window: FrequencyWindow) -> None:
        """等待已归属的在途数据，再发送窗口封口事件。"""
        try:
            # 有限时间内结算本轮已经产生的测量。
            if window.pending_deliveries:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(window.pending_deliveries)),
                    self.configuration.frequency_drain_timeout_ms / 1000,
                )
        except asyncio.TimeoutError:
            await self.publish_event(MeasurementEvent(
                "FrequencyFailed", self.machine.machine_id, window.session_id,
                "FREQUENCY_DRAIN_TIMEOUT",
            ))
        finally:
            # 只移除本轮窗口，不改变新一轮的活动窗口。
            self.windows.pop(window.session_id, None)

        # 在本轮已归属测量处理完毕后发送封口事件。
        await self.publish_event(MeasurementEvent(
            "FrequencyWindowSealed", self.machine.machine_id, window.session_id,
        ))
