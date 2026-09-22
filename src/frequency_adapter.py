"""定义持续接收频率数据的频率仪黑盒及当前周期归属。"""

from enums import EventType
import asyncio
import math
from itertools import cycle

from config_util import MachineConfig, AppConfig
from models import FrequencyMeasurement, RuntimeEvent, PublishEvent


class FrequencyAdapter:
    """持续接收新有效测量，按当前 Session 交付频率事件。"""

    def __init__(
        self,
        machine: MachineConfig,
        config: AppConfig,
        publish_event: PublishEvent,
    ) -> None:
        """登记频率仪配置、测量事件入口和当前周期编号。

        Args:
            machine: 当前机器与频率来源绑定。
            config: 频率仪参数及有效频率范围。
            publish_event: 与 START/CLOSE 共用机器 FIFO 队列的事件入口。

        Returns:
            返回示例：
                None  # 适配器已初始化，当前没有活动周期
        """
        # 登记频率仪绑定、公共参数和事件入口。
        self.machine = machine
        self.config = config
        self.publish_event = publish_event

        # 初始化当前周期编号空位，由机器业务处理器在 START/CLOSE 时设置或清空。
        self.active_session_id: str | None = None

    async def listen_measurements(self) -> None:
        """按配置循环产生联调频率，向当前周期交付新有效测量。

        当前为频率仪读取占位实现，后续替换为真实协议读取。

        Args:
            无外部参数；使用 simulated_frequencies_hz 和 frequency_interval_ms。

        Returns:
            返回示例：
                None  # 持续发送 FrequencyMeasured 事件，直到任务取消
        """
        # 按配置准备联调读数。
        frequency_values = cycle(self.machine.simulated_frequencies_hz)
        while True:
            # 按配置的读取间隔等待下一次读数。
            await asyncio.sleep(self.config.frequency_interval_ms / 10000)

            # 取下一条配置读数。
            value_hz = next(frequency_values, None)

            # 过滤无读数和非有限值。
            if value_hz is None or not math.isfinite(value_hz):
                continue

            # 过滤超出有效范围的读数。
            if not self.config.minimum_frequency_hz <= value_hz <= self.config.maximum_frequency_hz:
                continue

            # 取出当前活动周期，无活动周期时不交付。
            session_id = self.active_session_id
            if session_id is None:
                continue

            # 组装带周期身份的频率读数。
            measurement = FrequencyMeasurement(
                session_id=session_id,
                frequency_meter_serial=self.machine.frequency_meter_serial,
                value_hz=value_hz,
            )

            # 顺序等待本次读数入队。
            await self.publish_event(RuntimeEvent(
                EventType.FREQUENCY_MEASURED, self.machine.machine_id, session_id, measurement,
            ))
