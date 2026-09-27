"""按当前周期读取频率并交付测量事件。"""

import asyncio
import logging
import math
import random

from config_util import MachineConfig, AppConfig
from enums import EventType
from models import FrequencyMeasurement, RuntimeEvent, PublishEvent


logger = logging.getLogger(__name__)


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
        """按配置间隔读取当前周期的频率并交付有效测量。

        Args:
            无外部参数；使用当前活动周期和频率读取间隔。

        Returns:
            返回示例：
                None  # 持续发送 FrequencyMeasured 事件，直到任务取消
        """
        # 登记当前读取周期和已交付的读数数量。
        reading_session_id: str | None = None
        readings_sent = 0

        while True:
            # 按配置的读取间隔等待下一次读数。
            await asyncio.sleep(self.config.frequency_interval_ms / 1000)

            # 空闲时清除上一个周期的读取进度。
            session_id = self.active_session_id
            if session_id is None:
                reading_session_id = None
                readings_sent = 0
                continue

            # 新周期从第一条频率重新读取。
            if session_id != reading_session_id:
                reading_session_id = session_id
                readings_sent = 0

            # 每个周期交付三条频率后等待下一周期。
            if readings_sent >= 3:
                continue

            # 读取本次频率值。
            value_hz = random.uniform(0.01, 100.0)

            # 过滤无读数和非有限值。
            if value_hz is None or not math.isfinite(value_hz):
                continue

            # 过滤超出有效范围的读数。
            if not (
                self.config.minimum_frequency_hz
                <= value_hz
                <= self.config.maximum_frequency_hz
            ):
                continue

            # 组装带周期身份的频率读数。
            measurement = FrequencyMeasurement(
                session_id=session_id,
                frequency_meter_serial=self.machine.frequency_meter_serial,
                value_hz=value_hz,
            )

            # 顺序等待本次读数入队。
            event = RuntimeEvent(
                EventType.FREQUENCY_MEASURED,
                self.machine.machine_id,
                session_id,
                measurement,
            )
            await self.publish_event(event)
            readings_sent += 1

            # 记录本轮已交付的频率读数。
            logger.info(
                "频率读数 machine_id=%s session_id=%s reading=%s value_hz=%s",
                self.machine.machine_id,
                session_id,
                readings_sent,
                value_hz,
            )
