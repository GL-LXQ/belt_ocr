"""定义持续接收频率数据的设备黑盒及当前周期归属。"""

from enums import EventType
import asyncio
import math
from datetime import datetime, timezone
from itertools import cycle
from uuid import uuid4

from configuration import MachineConfiguration, MeasurementConfiguration
from models import FrequencyMeasurement, MeasurementEvent, PublishEvent


class FrequencyAdapter:
    """持续接收新有效测量，按当前 Session 交付频率事件。"""

    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
    ) -> None:
        """登记设备配置、测量事件入口和当前周期编号。

        Args:
            machine: 当前机器与频率来源绑定。
            configuration: 设备参数及有效频率范围。
            publish_event: 与 START/CLOSE 共用机器 FIFO 队列的事件入口。

        Returns:
            None  # 适配器已初始化，当前没有活动周期
        """
        # 保存设备绑定、参数和事件发布入口。
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event

        # 由机器业务处理器在 START/CLOSE 时设置或清空当前周期。
        self.active_session_id: str | None = None

    async def run(self) -> None:
        """运行持续监听入口，将读取异常交给后台任务入口处理。

        Args:
            无外部参数。

        Returns:
            None  # 监听被取消；读取失败时抛出异常
        """
        # 调用设备黑盒，连接和持续读取由该接口完成。
        await self.listen_measurements()

    async def listen_measurements(self) -> None:
        """按配置循环产生联调频率，向当前 Session 交付新有效测量。

        Args:
            无外部参数；使用 simulated_frequencies_hz 和 frequency_interval_ms。

        Returns:
            None  # 持续发送 FrequencyMeasured 事件，直到任务取消

        当前为设备读取占位实现，后续替换为真实协议读取。
        """
        # 按配置准备联调读数，并为本次监听分配测量身份前缀。
        source_epoch = uuid4().hex
        source_sequence = 0
        frequency_values = cycle(self.machine.simulated_frequencies_hz)
        while True:
            await asyncio.sleep(self.configuration.frequency_interval_ms / 10000)
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
                measurement_id=f"{source_epoch}-{source_sequence}",
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
