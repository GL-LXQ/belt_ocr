"""验证有界事件队列中的接收顺序、取消和退出行为。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from config_util import AppConfig
from enums import EventType, FrequencyState
from models import FrequencyMeasurement, MeasurementSession, RuntimeEvent
from runtime.machine_runtime import CycleContext
from runtime.system_runtime import SystemRuntime


@pytest.fixture
def event_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SystemRuntime:
    """建立两台使用单格事件队列的测试机器。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            SystemRuntime(...)  # 已建立机器，没有启动设备和数据库
    """
    # 使用最小有界队列建立运行时。
    runtime = SystemRuntime(AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        event_queue_capacity=1,
    ))

    # 用固定机器配置替代数据库读取。
    machine_rows = [
        {
            "id": machine_id,
            "machine_name": f"{machine_id}号皮带机",
            "camera_serial": f"camera-{machine_id}",
            "frequency_meter_serial": f"meter-{machine_id}",
        }
        for machine_id in (1, 2)
    ]
    monkeypatch.setattr("runtime.system_runtime.MachineRepo.list_enabled", Mock(return_value=machine_rows))
    runtime.initialize_machines()

    # 替代外部存储和相机停止操作，保留真实事件处理。
    runtime.database.save_abnormal_event = Mock()
    for machine in runtime.machines.values():
        machine.camera.inform_capture_workflow_stop = AsyncMock()
    return runtime


@pytest.mark.asyncio
async def test_frequency_cannot_overtake_waiting_close(event_runtime: SystemRuntime) -> None:
    """确认队列满时，晚到频率不会越过已经等待入队的关闭事件。

    Args:
        event_runtime: 带单格事件队列的测试运行时。

    Returns:
        返回示例：
            None  # 最终频率仍为关闭前的 10 Hz，关闭后的 99 Hz 已隔离
    """
    # 建立正在等待 OCR 的活动测量。
    machine = event_runtime.machines["1"]
    session = MeasurementSession(
        session_id="session-1",
        machine_id="1",
        camera_serial="camera-1",
        frequency_meter_serial="meter-1",
        capture_id="capture-1",
        start_time="2026-10-02T00:00:00+00:00",
        capture_start_time=0.0,
    )
    machine.cycles[session.session_id] = CycleContext(session)
    machine.active_session_id = session.session_id
    delivered_events = []

    async def consume_remaining_events() -> None:
        """按实际队列顺序处理关闭和晚到频率。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 两个事件已处理，并登记了实际接收顺序
        """
        for _ in range(2):
            event = await machine.queue.get()
            delivered_events.append(event)
            try:
                await machine.handle_event(event)
            finally:
                machine.queue.task_done()

    async def deliver_saturated_events() -> None:
        """填满队列并交错发送关闭与频率事件。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 两个并发发送任务和事件处理均已结束
        """
        # 先放入有效频率，让关闭事件等待队列空位。
        await event_runtime.publish_event(RuntimeEvent(
            EventType.FREQUENCY_MEASURED,
            "1",
            session.session_id,
            FrequencyMeasurement(session.session_id, "meter-1", 10.0),
        ))
        close_task = asyncio.create_task(event_runtime.publish_event(RuntimeEvent(
            EventType.MACHINE_CLOSED, "1", session.session_id,
        )))
        consumer_task = None
        try:
            await asyncio.sleep(0)
            assert not close_task.done()

            # 取走已有读数，在关闭发送任务恢复前交付一条新频率。
            first_event = machine.queue.get_nowait()
            await machine.handle_event(first_event)
            machine.queue.task_done()
            consumer_task = asyncio.create_task(consume_remaining_events())
            await event_runtime.publish_event(RuntimeEvent(
                EventType.FREQUENCY_MEASURED,
                "1",
                session.session_id,
                FrequencyMeasurement(session.session_id, "meter-1", 99.0),
            ))
            await close_task
            await consumer_task
        finally:
            # 清理失败断言或超时留下的测试任务。
            tasks = [task for task in (close_task, consumer_task) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # 检查最终频率与关闭截止时间，没有向数据库写入正式记录。
    await asyncio.wait_for(deliver_saturated_events(), timeout=1)
    assert session.final_frequency is not None
    assert session.final_frequency.value_hz == 10.0
    assert session.frequency_state == FrequencyState.SUCCESS
    assert [event.event_type for event in delivered_events] == [
        EventType.MACHINE_CLOSED,
        EventType.FREQUENCY_MEASURED,
    ]
    assert delivered_events[0].received_monotonic <= delivered_events[1].received_monotonic
    assert session.capture_stop_time == delivered_events[0].received_monotonic
    assert [reading.value_hz for reading in session.measurement_frequencies] == [10.0]
    event_runtime.database.save_abnormal_event.assert_called_once()
    await asyncio.wait_for(machine.queue.join(), timeout=1)


@pytest.mark.asyncio
async def test_full_machine_queue_does_not_block_other_machine(event_runtime: SystemRuntime) -> None:
    """确认一台机器等待队列空位时，其他机器仍能接收事件。

    Args:
        event_runtime: 带单格事件队列的测试运行时。

    Returns:
        返回示例：
            None  # 机器 1 仍在等待时，机器 2 已收到启动事件
    """
    # 填满机器 1 的队列并安排等待入队的关闭事件。
    first_machine = event_runtime.machines["1"]
    await event_runtime.publish_event(RuntimeEvent(EventType.MACHINE_STARTED, "1"))
    close_task = asyncio.create_task(event_runtime.publish_event(RuntimeEvent(EventType.MACHINE_CLOSED, "1")))
    try:
        await asyncio.sleep(0)
        assert not close_task.done()

        # 机器 2 的事件无需等待机器 1 释放空位。
        await asyncio.wait_for(
            event_runtime.publish_event(RuntimeEvent(EventType.MACHINE_STARTED, "2")),
            timeout=1,
        )
        second_machine = event_runtime.machines["2"]
        assert second_machine.queue.get_nowait().event_type == EventType.MACHINE_STARTED
        second_machine.queue.task_done()
        assert not close_task.done()
    finally:
        # 取消等待任务并清空测试队列。
        close_task.cancel()
        await asyncio.gather(close_task, return_exceptions=True)
        first_machine.discard_pending_events()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [True, False])
async def test_cancelled_publisher_does_not_block_next_event(
    event_runtime: SystemRuntime,
    cancel_first: bool,
) -> None:
    """确认等待队列或入队锁的任务取消后，剩余事件仍能正常入队。

    Args:
        event_runtime: 带单格事件队列的测试运行时。
        cancel_first: 是否取消第一个正在等待队列空位的发送任务。

    Returns:
        返回示例：
            None  # 取消任务已结束，未取消事件进入队列且队列计数正常
    """
    # 填满队列，再依次安排两个发送任务。
    machine = event_runtime.machines["1"]
    await event_runtime.publish_event(RuntimeEvent(EventType.MACHINE_STARTED, "1"))
    first_task = asyncio.create_task(event_runtime.publish_event(RuntimeEvent(EventType.MACHINE_CLOSED, "1")))
    await asyncio.sleep(0)
    second_task = asyncio.create_task(event_runtime.publish_event(RuntimeEvent(EventType.FREQUENCY_MEASURED, "1")))
    try:
        await asyncio.sleep(0)
        assert not first_task.done()
        assert not second_task.done()

        # 分别覆盖锁持有者和锁等待者的取消。
        cancelled_task = first_task if cancel_first else second_task
        remaining_task = second_task if cancel_first else first_task
        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task

        # 释放队列后，只接收未取消的事件。
        machine.queue.get_nowait()
        machine.queue.task_done()
        await asyncio.wait_for(remaining_task, timeout=1)
        expected_type = EventType.FREQUENCY_MEASURED if cancel_first else EventType.MACHINE_CLOSED
        assert machine.queue.get_nowait().event_type == expected_type
        machine.queue.task_done()
        await asyncio.wait_for(machine.queue.join(), timeout=1)
    finally:
        # 清理失败断言留下的发送任务。
        first_task.cancel()
        second_task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        machine.discard_pending_events()


@pytest.mark.asyncio
async def test_shutdown_releases_queue_and_lock_waiters(event_runtime: SystemRuntime) -> None:
    """确认退出释放队列空位和入队锁等待者，并取消未处理事件的回执。

    Args:
        event_runtime: 带单格事件队列的测试运行时。

    Returns:
        返回示例：
            None  # 退出和发送任务均已结束，队列清空，所有回执已取消
    """
    # 分别建立已入队、等待队列和等待入队锁的三个事件。
    machine = event_runtime.machines["1"]
    acknowledgements = [asyncio.get_running_loop().create_future() for _ in range(3)]
    await event_runtime.publish_event(RuntimeEvent(
        EventType.MACHINE_STARTED, "1", acknowledgement=acknowledgements[0],
    ))
    first_task = asyncio.create_task(event_runtime.publish_event(RuntimeEvent(
        EventType.MACHINE_CLOSED, "1", acknowledgement=acknowledgements[1],
    )))
    await asyncio.sleep(0)
    second_task = asyncio.create_task(event_runtime.publish_event(RuntimeEvent(
        EventType.FREQUENCY_MEASURED, "1", acknowledgement=acknowledgements[2],
    )))
    try:
        await asyncio.sleep(0)
        assert not first_task.done()
        assert not second_task.done()

        # 沿真实退出流程释放所有等待，未启动任何现场设备。
        await asyncio.wait_for(event_runtime.stop(), timeout=1)
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1)
        await asyncio.wait_for(machine.queue.join(), timeout=1)
        assert machine.queue.empty()
        assert all(acknowledgement.cancelled() for acknowledgement in acknowledgements)
        assert event_runtime.failure is None
    finally:
        # 清理失败断言留下的发送任务。
        first_task.cancel()
        second_task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        machine.discard_pending_events()


@pytest.mark.asyncio
@pytest.mark.parametrize("background_kind", ["cycle", "delivery", "refusal_audit"])
async def test_idle_waits_for_background_work_without_active_cycle(
    event_runtime: SystemRuntime, background_kind: str,
) -> None:
    """确认现场空闲时仍等待所有后台周期和实际任务。

    Args:
        event_runtime: 不连接设备的真实系统运行时。
        background_kind: 尚未结束的后台工作类型。

    Returns:
        返回示例：
            None  # 后台资源收尾后才报告 idle
    """
    # 注册一个由事件控制结束的后台任务。
    from enums import SessionState

    machine = event_runtime.machines["1"]
    release = asyncio.Event()
    background_task = asyncio.create_task(release.wait())
    session = MeasurementSession(
        session_id="background", machine_id="1", camera_serial="camera-1",
        frequency_meter_serial="meter-1", capture_id="capture-1",
        start_time="2026-10-03T00:00:00+00:00", capture_start_time=0.0,
        capture_stop_time=1.0, state=SessionState.FAILED,
    )
    if background_kind == "cycle":
        machine.cycles[session.session_id] = CycleContext(
            session, recognition_task=background_task,
        )
        background_task.add_done_callback(
            lambda task: machine.handle_recognition_task_finished(
                task, session.session_id
            )
        )
    elif background_kind == "delivery":
        machine.camera.unfinished_delivery_tasks.add(background_task)
        background_task.add_done_callback(
            lambda task: machine.camera.unfinished_delivery_tasks.discard(task)
        )
        background_task.add_done_callback(
            lambda task: machine.handle_delivery_finished(task, session.session_id)
        )
    else:
        machine.rejected_start_audit_task = background_task
        background_task.add_done_callback(machine.handle_rejected_start_audit_finished)

    # 没有现场活动周期时，等待也不能提前完成。
    waiting = asyncio.create_task(event_runtime.wait_until_idle(1))
    try:
        await asyncio.sleep(0)
        assert machine.active_session_id is None
        assert not waiting.done()
        release.set()
        await background_task
        await asyncio.wait_for(waiting, 1)
        assert not machine.cycles
        assert event_runtime.failure is None
    finally:
        release.set()
        await asyncio.gather(background_task, waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_frequency_keeps_generated_session_across_close_and_next_start(
    event_runtime: SystemRuntime,
) -> None:
    """确认常驻频率监听的旧读数保留生成时归属且不会写入新轮。

    Args:
        event_runtime: 使用真实机器事件入口的测试系统。

    Returns:
        返回示例：
            None  # 旧读数未改变关闭后频率，新读数只进入新 Session
    """
    from dataclasses import replace
    from frequency_adapter import FrequencyAdapter

    # 建立已关闭的旧轮和活动的新轮。
    machine = event_runtime.machines["1"]
    first = MeasurementSession(
        session_id="first", machine_id="1", camera_serial="camera-1",
        frequency_meter_serial="meter-1", capture_id="capture-1",
        start_time="2026-10-03T00:00:00+00:00", capture_start_time=0.0,
        capture_stop_time=1.0, frequency_state=FrequencyState.SUCCESS,
        final_frequency=FrequencyMeasurement("first", "meter-1", 10.0),
    )
    second = replace(first, session_id="second", capture_stop_time=None,
                     frequency_state=FrequencyState.RUNNING, final_frequency=None,
                     measurement_frequencies=[])
    machine.cycles = {
        first.session_id: CycleContext(first),
        second.session_id: CycleContext(second),
    }
    machine.active_session_id = second.session_id
    reading_generated = asyncio.Event()
    deliver_reading = asyncio.Event()
    delivered = asyncio.Queue()

    async def deliver_after_release(event):
        """阻塞已生成的旧读数并交给所属周期。

        Args:
            event: 生成时已经携带 Session ID 的频率事件。

        Returns:
            返回示例：
                None  # 读数按原身份交付且测试已获通知
        """
        if event.session_id == first.session_id:
            reading_generated.set()
            await deliver_reading.wait()
        await machine.handle_event(event)
        delivered.put_nowait(event)

    # 旧读数生成后才切换绑定，交付期间不重新归类。
    adapter = FrequencyAdapter(machine.machine_config,
                               replace(event_runtime.config, frequency_interval_ms=1),
                               deliver_after_release)
    adapter.active_session_id = first.session_id
    listener = asyncio.create_task(adapter.listen_measurements())
    try:
        await asyncio.wait_for(reading_generated.wait(), 1)
        adapter.active_session_id = second.session_id
        machine.frequency_adapter = adapter
        deliver_reading.set()
        old_event = await asyncio.wait_for(delivered.get(), 1)
        assert old_event.session_id == first.session_id
        assert old_event.payload.session_id == first.session_id
        assert first.final_frequency.value_hz == 10.0
        assert first.measurement_frequencies == []
        new_event = await asyncio.wait_for(delivered.get(), 1)
        assert new_event.session_id == second.session_id
        assert all(
            reading.session_id == second.session_id
            for reading in second.measurement_frequencies
        )
        event_runtime.database.save_abnormal_event.assert_called_once()
    finally:
        deliver_reading.set()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_io_interruption_targets_only_active_cycle(event_runtime: SystemRuntime):
    """确认 IO 中断只定位现场周期，旧周期中断事件不触碰新轮。

    Args:
        event_runtime: 使用真实信号入口的测试系统。

    Returns:
        返回示例：
            None  # 现场轮失败收尾，已 CLOSE 的后台轮继续等待识别
    """
    from dataclasses import replace
    from enums import SessionState

    # 同时保留一个正常 CLOSE 后台周期和一个现场活动周期。
    machine = event_runtime.machines["1"]
    old = MeasurementSession(
        session_id="old", machine_id="1", camera_serial="camera-1",
        frequency_meter_serial="meter-1", capture_id="capture-1",
        start_time="2026-10-03T00:00:00+00:00", capture_start_time=0.0,
        capture_stop_time=1.0,
    )
    current = replace(old, session_id="current", capture_stop_time=None, errors=[])
    machine.cycles = {
        old.session_id: CycleContext(old),
        current.session_id: CycleContext(current),
    }
    machine.active_session_id = current.session_id
    machine.frequency_adapter.active_session_id = current.session_id
    await machine.handle_event(RuntimeEvent(
        EventType.IO_INTERRUPTED, "1", old.session_id,
    ))
    assert machine.active_session is current
    assert not machine.waiting_cycle_reset
    machine.camera.inform_capture_workflow_stop.assert_not_awaited()

    async def handle_and_acknowledge(event):
        """接收系统定位的中断事件并完成回执。

        Args:
            event: 带现场周期编号的中断事件。

        Returns:
            返回示例：
                None  # 所属现场周期已中断，回执已完成
        """
        assert event.session_id == current.session_id
        await machine.handle_event(event)
        event.acknowledgement.set_result(None)

    # 真实 send_signal 使用 active_session_id，后台旧轮不参与中断。
    event_runtime.accepting_signals = True
    event_runtime.publish_event = handle_and_acknowledge
    await event_runtime.send_signal(EventType.IO_INTERRUPTED, "1")
    audits = [
        cycle.failure_audit_task for cycle in machine.cycles.values()
        if cycle.failure_audit_task is not None
    ]
    await asyncio.gather(*audits)
    assert current.state == SessionState.FAILED
    assert current.errors == ["IO 通信中断"]
    assert machine.active_session_id is None
    assert machine.frequency_adapter.active_session_id is None
    assert machine.waiting_cycle_reset
    assert old.state == SessionState.RUNNING
    assert old.errors == []
    assert machine.cycles[old.session_id].session is old
    machine.camera.inform_capture_workflow_stop.assert_awaited_once_with(None)


@pytest.fixture
def reset_runtime(event_runtime: SystemRuntime) -> SystemRuntime:
    """建立保留真实信号队列、可立即停流的单机复位测试环境。

    Args:
        event_runtime: 没有连接外部设备的真实系统运行时。

    Returns:
        返回示例：
            SystemRuntime(...)  # 旧轮占用现场，新轮采集由测试相机接收
    """
    from dataclasses import replace
    from types import SimpleNamespace
    from camera.camera import CaptureTask

    # 固定单机 DI 基线，并允许超时和 CLOSE 同时等待消费。
    machine = event_runtime.machines["1"]
    event_runtime.machines = {"1": machine}
    event_runtime.config = replace(event_runtime.config, io_machine_channels={"1": 0})
    event_runtime.io_previous_states = {0: True}
    event_runtime.accepting_signals = True
    machine.queue = asyncio.Queue(128)

    # 登记现场旧轮，保留真实失败审计与上下文回收流程。
    session = MeasurementSession(
        session_id="old", machine_id="1", camera_serial="camera-1",
        frequency_meter_serial="meter-1", capture_id="capture-1",
        start_time="2026-10-03T00:00:00+00:00", capture_start_time=0.0,
    )
    machine.cycles[session.session_id] = CycleContext(session)
    machine.active_session_id = session.session_id
    machine.frequency_adapter.active_session_id = session.session_id

    def start_capture(session_id: str, capture_start_time: float):
        """接收新轮采集并返回已停流的测试任务。

        Args:
            session_id: 新轮周期编号。
            capture_start_time: 新轮采集开始时间。

        Returns:
            返回示例：
                (
                    CaptureTask(...),  # 已完成现场停流
                    Future(...),  # 已结束的相机交付
                )
        """
        capture = CaptureTask(None, capture_start_time, 1.0, 50)
        capture.capture_finished.set()
        delivery = asyncio.get_running_loop().create_future()
        delivery.set_result(None)
        return capture, delivery

    # 只替换设备边界，不替换机器事件、周期或复位处理。
    machine.camera = SimpleNamespace(
        available=True, is_capturing=False, unfinished_delivery_tasks=set(),
        start_capture=Mock(side_effect=start_capture),
        inform_capture_workflow_stop=AsyncMock(), stop=AsyncMock(),
    )
    return event_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_mode", ["immediate", "delayed", "reconnected"])
@pytest.mark.parametrize("interruption", [EventType.CYCLE_TIMEOUT, EventType.IO_INTERRUPTED])
async def test_queued_physical_close_resets_interrupted_cycle(
    reset_runtime: SystemRuntime, stop_mode: str, interruption: EventType,
) -> None:
    """确认中断后的同轮真实 CLOSE 解除复位等待并受理下一次 DI 启动。

    Args:
        reset_runtime: 使用真实 DI 入口及 FIFO 的单机运行时。
        stop_mode: 立即停流、延迟停流或停流期间重新建立 IO 基线。
        interruption: 先于真实 CLOSE 排队的周期中断事件。

    Returns:
        返回示例：
            None  # 旧轮只失败一次，后续真实 START 已创建新轮采集
    """
    # 用明确的入队和停流通知控制两种事件交错。
    runtime = reset_runtime
    machine = runtime.machines["1"]
    old = machine.active_session
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()
    close_queued = asyncio.Event()
    original_publish = runtime.publish_event
    delayed_stop = stop_mode != "immediate"
    listener = None
    closing = None

    async def stop_after_release(capture_task):
        """等待测试放行后完成本轮停流。

        Args:
            capture_task: 需要停流的所属采集任务。

        Returns:
            返回示例：
                None  # 停流等待已结束
        """
        stop_entered.set()
        await release_stop.wait()

    async def publish_and_notify(event):
        """保留真实事件路由并通知 CLOSE 已入队。

        Args:
            event: 待入队的机器事件。

        Returns:
            返回示例：
                None  # 事件已入队，CLOSE 仍携带入队时的旧轮编号
        """
        await original_publish(event)
        if event.event_type == EventType.MACHINE_CLOSED:
            assert event.session_id == old.session_id
            close_queued.set()

    runtime.publish_event = publish_and_notify
    if delayed_stop:
        machine.camera.inform_capture_workflow_stop.side_effect = stop_after_release
    try:
        # 即时停流时先排入两事件；延迟停流时在中断处理期间排入 CLOSE。
        await runtime.publish_event(RuntimeEvent(interruption, "1", old.session_id))
        if delayed_stop:
            listener = asyncio.create_task(machine.listen_events())
            await asyncio.wait_for(stop_entered.wait(), 1)
        if stop_mode == "reconnected":
            # 停流期间重新读到 OPEN，下一次下降沿仍属于尚未释放的现场轮。
            runtime.io_previous_states.clear()
            await runtime.handle_io_states([True])
        closing = asyncio.create_task(runtime.handle_io_states([False]))
        await asyncio.wait_for(close_queued.wait(), 1)
        assert machine.queue.qsize() == (1 if delayed_stop else 2)
        if listener is None:
            listener = asyncio.create_task(machine.listen_events())
        release_stop.set()
        await asyncio.wait_for(closing, 1)

        # 真实下降沿完成复位，旧轮审计及回收不会改变复位结果。
        assert machine.active_session_id is None
        assert not machine.waiting_cycle_reset
        await runtime.wait_until_idle(1)
        assert not machine.cycles
        assert len(old.errors) == 1
        runtime.database.save_abnormal_event.assert_called_once()

        # 下一个真实上升沿立即创建新轮，不需要额外 CLOSE。
        await runtime.handle_io_states([True])
        assert machine.active_session_id != old.session_id
        assert machine.active_session_id is not None
        machine.camera.start_capture.assert_called_once()
        assert runtime.io_previous_states == {0: True}
        assert runtime.failure is None
    finally:
        release_stop.set()
        if closing is not None and not closing.done():
            closing.cancel()
        if listener is not None:
            listener.cancel()
        await asyncio.gather(*(task for task in (closing, listener) if task is not None), return_exceptions=True)
        await machine.release_resources("测试结束")


@pytest.mark.asyncio
@pytest.mark.parametrize("new_state", ["active", "capacity", "camera", "io_baseline", "io_interruption"])
async def test_old_close_does_not_reset_new_physical_cycle(
    reset_runtime: SystemRuntime, new_state: str,
) -> None:
    """确认旧轮 CLOSE 不关闭新轮或解除新一轮的拒收与 IO 复位等待。

    Args:
        reset_runtime: 可接受新轮采集的单机运行时。
        new_state: 旧轮复位后建立的新现场状态。

    Returns:
        返回示例：
            None  # 旧 CLOSE 被隔离，无周期编号的真实 CLOSE 仍能正常复位
    """
    from dataclasses import replace

    # 中断旧轮并等待其上下文回收，复位归属仍须保留。
    runtime = reset_runtime
    machine = runtime.machines["1"]
    old = machine.active_session
    old_close = RuntimeEvent(EventType.MACHINE_CLOSED, "1", old.session_id)
    try:
        await machine.handle_event(RuntimeEvent(EventType.CYCLE_TIMEOUT, "1", old.session_id))
        await runtime.wait_until_idle(1)
        assert not machine.cycles
        assert machine.waiting_cycle_reset
        if new_state not in {"io_baseline", "io_interruption"}:
            await machine.handle_event(old_close)
            assert not machine.waiting_cycle_reset

        # 分别建立新轮现场占用、容量拒收、相机拒收和重连初始 OPEN。
        if new_state == "capacity":
            machine.config = replace(machine.config, max_inflight_cycles=1)
            machine.cycles["background"] = CycleContext(replace(old, session_id="background"))
        elif new_state == "camera":
            machine.camera.available = False
        elif new_state == "io_baseline":
            runtime.io_previous_states.clear()
            await runtime.handle_io_states([True])
        elif new_state == "io_interruption":
            await machine.handle_event(RuntimeEvent(EventType.IO_INTERRUPTED, "1"))
        if new_state not in {"io_baseline", "io_interruption"}:
            await machine.handle_machine_start()
        current = machine.active_session
        assert machine.waiting_cycle_reset is (new_state != "active")
        stop_count = machine.camera.inform_capture_workflow_stop.await_count

        # 重复旧 CLOSE 保留新现场状态，不触发新轮停流。
        await machine.handle_event(old_close)
        assert machine.active_session is current
        assert machine.waiting_cycle_reset is (new_state != "active")
        assert machine.camera.inform_capture_workflow_stop.await_count == stop_count
        if new_state == "active":
            assert current.capture_stop_time is None
            assert machine.frequency_adapter.active_session_id == current.session_id
        else:
            await machine.handle_machine_start()
            machine.camera.start_capture.assert_not_called()

            # 无所属周期的真实 CLOSE 仍可解除当前拒收或基线复位等待。
            await machine.handle_event(RuntimeEvent(EventType.MACHINE_CLOSED, "1"))
            assert not machine.waiting_cycle_reset
    finally:
        await machine.release_resources("测试结束")
