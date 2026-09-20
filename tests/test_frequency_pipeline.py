"""验证频率立即关闭、按接收顺序结算和 SQLite 明细存储。"""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import App
from configuration import load_configuration
from enums import OCRState, FrequencyState, SessionState, EventType
from models import CapturedFrame, BeltSession, FrequencyMeasurement, MeasurementEvent, OCRResult


@pytest.fixture
def frequency_context(tmp_path):
    """准备独立数据库、业务处理器和准备关闭的周期。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        (
            app,  # 未启动设备的应用及独立数据库
            manager,  # M01 的串行业务处理器
            session,  # 图像已收尾、OCR 已成功的测试周期
        )
    """
    # 创建独立存储配置，启用较短的频率收尾期限。
    configuration = load_configuration(Path(__file__).resolve().parents[1] / "config.example.json")
    configuration = replace(
        configuration,
        database_path=tmp_path / "measurements.sqlite3",
        recovery_database_path=tmp_path / "recovery.sqlite3",
        evidence_directory=tmp_path / "evidence",
    )
    app = App(configuration)
    app.database.initialize()
    manager = app.machine_managers["M01"]

    # 建立已完成图像收尾但尚未关闭的周期。
    session = BeltSession(
        session_id="frequency-session",
        machine_id="M01",
        camera_id="CAM01",
        frequency_source_id="FREQ01",
        capture_id="capture-frequency",
        start_time="2026-09-18T00:00:10+00:00",
        capture_start_time=10,
        ocr_state=OCRState.SUCCESS,
        ocr_result=OCRResult(
            ordered_lines=("MODEL",),
            selected_frames=(CapturedFrame(
                session_id="frequency-session",
                capture_id="capture-frequency",
                camera_id="CAM01",
                frame_id="frame-1",
                captured_at="2026-09-18T00:00:10+00:00",
                captured_monotonic=10,
                image_data=b"BM-test",
            ),),
            line_frame_ids=(("frame-1",),),
        ),
    )
    manager.current_session = session
    manager.frequency_adapter.active_session_id = session.session_id
    manager.camera.inform_capture_workflow_stop = AsyncMock()
    try:
        yield app, manager, session
    finally:
        app.database.close()


def create_measurement(session, value):
    """创建具有明确周期身份和频率值的设备测试测量。

    Args:
        session: 测量所属周期。
        value: 频率值，单位 Hz。

    Returns:
        FrequencyMeasurement(
            session_id="frequency-session",  # 所属周期
            frequency_source_id="FREQ01",  # 仪器来源
            value_hz=42.0,  # 频率值
        )
    """
    return FrequencyMeasurement(
        session_id=session.session_id,
        frequency_source_id=session.frequency_source_id,
        value_hz=value,
    )


def test_close_freezes_received_frequencies_and_saves_together(frequency_context):
    """验证 CLOSE 立即选取最后收到的频率并将明细与最终值一起入库。

    Args:
        frequency_context: 应用、处理器和测量周期。

    Returns:
        None  # 已验证接收顺序、立即封口及数据库幂等写入
    """
    app, manager, session = frequency_context

    async def receive_and_close():
        """依次交付多条频率读数，再执行正常关闭。

        Args:
            无外部参数。

        Returns:
            None  # 关闭已经生成冻结提交请求
        """
        # 按接收顺序保存全部读数，保留连续相同的频率值。
        for measurement in (
            create_measurement(session, 42.0),
            create_measurement(session, 42.0),
            create_measurement(session, 43.0),
        ):
            await manager.handle_event(MeasurementEvent(
                EventType.FREQUENCY_MEASURED, "M01", session.session_id, measurement,
            ))
        assert len(session.measurement_frequencies) == 3
        assert session.state != SessionState.WAITING_COMMIT_DB

        # CLOSE 返回时频率已结算，没有等待设备或另发封口事件。
        await manager.handle_machine_close()
        assert manager.frequency_adapter.active_session_id is None
        assert session.frequency_window_sealed
        assert session.final_frequency is session.measurement_frequencies[-1]
        assert session.state == SessionState.WAITING_COMMIT_DB

    asyncio.run(receive_and_close())

    # 重复写入同一请求，检查频率明细与最终频率一致。
    request = app.database.queue.get_nowait()
    app.database.write_record(request)
    app.database.write_record(request)
    app.database.queue.task_done()
    with sqlite3.connect(app.configuration.database_path) as connection:
        rows = connection.execute(
            "SELECT measurement_frequencies, final_frequency_hz FROM measurements"
        ).fetchall()
    assert len(rows) == 1
    frequencies = json.loads(rows[0][0])
    assert [measurement["value_hz"] for measurement in frequencies] == [42.0, 42.0, 43.0]
    assert rows[0][1] == frequencies[-1]["value_hz"] == 43.0
    assert all(
        set(measurement) == {"session_id", "frequency_source_id", "value_hz"}
        for measurement in frequencies
    )


@pytest.mark.parametrize("outcome", ["empty", "interrupted"])
def test_close_preserves_partial_data_without_final_value(frequency_context, outcome):
    """验证无读数及中断时保留明细但不确认最终频率。

    Args:
        frequency_context: 应用、处理器和周期。
        outcome: 无测量或周期中断场景。

    Returns:
        None  # 异常周期已清理且未入库，最终频率为空
    """
    app, manager, session = frequency_context

    async def receive_and_close():
        """接收部分数据并以指定结果关闭本轮。

        Args:
            无外部参数。

        Returns:
            None  # 本轮频率已封闭，失败周期未提交数据库
        """
        # 中断场景先接收部分有效数据。
        if outcome != "empty":
            await manager.handle_event(MeasurementEvent(
                EventType.FREQUENCY_MEASURED, "M01", session.session_id,
                create_measurement(session, 42.0),
            ))

        # 正常关闭或明确中断均立即完成频率结算。
        await manager.handle_machine_close(interrupted=outcome == "interrupted")
        assert manager.frequency_adapter.active_session_id is None

    asyncio.run(receive_and_close())
    assert session.state == SessionState.FAILED
    assert session.final_frequency is None
    assert len(session.measurement_frequencies) == (0 if outcome == "empty" else 1)
    assert app.database.queue.empty()
    assert manager.current_session is None
    assert session.frequency_window_sealed
    assert session.frequency_state == FrequencyState.FAILED


def test_fifo_includes_queued_reading_before_close_and_rejects_late_reading(frequency_context):
    """验证队列中关闭前的数据先处理，关闭后的旧轮数据不再加入列表。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 已验证真实事件循环中的接收、关闭及迟到事件顺序
    """
    app, manager, session = frequency_context

    async def process_queued_events():
        """按接收顺序入队测量、关闭和迟到测量。

        Args:
            无外部参数。

        Returns:
            None  # 队列已排空，业务监听任务已停止
        """
        # 关闭前的测量先入队；即使尚未处理，也必须计入本轮。
        await app.publish_event(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", session.session_id,
            create_measurement(session, 42.0),
        ))
        await app.publish_event(MeasurementEvent(EventType.MACHINE_CLOSED, "M01"))
        await app.publish_event(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", session.session_id,
            create_measurement(session, 99.0),
        ))

        # 启动正式串行处理器并等待三条事件全部处理。
        listener = asyncio.create_task(manager.listen_events())
        try:
            await asyncio.wait_for(manager.queue.join(), 1)
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        assert [measurement.value_hz for measurement in session.measurement_frequencies] == [42.0]
        assert session.final_frequency.value_hz == 42.0

    asyncio.run(process_queued_events())


def test_next_session_is_not_changed_by_old_frequency_event(frequency_context):
    """验证旧轮封闭后的测量不会改写新周期或追加旧列表。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 已验证旧轮迟到数据与新轮活动身份隔离
    """
    app, manager, session = frequency_context

    async def close_then_receive_old_data():
        """关闭旧轮，登记新轮身份并交付旧轮事件。

        Args:
            无外部参数。

        Returns:
            None  # 新轮身份保持不变
        """
        await manager.handle_machine_close()
        manager.current_session = replace(
            session,
            session_id="next-session",
            capture_stop_time=None,
            state=SessionState.RUNNING,
        )
        manager.frequency_adapter.active_session_id = "next-session"
        await manager.handle_event(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", session.session_id,
            create_measurement(session, 42.0),
        ))
        assert session.measurement_frequencies == []
        assert manager.current_session.session_id == "next-session"
        assert manager.frequency_adapter.active_session_id == "next-session"

    asyncio.run(close_then_receive_old_data())


def test_listener_failure_raises_error(frequency_context):
    """验证频率监听异常交给后台任务入口，不发送故障恢复事件。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 原始异常已抛出，原周期未收到伪造事件
    """
    # 准备读取失败的频率适配器。
    app, manager, session = frequency_context
    adapter = manager.frequency_adapter
    adapter.publish_event = AsyncMock()
    adapter.listen_measurements = AsyncMock(side_effect=OSError("读取失败"))

    # 确认异常直接传播，原周期没有收到伪造测量或故障事件。
    with pytest.raises(OSError, match="读取失败"):
        asyncio.run(adapter.run())
    adapter.publish_event.assert_not_awaited()


def test_placeholder_listener_delivers_only_active_session(frequency_context):
    """验证联调监听过滤无效值、保留相同新读数，并在关闭后停止交付。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 已验证事件身份、有效值、跨轮归属和取消退出
    """
    app, manager, session = frequency_context
    adapter = manager.frequency_adapter
    adapter.configuration = replace(app.configuration, frequency_interval_ms=5)
    adapter.machine = replace(adapter.machine, simulated_frequencies_hz=(-1, float("nan"), 42, 42))

    async def receive_events():
        """运行正式监听并核对两个周期的测量事件。

        Args:
            无外部参数。

        Returns:
            None  # 监听已取消，事件已核对
        """
        # 直接接收正式适配器发布的事件，不替换监听函数。
        events = asyncio.Queue()
        adapter.publish_event = events.put
        listener = asyncio.create_task(adapter.run())
        try:
            first = await asyncio.wait_for(events.get(), 1)
            second = await asyncio.wait_for(events.get(), 1)
            assert first.event_type == second.event_type == EventType.FREQUENCY_MEASURED
            assert first.payload.value_hz == second.payload.value_hz == 42
            assert first.session_id == second.session_id == session.session_id

            # 清空活动周期后，监听继续运行但不交付频率。
            adapter.active_session_id = None
            await asyncio.sleep(0.03)
            assert events.empty()

            # 新周期接收的读数携带新的周期编号。
            adapter.active_session_id = "next-session"
            following = await asyncio.wait_for(events.get(), 1)
            assert following.session_id == "next-session"
            assert following.payload.session_id == "next-session"
            assert following.payload.frequency_source_id == session.frequency_source_id
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        assert listener.cancelled()

    asyncio.run(receive_events())
