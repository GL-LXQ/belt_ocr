"""为业务测试提供按接收顺序交付测量的频率设备替身。"""

from enums import EventType
import asyncio
import math
from datetime import datetime, timezone
from itertools import cycle

from frequency_adapter import FrequencyAdapter
from models import FrequencyMeasurement, MeasurementEvent


class FakeFrequency(FrequencyAdapter):
    """提供有效测试测量，沿用正式适配器的周期管理。"""

    async def listen_measurements(self) -> None:
        """持续读取测试数值并顺序交付当前周期的新有效测量。

        Args:
            无外部参数。

        Returns:
            None  # 持续交付测试测量直到任务取消
        """
        # 初始化设备测量序号和测试数值来源。
        source_sequence = 0
        frequency_values = cycle(self.machine.simulated_frequencies_hz)
        while True:
            await asyncio.sleep(self.configuration.frequency_interval_ms / 1000)
            source_sequence += 1
            value_hz = next(frequency_values, None)

            # 在设备边界过滤无读数、非有限值和超出范围的读数。
            if value_hz is None or not math.isfinite(value_hz):
                continue
            if not self.configuration.minimum_frequency_hz <= value_hz <= self.configuration.maximum_frequency_hz:
                continue

            # 接收时固定所属周期，无活动周期时不交付数据。
            session_id = self.active_session_id
            if session_id is None:
                continue
            received_at = datetime.now(timezone.utc).isoformat()
            measurement = FrequencyMeasurement(
                session_id=session_id,
                frequency_source_id=self.machine.frequency_source_id,
                measurement_id=f"reading-{source_sequence}",
                source_sequence=source_sequence,
                value_hz=value_hz,
                measured_at=received_at,
                measured_monotonic=asyncio.get_running_loop().time(),
                received_at=received_at,
            )

            # 顺序等待测量事件入队，不安排延迟交付任务。
            await self.publish_event(MeasurementEvent(
                EventType.FREQUENCY_MEASURED, self.machine.machine_id, session_id, measurement,
            ))
