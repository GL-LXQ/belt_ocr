"""验证频率读取次数、周期归属和关闭后的停止行为。"""

import asyncio
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

import frequency_adapter as frequency_adapter_module
from config_util import AppConfig, MachineConfig
from enums import EventType, FrequencyState
from frequency_adapter import FrequencyAdapter
from machine import Machine
from models import BeltSession, RuntimeEvent


@pytest.mark.asyncio
async def test_frequency_readings_follow_each_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认每轮交付三条有效频率并在新周期重新读取。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的临时替换工具。

    Returns:
        返回示例：
            None  # 两个周期各收到三条频率，关闭后没有旧周期读数

    """
    # 创建机器配置和频率事件接收器。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        frequency_interval_ms=125,
    )
    machine_config = MachineConfig("1", "camera-1", "meter-1")
    delivered_events: list[RuntimeEvent] = []

    async def publish_event(event: RuntimeEvent) -> None:
        """登记当前周期收到的频率事件。

        Args:
            event: Adapter 交付的频率事件。

        Returns:
            返回示例：
                None  # 事件已加入接收列表

        """
        delivered_events.append(event)

    adapter = FrequencyAdapter(machine_config, config, publish_event)
    adapter.active_session_id = "first-session"

    # 按轮询次数切换周期并记录各阶段的事件数量。
    polling_intervals: list[float] = []
    event_counts: dict[str, int] = {}

    async def advance_polling(interval_seconds: float) -> None:
        """推进一次轮询并切换预定的周期状态。

        Args:
            interval_seconds: Adapter 本次等待的秒数。

        Returns:
            返回示例：
                None  # 本次轮询继续，最后一次抛出结束标记

        """
        polling_intervals.append(interval_seconds)
        poll_number = len(polling_intervals)
        if poll_number == 5:
            event_counts["after_first_extra_poll"] = len(delivered_events)
            adapter.active_session_id = None
        elif poll_number == 7:
            event_counts["after_first_close"] = len(delivered_events)
            adapter.active_session_id = "next-session"
        elif poll_number == 11:
            event_counts["after_next_extra_poll"] = len(delivered_events)
            adapter.active_session_id = None
        elif poll_number == 13:
            event_counts["after_next_close"] = len(delivered_events)
            raise RuntimeError("轮询结束")

    # 记录每次随机取值并保留真实的随机范围行为。
    random_read_count = 0

    def read_random_frequency(minimum_hz: float, maximum_hz: float) -> float:
        """读取指定范围内的随机频率并累计读取次数。

        Args:
            minimum_hz: 随机频率下限。
            maximum_hz: 随机频率上限。

        Returns:
            返回示例：
                50.0  # 本次取得的频率，单位赫兹

        """
        nonlocal random_read_count
        random_read_count += 1
        return random.uniform(minimum_hz, maximum_hz)

    random_source = SimpleNamespace(uniform=read_random_frequency)
    monkeypatch.setattr(frequency_adapter_module, "random", random_source)
    monkeypatch.setattr(
        frequency_adapter_module, "asyncio", SimpleNamespace(sleep=advance_polling)
    )

    # 运行两个周期并核对频率事件、停止边界和毫秒换算。
    with pytest.raises(RuntimeError, match="轮询结束"):
        await adapter.listen_measurements()

    expected_session_ids = ["first-session"] * 3 + ["next-session"] * 3
    assert len(delivered_events) == 6
    assert [event.session_id for event in delivered_events] == expected_session_ids
    assert [
        event.payload.session_id for event in delivered_events
    ] == expected_session_ids
    assert all(
        event.event_type == EventType.FREQUENCY_MEASURED
        for event in delivered_events
    )
    assert all(
        0.01 <= event.payload.value_hz <= 100.0
        for event in delivered_events
    )
    assert all(isinstance(event.payload.value_hz, float) for event in delivered_events)
    assert event_counts == {
        "after_first_extra_poll": 3,
        "after_first_close": 3,
        "after_next_extra_poll": 6,
        "after_next_close": 6,
    }
    assert random_read_count == 6
    assert all(interval_seconds == 0.125 for interval_seconds in polling_intervals)


@pytest.mark.asyncio
async def test_machine_collects_three_readings_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认频率事件进入机器并在关闭时选定最后一条读数。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的临时替换工具。

    Returns:
        返回示例：
            None  # 机器收集三条读数，关闭后保留最后一条为最终频率

    """
    # 建立活动周期和机器的频率事件入口。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    machine_config = MachineConfig("1", "camera-1", "meter-1")
    session = BeltSession(
        session_id="current-session",
        machine_id="1",
        camera_serial="camera-1",
        frequency_meter_serial="meter-1",
        capture_id="capture-1",
        start_time="2026-09-27T00:00:00+00:00",
        capture_start_time=0.0,
    )
    delivered_events: list[RuntimeEvent] = []
    system_errors: list[Exception] = []

    async def publish_event(event: RuntimeEvent) -> None:
        """把频率事件交给当前机器处理。

        Args:
            event: Adapter 交付的频率事件。

        Returns:
            返回示例：
                None  # 机器已处理本次事件

        """
        delivered_events.append(event)
        await machine.handle_event(event)

    async def stop_capture_workflow() -> None:
        """完成本轮相机采集停止通知。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 相机采集已停止

        """
        return None

    adapter = FrequencyAdapter(machine_config, config, publish_event)
    machine = Machine(
        machine_config=machine_config,
        config=config,
        camera=SimpleNamespace(inform_capture_workflow_stop=stop_capture_workflow),
        frequency_adapter=adapter,
        text_recognizer=SimpleNamespace(),
        database=SimpleNamespace(),
        publish_event=publish_event,
        notify_measurement_progress=None,
        on_system_failure=system_errors.append,
        state_changed=asyncio.Event(),
    )
    machine.current_session = session
    adapter.active_session_id = session.session_id

    # 第四次轮询结束后关闭周期，再继续轮询并检查旧周期事件数量。
    polling_count = 0
    event_count_at_close = 0

    async def advance_polling(interval_seconds: float) -> None:
        """推进频率轮询并在三次读数后关闭周期。

        Args:
            interval_seconds: Adapter 本次等待的秒数。

        Returns:
            返回示例：
                None  # 当前轮询继续，最后一次抛出结束标记

        """
        nonlocal polling_count, event_count_at_close
        polling_count += 1
        if polling_count == 5:
            event_count_at_close = len(delivered_events)
            await machine.handle_machine_close()
        elif polling_count == 8:
            raise RuntimeError("轮询结束")

    monkeypatch.setattr(
        frequency_adapter_module, "asyncio", SimpleNamespace(sleep=advance_polling)
    )

    # 核对 Machine 收到的读数、关闭状态和最终频率。
    with pytest.raises(RuntimeError, match="轮询结束"):
        await adapter.listen_measurements()

    assert event_count_at_close == 3
    assert len(delivered_events) == 3
    assert len(session.measurement_frequencies) == 3
    assert session.final_frequency is session.measurement_frequencies[-1]
    assert session.frequency_state == FrequencyState.SUCCESS
    assert session.frequency_window_sealed is True
    assert session.capture_stop_time is not None
    assert adapter.active_session_id is None
    assert system_errors == []
