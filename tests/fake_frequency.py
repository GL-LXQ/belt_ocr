"""为业务测试提供独立的频率设备替身。"""

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


class FakeFrequency:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
    ) -> None:
        """准备测试设备绑定和在途交付任务集合。

        Args:
            machine: 测试机器和频率数据配置。
            configuration: 测试交付间隔、延迟及容量配置。
            publish_event: 测量和封口事件发布入口。

        Returns:
            None  # 测试设备已初始化
        """
        # 保存测试配置、事件入口和周期任务集合。
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event
        self.source_epoch = uuid4().hex
        self.active_window: FrequencyWindow | None = None
        self.windows: dict[str, FrequencyWindow] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def open_window(self, session_id: str, start_boundary: float) -> None:
        """登记本轮频率窗口。

        Args:
            session_id: 本轮测试测量编号。
            start_boundary: 业务入口提供的开始边界。

        Returns:
            None  # 本轮窗口已打开
        """
        window = FrequencyWindow(session_id)
        self.windows[session_id] = window
        self.active_window = window

    def seal_window(self, session_id: str, close_boundary: float) -> None:
        """关闭指定窗口并启动在途测量结算。

        Args:
            session_id: 需要关闭的测试周期编号。
            close_boundary: 业务入口提供的关闭边界。

        Returns:
            None  # 已安排本轮测试数据收尾
        """
        window = self.windows.get(session_id)
        if window is None:
            return
        if self.active_window is window:
            self.active_window = None
        task = asyncio.create_task(self.drain_window(window))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        """持续产生测试设备的新测量。

        Args:
            无外部参数。

        Returns:
            None  # 持续交付测试测量，直至任务取消
        """
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
            if (
                len(window.pending_deliveries)
                >= self.configuration.event_queue_capacity
            ):
                await self.publish_event(MeasurementEvent(
                    "FrequencyFailed", self.machine.machine_id, window.session_id,
                    "FREQUENCY_DELIVERY_CAPACITY_EXCEEDED",
                ))
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
        """延迟交付具有固定周期归属的测试测量。

        Args:
            session_id: 测量所属周期编号。
            source_sequence: 测量顺序编号。
            value_hz: 测量频率值。
            measured_at: UTC 测量时间。
            measured_monotonic: 测量产生时的主机单调时间。

        Returns:
            None  # 测量事件已交付
        """
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
        """等待已归属的测试数据，再发送窗口封口事件。

        Args:
            window: 待收尾的测试周期窗口。

        Returns:
            None  # 在途任务已结算并交付终态
        """
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
