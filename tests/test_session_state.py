"""验证 Session 四态、机器启停隔离和失败不入库。"""

import asyncio
import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_measurement_flow as flow_support

from enums import FrequencyState, OCRState, SessionState
from models import CapturedFrame, MeasurementEvent, OCRResult
from test_frequency_pipeline import create_measurement, frequency_context


@pytest.mark.parametrize("close_first", [True, False])
def test_success_waits_for_close_results_and_database(frequency_context, close_first):
    """验证关闭与 OCR 两种先后顺序均等待入库回调才完成。

    Args:
        frequency_context: 独立应用、机器处理器和活动周期。
        close_first: 是否先关闭再完成 OCR。

    Returns:
        None  # 状态顺序、单次提交和存储结果均已验证
    """
    app, manager, session = frequency_context

    async def process_measurement():
        """执行关闭、OCR 完成和数据库回调。

        Args:
            无外部参数。

        Returns:
            None  # 正常记录已经入库，Session 已移除
        """
        # 准备有效频率，按场景控制 OCR 完成时机。
        session.ocr_state = OCRState.WAITING if close_first else OCRState.SUCCESS
        session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))
        await manager.try_finalize(session)
        assert session.state == SessionState.RUNNING
        assert app.database.queue.empty()

        # 正常关闭不结束未完成的 OCR，结果齐全后只提交一次。
        await manager.close_measurement()
        if close_first:
            assert session.state == SessionState.RUNNING
            assert manager.active_session_id is None
            session.ocr_state = OCRState.SUCCESS
            await manager.try_finalize(session)
        assert session.state == SessionState.WAITING_COMMIT_DB
        await manager.try_finalize(session)
        assert app.database.queue.qsize() == 1

        # 执行真实 SQLite 写入，成功回调前保持等待入库状态。
        worker = asyncio.create_task(app.database.run())
        try:
            await asyncio.wait_for(app.database.queue.join(), 1)
            assert session.state == SessionState.WAITING_COMMIT_DB
            event = manager.queue.get_nowait()
            assert event.event_type == "CommitSucceeded"
            await manager.apply_event(event)
            manager.queue.task_done()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert session.state == SessionState.COMMITTED
        assert session.finished
        assert session.session_id not in manager.sessions

    asyncio.run(process_measurement())
    with sqlite3.connect(app.configuration.database_path) as connection:
        records = connection.execute(
            "SELECT outcome, payload_json FROM measurements"
        ).fetchall()
        assert len(records) == 1
        assert records[0][0] == "COMPLETE"
        payload = json.loads(records[0][1])
        assert "close_time" not in payload
        assert "selected_frames" not in payload
        # 检查入库内容已移除运行批次和配置快照，保留配置版本。
        assert "process_epoch" not in payload
        assert "configuration_snapshot" not in payload
        assert payload["configuration_version"] == app.configuration.configuration_version
        columns = connection.execute("PRAGMA table_info(measurements)").fetchall()
        assert "close_time" not in {column[1] for column in columns}
    assert app.recovery.pending_count() == 0


@pytest.mark.parametrize("readable", [True, False])
def test_evidence_validation_controls_commit(frequency_context, tmp_path, readable):
    """验证证据完成前保持运行，验证失败时不提交。

    Args:
        frequency_context: 独立应用、机器处理器和活动周期。
        tmp_path: pytest 临时目录。
        readable: 是否创建非空证据文件。

    Returns:
        None  # 证据事件已决定等待入库或失败清理
    """
    app, manager, session = frequency_context
    evidence_path = tmp_path / "evidence.bmp"
    if readable:
        evidence_path.write_bytes(b"BM-evidence")
    session.ocr_result = OCRResult(("MODEL",), (str(evidence_path),), ())
    session.evidence_verified = False
    session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))

    async def validate_and_finalize():
        """关闭本轮并交付实际文件验证结果。

        Args:
            无外部参数。

        Returns:
            None  # 验证期间无重复任务，失败时无待提交记录
        """
        # 启动证据验证，重复检查不得重复创建任务。
        await manager.close_measurement()
        assert session.state == SessionState.RUNNING
        assert session.evidence_validation_pending
        await manager.try_finalize(session)
        assert len(manager.background_tasks) == 1

        # 等待验证并将结果交付同一机器事件处理器。
        await asyncio.gather(*tuple(manager.background_tasks))
        event = manager.queue.get_nowait()
        await manager.apply_event(event)
        manager.queue.task_done()
        if readable:
            assert session.state == SessionState.WAITING_COMMIT_DB
            assert app.database.queue.qsize() == 1
        else:
            assert session.state == SessionState.FAILED
            assert session.session_id not in manager.sessions
            assert "EVIDENCE_UNAVAILABLE" in session.errors
            assert app.database.queue.empty()

    asyncio.run(validate_and_finalize())


@pytest.mark.parametrize("end_event", ["MachineClosed", "CycleTimeout"])
def test_failure_before_close_keeps_machine_identity(frequency_context, end_event):
    """验证关闭前失败清理资源，但保留机器身份直到关闭或超时。

    Args:
        frequency_context: 独立应用、机器处理器和活动周期。
        end_event: 正常关闭或等待关闭超时事件。

    Returns:
        None  # 失败不入库，关闭前不误受理下一轮，迟到图片已丢弃
    """
    app, manager, session = frequency_context

    async def fail_then_end():
        """交付 OCR 失败、迟到批次、重复启动和最终关闭事件。

        Args:
            无外部参数。

        Returns:
            None  # 本轮资源和期限任务均已清理
        """
        # 准备本轮原图、排队批次和两个期限任务。
        session.ocr_state = OCRState.WAITING
        frame = CapturedFrame(
            session_id=session.session_id,
            capture_id=session.capture_id,
            camera_id=session.camera_id,
            frame_id="frame-before-failure",
            captured_at=session.start_time,
            captured_monotonic=11,
            image_data=b"BM-test",
        )
        await manager.apply_event(MeasurementEvent(
            "FrameBatchSelected", "M01", session.session_id, (frame,),
        ))
        manager.schedule_timeout(session, "CycleTimeout", 60000)
        manager.schedule_timeout(session, "OCRTimeout", 60000)
        deadlines = tuple(manager.deadline_tasks.values())

        # 整轮 OCR 失败后清理图片，保留活动编号和等待关闭的期限。
        await manager.apply_event(MeasurementEvent(
            "OCRFailed", "M01", session.session_id, "MODEL_FAILED",
        ))
        assert session.state == SessionState.FAILED
        assert not session.images_for_final_selection
        assert app.text_recognizer.batch_queue.empty()
        assert manager.active_session_id == session.session_id
        assert session.session_id in manager.sessions
        assert (session.session_id, "CycleTimeout") in manager.deadline_tasks
        assert (session.session_id, "OCRTimeout") not in manager.deadline_tasks

        # 重复启动和迟到图片不得恢复失败周期或创建新周期。
        await manager.start_measurement()
        await manager.apply_event(MeasurementEvent(
            "FrameBatchSelected", "M01", session.session_id, (frame,),
        ))
        assert len(manager.sessions) == 1
        assert app.text_recognizer.batch_queue.empty()

        # 真实关闭或关闭超时释放活动身份，超时仍要求机器复位。
        await manager.apply_event(MeasurementEvent(
            end_event, "M01", session.session_id,
        ))
        assert manager.active_session_id is None
        assert session.session_id not in manager.sessions
        assert not manager.deadline_tasks
        assert manager.waiting_cycle_reset == (end_event == "CycleTimeout")
        await asyncio.gather(*deadlines, return_exceptions=True)
        if end_event == "CycleTimeout":
            await manager.apply_event(MeasurementEvent(
                "MachineClosed", "M01", session.session_id,
            ))
            assert not manager.waiting_cycle_reset
        assert app.database.queue.empty()

    asyncio.run(fail_then_end())


@pytest.mark.parametrize("failure_mode", ["write", "conflict", "queue", "submit"])
def test_database_failure_is_terminal_without_retry(frequency_context, failure_mode):
    """验证写入、冲突、队满和提交异常均失败清理且不自动补交。

    Args:
        frequency_context: 独立应用、机器处理器和活动周期。
        failure_mode: 写入失败、内容冲突、队列满或提交接口异常。

    Returns:
        None  # 本轮已失败且恢复库无待提交记录
    """
    app, manager, session = frequency_context
    session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))

    async def submit_and_fail():
        """注入存储失败并处理对应回调。

        Args:
            无外部参数。

        Returns:
            None  # 存储未重试，失败 Session 已清理
        """
        # 设置存储失败位置，保留配置中的多次重试值验证其不再生效。
        if failure_mode == "queue":
            app.database.queue = asyncio.Queue(maxsize=1)
            app.database.queue.put_nowait(object())
        elif failure_mode == "submit":
            app.database.submit = AsyncMock(side_effect=OSError("提交异常"))
        else:
            error = (
                ValueError("内容冲突")
                if failure_mode == "conflict"
                else OSError("磁盘错误")
            )
            app.database.write_record = Mock(side_effect=error)

        # 正常关闭后尝试提交，入队成功时执行存储工作任务。
        await manager.close_measurement()
        if failure_mode in {"write", "conflict"}:
            worker = asyncio.create_task(app.database.run())
            try:
                await asyncio.wait_for(app.database.queue.join(), 1)
                event = manager.queue.get_nowait()
                await manager.apply_event(event)
                manager.queue.task_done()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            assert app.database.write_record.call_count == 1
            assert not app.database.queued_records
        elif failure_mode == "queue":
            app.database.queue.get_nowait()
            app.database.queue.task_done()

        # 失败后再次检查不重新入队，也不保留冻结数据供补交。
        assert session.state == SessionState.FAILED
        assert session.session_id not in manager.sessions
        assert session.frozen_payload is None
        await manager.try_finalize(session)
        assert app.database.queue.empty()
        assert app.recovery.pending_count() == 0

    asyncio.run(submit_and_fail())
    with sqlite3.connect(app.configuration.database_path) as connection:
        records = connection.execute("SELECT COUNT(*) FROM measurements").fetchone()
        assert records == (0,)


@pytest.mark.parametrize("commit_success", [True, False])
def test_old_commit_does_not_change_new_active_session(
    frequency_context, commit_success,
):
    """验证旧轮提交成功或失败均不改变新轮的现场身份。

    Args:
        frequency_context: 独立应用、机器处理器和活动周期。
        commit_success: 旧轮提交是否成功。

    Returns:
        None  # 新轮活动身份、状态和频率接收均保持不变
    """
    app, manager, session = frequency_context

    async def overlap_sessions():
        """关闭旧轮，启动新轮，再交付旧轮数据库回调。

        Args:
            无外部参数。

        Returns:
            None  # 旧轮回调和迟到关闭均未影响新轮
        """
        # 关闭结果完整的旧轮，进入等待入库状态。
        session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))
        await manager.close_measurement()
        assert session.state == SessionState.WAITING_COMMIT_DB

        # 使用设备替身启动新轮，保留真实业务创建与期限逻辑。
        manager.camera.device = SimpleNamespace(
            closed=False, faulted=False, capture_lock=threading.Lock(),
        )
        manager.camera.start_capture = Mock()
        await manager.start_measurement()
        next_session = manager.sessions[manager.active_session_id]
        deadlines = tuple(manager.deadline_tasks.values())

        # 交付旧轮提交回调，再交付带旧编号的关闭信号。
        await manager.apply_event(MeasurementEvent(
            "CommitSucceeded" if commit_success else "CommitFailed",
            "M01",
            session.session_id,
            None if commit_success else {"error_code": "DATABASE_WRITE_FAILED"},
        ))
        await manager.apply_event(MeasurementEvent(
            "MachineClosed", "M01", session.session_id,
        ))
        assert manager.active_session_id == next_session.session_id
        assert manager.frequency_adapter.active_session_id == next_session.session_id
        assert next_session.state == SessionState.RUNNING
        assert session.session_id not in manager.sessions

        # 结束测试中新建的周期，撤销其期限任务。
        await manager.close_measurement(interrupted=True)
        await asyncio.gather(*deadlines, return_exceptions=True)

    asyncio.run(overlap_sessions())


def test_closed_session_remains_running_while_next_cycle_starts(frequency_context):
    """验证关闭后等待 OCR 的旧轮与新轮同时保持 RUNNING。

    Args:
        frequency_context: 独立应用、机器处理器和活动周期。

    Returns:
        None  # 旧轮失败清理不影响新轮运行
    """
    app, manager, session = frequency_context

    async def close_and_start():
        """关闭未完成 OCR 的旧轮并启动下一轮。

        Args:
            无外部参数。

        Returns:
            None  # 新旧 Session 身份和状态已经核对
        """
        # 保留旧轮等待 OCR，关闭时只结算有效频率。
        session.ocr_state = OCRState.WAITING
        session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))
        await manager.close_measurement()
        assert session.state == SessionState.RUNNING
        assert session.frequency_state == FrequencyState.SUCCESS

        # 启动新轮，旧轮的迟到失败只清理旧轮。
        manager.camera.device = SimpleNamespace(
            closed=False, faulted=False, capture_lock=threading.Lock(),
        )
        manager.camera.start_capture = Mock()
        await manager.start_measurement()
        next_session = manager.sessions[manager.active_session_id]
        deadlines = tuple(manager.deadline_tasks.values())
        await manager.apply_event(MeasurementEvent(
            "OCRFailed", "M01", session.session_id, "MODEL_FAILED",
        ))
        assert session.state == SessionState.FAILED
        assert next_session.state == SessionState.RUNNING
        assert manager.active_session_id == next_session.session_id
        assert app.database.queue.empty()

        # 中断测试的新轮并等待期限任务取消完成。
        await manager.close_measurement(interrupted=True)
        await asyncio.gather(*deadlines, return_exceptions=True)

    asyncio.run(close_and_start())


class SessionShutdownTests(unittest.IsolatedAsyncioTestCase):
    """使用相机 SDK 替身验证应用退出和失败后的现场复位。"""

    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    async def test_shutdown_interrupts_active_session_without_saving(self):
        """验证退出中断活动测量，不保存异常记录并释放相机。

        Args:
            无外部参数。

        Returns:
            None  # Session 已失败，相机和队列均已释放
        """
        # 启动真实业务流程，保留活动周期对象。
        app = await self.start_app(capture_window_ms=10000)
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.sessions[manager.active_session_id]

        # 退出应用，确认未伪造关闭或保存异常记录。
        await app.stop()
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertIn("CYCLE_INTERRUPTED", session.errors)
        self.assertEqual(self.read_records(), [])
        self.assertFalse(manager.sessions)
        self.assertFalse(app.database.queued_records)
        self.assertTrue(app.database.queue.empty())
        self.assertTrue(app.camera_sdk.closed)

    async def test_shutdown_cleans_closed_session_after_deadline(self):
        """验证退出期限到达后清理等待 OCR 的已关闭周期。

        Args:
            无外部参数。

        Returns:
            None  # 超期 Session 已失败且无图片或待提交记录残留
        """
        # 正常关闭本轮，保留等待占位 OCR 的运行状态。
        app = await self.start_app(shutdown_timeout_ms=100)
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: bool(session.measurement_frequencies))
        await app.handle_close("M01")
        self.assertEqual(session.state, SessionState.RUNNING)

        # 等待退出期限，确认失败清理不创建测量记录。
        await app.stop()
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertIn("SHUTDOWN_TIMEOUT", session.errors)
        self.assertFalse(session.images_for_final_selection)
        self.assertFalse(manager.sessions)
        self.assertEqual(self.read_records(), [])

    async def test_capture_failure_waits_for_close_before_next_start(self):
        """验证采集失败只清理本轮，真实关闭后才允许下一次启动。

        Args:
            无外部参数。

        Returns:
            None  # 失败记录未入库，机器活动身份由真实关闭释放
        """
        # 注入图片编码失败，等待本轮进入失败状态。
        app = await self.start_app(capture_window_ms=100)
        manager = app.machine_managers["M01"]
        manager.camera.device.handle.encoding_error = 123
        await app.handle_start("M01")
        session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: session.state == SessionState.FAILED)
        self.assertEqual(manager.active_session_id, session.session_id)

        # 失败期间重复启动不创建新周期，正常关闭后清除活动身份。
        await app.handle_start("M01")
        self.assertEqual(len(manager.sessions), 1)
        await app.handle_close("M01")
        await app.wait_until_idle()
        self.assertIsNone(manager.active_session_id)
        self.assertEqual(self.read_records(), [])

        # 恢复编码接口并受理下一轮。
        manager.camera.device.handle.encoding_error = 0
        await app.handle_start("M01")
        self.assertIsNotNone(manager.active_session_id)
        self.assertNotEqual(manager.active_session_id, session.session_id)
