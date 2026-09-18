"""验证频率黑盒收尾、按时间结算和 SQLite 明细存储。"""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import App
from configuration import load_configuration
from models import BeltSession, FrequencyMeasurement, MeasurementEvent


@pytest.fixture
def frequency_context(tmp_path):
    """准备独立数据库、业务处理器和等待频率收尾的周期。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        (
            app,  # 未启动设备的应用及独立数据库
            manager,  # M01 的串行业务处理器
            session,  # 图像已收尾、OCR 已失败的测试周期
        )
    """
    # 创建独立存储配置，启用较短的频率收尾期限。
    configuration = load_configuration(Path(__file__).resolve().parents[1] / "config.example.json")
    configuration = replace(
        configuration,
        database_path=tmp_path / "measurements.sqlite3",
        recovery_database_path=tmp_path / "recovery.sqlite3",
        evidence_directory=tmp_path / "evidence",
        frequency_drain_timeout_ms=30,
    )
    app = App(configuration)
    app.recovery.initialize()
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
        start_boundary=10,
        capture_sealed=True,
        ocr_state="FAILED",
        configuration_snapshot={"configuration_version": "test-frequency"},
    )
    manager.sessions[session.session_id] = session
    try:
        yield app, manager, session
    finally:
        app.recovery.close()


def create_measurement(session, sequence, measured_time, value):
    """创建具有明确周期身份和统一时钟的设备测试测量。

    Args:
        session: 测量所属周期。
        sequence: 唯一测量序号。
        measured_time: 统一时钟下的测量秒数。
        value: 频率值，单位 Hz。

    Returns:
        FrequencyMeasurement(
            session_id="frequency-session",  # 所属周期
            frequency_source_id="FREQ01",  # 仪器来源
            measurement_id="reading-1",  # 测量身份
            source_sequence=1,  # 来源序号
            value_hz=42.0,  # 频率值
            measured_at="2026-09-18T00:00:11+00:00",  # 测量时间
            measured_monotonic=11,  # 统一时钟下的测量时间
            received_at="2026-09-18T00:00:25+00:00",  # 延迟接收时间
        )
    """
    return FrequencyMeasurement(
        session_id=session.session_id,
        frequency_source_id=session.frequency_source_id,
        measurement_id=f"reading-{sequence}",
        source_sequence=sequence,
        value_hz=value,
        measured_at=f"2026-09-18T00:00:{int(measured_time):02}+00:00",
        measured_monotonic=measured_time,
        received_at="2026-09-18T00:00:25+00:00",
    )


def test_delayed_measurements_are_sorted_and_saved_together(frequency_context):
    """验证 OCR 失败不会提前冻结频率，并将时间顺序明细与最终值一起入库。

    Args:
        frequency_context: 应用、处理器和测量周期。

    Returns:
        None  # 已验证晚到数据、跨轮隔离、时间排序和幂等写入
    """
    app, manager, session = frequency_context

    async def complete_frequency():
        """交付乱序测量、关闭后测量和成功封口事件。

        Args:
            无外部参数。

        Returns:
            None  # 已生成冻结提交请求
        """
        # 接收两个数值相同、身份不同的测量。
        for measurement in (
            create_measurement(session, 10, 15, 42.0),
            create_measurement(session, 11, 11, 42.0),
        ):
            await manager.apply_event(MeasurementEvent(
                "FrequencyMeasured", "M01", session.session_id, measurement,
            ))
        assert len(session.measurement_frequencies) == 2

        # 关闭后仍等待频率，OCR 超时也不能提前冻结本轮记录。
        session.cycle_state = "CLOSED"
        session.close_boundary = 20
        session.close_time = "2026-09-18T00:00:20+00:00"
        session.ocr_state = "TIMED_OUT"
        await manager.try_finalize(session)
        assert session.frozen_payload is None

        # 新周期打开时，旧轮晚到数据仍按原 Session 结算。
        manager.active_session_id = "next-session"
        await manager.apply_event(MeasurementEvent(
            "FrequencyMeasured", "M01", session.session_id,
            create_measurement(session, 2, 19, 43.0),
        ))
        await manager.apply_event(MeasurementEvent(
            "FrequencyWindowSealed", "M01", session.session_id,
        ))
        assert manager.active_session_id == "next-session"
        assert session.final_frequency.measurement_id == "reading-2"

    asyncio.run(complete_frequency())

    # 写入同一冻结请求两次，检查两个数据库字段与冻结内容一致。
    request = app.database.queue.get_nowait()
    app.database.write_record(request)
    app.database.write_record(request)
    app.database.queue.task_done()
    with sqlite3.connect(app.configuration.database_path) as connection:
        rows = connection.execute(
            "SELECT measurement_frequencies, final_frequency_hz, payload_json FROM measurements"
        ).fetchall()
    assert len(rows) == 1
    frequencies = json.loads(rows[0][0])
    assert [measurement["measured_monotonic"] for measurement in frequencies] == [11, 15, 19]
    assert rows[0][1] == frequencies[-1]["value_hz"] == 43.0
    assert json.loads(rows[0][2])["measurement_frequencies"] == frequencies


@pytest.mark.parametrize("outcome", ["empty", "timeout", "failure"])
def test_frequency_terminal_outcomes_preserve_partial_data(frequency_context, outcome):
    """验证空测量、收尾超时和设备失败不产生最终频率。

    Args:
        frequency_context: 应用、处理器和周期。
        outcome: 黑盒收尾的终态场景。

    Returns:
        None  # 明细保留，最终频率为空，异常记录已冻结
    """
    app, manager, session = frequency_context
    session.cycle_state = "CLOSED"
    session.close_boundary = 20
    adapter = manager.frequency_adapter
    adapter.publish_event = manager.apply_event

    async def drain_measurements(window):
        """交付一条已确认测量，再触发指定的设备收尾结果。

        Args:
            window: 本轮已关闭窗口。

        Returns:
            None  # 空场景正常完成，其他场景超时或抛出异常
        """
        if outcome == "empty":
            return
        # 失败前交付已经确定属于本轮的测量。
        await adapter.publish_event(MeasurementEvent(
            "FrequencyMeasured", "M01", window.session_id,
            create_measurement(session, 1, 12, 42.0),
        ))
        if outcome == "failure":
            raise RuntimeError("设备输出中断")
        await asyncio.Future()

    async def close_window():
        """关闭设备窗口并等待有限收尾主流程。

        Args:
            无外部参数。

        Returns:
            None  # 收尾任务已结束，本轮窗口已移除
        """
        adapter.drain_measurements = drain_measurements
        adapter.open_window(session.session_id, 10)
        adapter.seal_window(session.session_id, 20)
        await asyncio.gather(*tuple(adapter.tasks))
        assert session.session_id not in adapter.windows

    asyncio.run(close_window())
    payload = json.loads(session.frozen_payload)
    assert payload["final_frequency_hz"] is None
    assert len(payload["measurement_frequencies"]) == (0 if outcome == "empty" else 1)
    assert payload["outcome"] == "REVIEW_REQUIRED"
    assert session.frequency_window_sealed
    assert session.frequency_state == "FINAL_INVALID"


def test_unimplemented_blackbox_does_not_report_success(frequency_context):
    """验证黑盒留空时只报告故障，不生成测量或成功封口。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 已验证黑盒未实现时的明确故障事件
    """
    app, manager, session = frequency_context
    adapter = manager.frequency_adapter
    adapter.publish_event = AsyncMock()

    async def run_interfaces():
        """依次调用留空监听和收尾接口。

        Args:
            无外部参数。

        Returns:
            None  # 接口故障已交付
        """
        await adapter.run()
        adapter.open_window(session.session_id, 10)
        adapter.seal_window(session.session_id, 20)
        await asyncio.gather(*tuple(adapter.tasks))

    asyncio.run(run_interfaces())
    events = [call.args[0] for call in adapter.publish_event.call_args_list]
    assert [event.event_type for event in events] == ["DeviceFault", "FrequencyFailed"]


def test_old_database_migration_preserves_frozen_record(frequency_context):
    """验证旧库回填频率明细，同时保留历史最终值及原始提交哈希。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 已验证重复初始化和历史冻结内容保持不变
    """
    app, manager, session = frequency_context
    historical_payload = json.dumps({
        "frequency_candidates": [
            {"measured_monotonic": 15, "source_sequence": 1, "value_hz": 42},
            {"measured_monotonic": 11, "source_sequence": 2, "value_hz": 41},
        ],
    })
    # 构造上一版本表结构和已有历史记录。
    with sqlite3.connect(app.configuration.database_path) as connection:
        connection.execute("ALTER TABLE measurements DROP COLUMN measurement_frequencies")
        connection.execute(
            "INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-session", "M01", "start", "close", "finish", "[]", 42, "old-reading",
             "[]", "REVIEW_REQUIRED", "[]", 1, historical_payload, "original-hash"),
        )

    # 重复初始化不重复迁移，原始 JSON 和哈希不被改写。
    app.database.initialize()
    app.database.initialize()
    with sqlite3.connect(app.configuration.database_path) as connection:
        row = connection.execute(
            "SELECT measurement_frequencies, final_frequency_hz, payload_json, payload_hash "
            "FROM measurements WHERE session_id = 'old-session'"
        ).fetchone()
    assert [measurement["value_hz"] for measurement in json.loads(row[0])] == [41, 42]
    assert row[1:] == (42, historical_payload, "original-hash")


def test_old_window_finishes_without_releasing_new_window(frequency_context):
    """验证旧窗口等待期间可开启新窗口，收尾只释放旧窗口。

    Args:
        frequency_context: 应用、处理器和周期。

    Returns:
        None  # 已验证两个窗口边界、封口归属及新窗口继续活动
    """
    app, manager, session = frequency_context
    adapter = manager.frequency_adapter
    adapter.publish_event = AsyncMock()

    async def complete_old_window():
        """暂停旧轮收尾，在新轮打开后完成旧轮。

        Args:
            无外部参数。

        Returns:
            None  # 旧轮已封口，新轮仍处于活动状态
        """
        release_delivery = asyncio.Event()

        async def wait_for_delivery(window):
            """等待测试放行在途数据交付。

            Args:
                window: 被关闭的旧周期窗口。

            Returns:
                None  # 旧轮数据交付已完成
            """
            assert window.start_boundary == 10
            assert window.close_boundary == 20
            await release_delivery.wait()

        # 关闭旧轮后立即打开新轮，两个窗口同时存在。
        adapter.drain_measurements = wait_for_delivery
        adapter.open_window(session.session_id, 10)
        adapter.seal_window(session.session_id, 20)
        adapter.open_window("next-session", 21)
        assert len(adapter.windows) == 2

        # 旧轮完成时只删除旧窗口，封口事件保留原周期身份。
        release_delivery.set()
        await asyncio.gather(*tuple(adapter.tasks))
        assert adapter.active_window.session_id == "next-session"
        assert set(adapter.windows) == {"next-session"}
        event = adapter.publish_event.call_args.args[0]
        assert event.event_type == "FrequencyWindowSealed"
        assert event.session_id == session.session_id

    asyncio.run(complete_old_window())
