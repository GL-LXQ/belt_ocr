"""验证 IO 断线后的状态失效和恢复同步。"""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config_util import AppConfig
from enums import EventType
from runtime.system_runtime import SystemRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovered_state", "later_states", "expected_events"),
    [
        (True, (False, True), (EventType.MACHINE_CLOSED, EventType.MACHINE_STARTED)),
        (False, (True,), (EventType.MACHINE_STARTED,)),
    ],
)
async def test_io_recovery_only_uses_first_read_as_baseline(
    tmp_path: Path,
    recovered_state: bool,
    later_states: tuple[bool, ...],
    expected_events: tuple[EventType, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """确认断线后首读只同步状态，后续边沿正常交付。

    Args:
        tmp_path: pytest 提供的临时目录。
        recovered_state: 通信恢复后的第一份 DI 状态。
        later_states: 恢复后继续读取的 DI 状态。
        expected_events: 恢复后应交付的边沿事件。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 断线仅通知一次且恢复首读未触发机器事件
    """
    # 记录 DI 初始状态和电平变化日志。
    caplog.set_level(logging.INFO)

    # 建立单机运行对象和轮询读数序列。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        io_machine_channels={"1": 0},
        modbus_poll_interval_ms=1,
        modbus_reconnect_interval_ms=1,
    )
    runtime = SystemRuntime(config)
    machine = SimpleNamespace(waiting_cycle_reset=False, current_session=None)
    runtime.machines = {"1": machine}
    runtime.accepting_signals = True
    readings = iter((False, True, None, None, recovered_state, *later_states))
    delivered_events = []

    async def read_discrete_inputs(address: int, count: int) -> list[bool] | None:
        """按顺序返回 DI 读数并在末尾停止轮询。

        Args:
            address: DI 起始地址。
            count: 本轮读取的 DI 数量。

        Returns:
            返回示例：
                [True]  # 本轮有效的 DI 状态
                None  # 本轮读取失败或轮询已结束
        """
        try:
            reading = next(readings)
        except StopIteration:
            runtime.stopping = True
            return None
        return None if reading is None else [reading]

    async def record_signal(
        event_type: EventType,
        machine_id: str,
        payload: object = None,
    ) -> None:
        """登记机器事件并模拟机器复位标志变化。

        Args:
            event_type: 本轮交付的机器事件类型。
            machine_id: 接收事件的机器编号。
            payload: 事件携带的现场状态。

        Returns:
            返回示例：
                None  # 事件已登记且机器复位标志已更新
        """
        delivered_events.append(event_type)
        if event_type == EventType.MACHINE_STARTED:
            machine.current_session = SimpleNamespace(capture_stop_time=None)
        elif event_type == EventType.IO_INTERRUPTED:
            machine.current_session = None
            machine.waiting_cycle_reset = True
        elif event_type == EventType.MACHINE_CLOSED:
            machine.waiting_cycle_reset = False

    # 运行轮询并核对断线与恢复前后的事件顺序。
    runtime.modbus_client = SimpleNamespace(
        read_discrete_inputs=read_discrete_inputs,
        disconnect=AsyncMock(),
    )
    runtime.send_signal = record_signal
    await asyncio.wait_for(runtime.listen_io(), timeout=1)
    assert delivered_events == [
        EventType.MACHINE_STARTED,
        EventType.IO_INTERRUPTED,
        *expected_events,
    ]
    assert runtime.io_previous_states == {0: later_states[-1]}
    runtime.modbus_client.disconnect.assert_not_awaited()

    # 核对首读只记录初始状态，首个边沿同时记录电平变化。
    assert "DI初始状态 machine_id=1 channel=0 state=False" in caplog.text
    assert "DI状态变化 machine_id=1 channel=0 previous=False current=True" in caplog.text

    # 核对每次 DI 电平变化产生一条日志和一条机器信号，正常轮询不产生日志。
    edge_logs = [record for record in caplog.records if "DI状态变化" in record.getMessage()]
    machine_signal_events = [
        event_type
        for event_type in delivered_events
        if event_type in {EventType.MACHINE_STARTED, EventType.MACHINE_CLOSED}
    ]
    assert len(edge_logs) == len(machine_signal_events)

    # 核对断线前后的两份首读各记录一条初始状态日志。
    baseline_logs = [record for record in caplog.records if "DI初始状态" in record.getMessage()]
    assert len(baseline_logs) == 2
