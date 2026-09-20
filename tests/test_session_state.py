"""验证 Session 四态、机器启停隔离和失败不入库。"""

import asyncio
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_measurement_flow as flow_support

from enums import FrequencyState, OCRState, SessionState, EventType
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
        session.measurement_frequencies.append(create_measurement(session, 42))
        await manager.try_finalize(session)
        assert session.state == SessionState.RUNNING
        assert app.database.queue.empty()

        # 正常关闭不结束未完成的 OCR，结果齐全后只提交一次。
        await manager.handle_machine_close()
        if close_first:
            assert session.state == SessionState.RUNNING
            assert manager.current_session is session
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
            assert event.event_type == EventType.COMMIT_SUCCEEDED
            await manager.handle_event(event)
            manager.queue.task_done()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert session.state == SessionState.COMMITTED
        assert manager.current_session is None

    asyncio.run(process_measurement())
    with sqlite3.connect(app.configuration.database_path) as connection:
        records = connection.execute("SELECT session_id FROM measurements").fetchall()
        assert records == [(session.session_id,)]
        # 测量表只保存业务列，不保存整包内容、哈希或文字图片对应关系。
        columns = connection.execute("PRAGMA table_info(measurements)").fetchall()
        assert not {
            "payload_json", "payload_hash", "line_evidence_refs", "close_time",
        } & {column[1] for column in columns}
        assert "outcome" not in {column[1] for column in columns}
        assert "is_simulated" not in {column[1] for column in columns}




@pytest.mark.parametrize("end_event", [EventType.MACHINE_CLOSED, EventType.CYCLE_TIMEOUT])
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
        # 准备待处理状态和两个周期期限。
        session.ocr_state = OCRState.RUNNING
        frame = session.ocr_result
        manager.schedule_timeout(session, EventType.CYCLE_TIMEOUT, 60000)
        manager.schedule_timeout(session, EventType.OCR_TIMEOUT, 60000)
        deadlines = tuple(manager.deadline_tasks.values())

        # 整轮 OCR 失败后清理图片，保留活动编号和等待关闭的期限。
        await manager.handle_event(MeasurementEvent(
            EventType.OCR_FAILED, "M01", session.session_id, "MODEL_FAILED",
        ))
        assert session.state == SessionState.FAILED
        assert session.ocr_result is None
        assert not manager.recognition_task
        assert manager.current_session.session_id == session.session_id
        assert manager.current_session is session
        assert EventType.CYCLE_TIMEOUT in manager.deadline_tasks
        assert EventType.OCR_TIMEOUT not in manager.deadline_tasks

        # 重复启动和迟到图片不得恢复失败周期或创建新周期。
        await manager.handle_machine_start()
        await manager.handle_event(MeasurementEvent(
            EventType.OCR_COMPLETED, "M01", session.session_id, frame,
        ))
        assert manager.current_session is session
        assert not manager.recognition_task

        # 真实关闭或关闭超时释放活动身份，超时仍要求机器复位。
        await manager.handle_event(MeasurementEvent(
            end_event, "M01", session.session_id,
        ))
        assert manager.current_session is None
        assert manager.current_session is None
        assert not manager.deadline_tasks
        assert manager.waiting_cycle_reset == (end_event == EventType.CYCLE_TIMEOUT)
        await asyncio.gather(*deadlines, return_exceptions=True)
        if end_event == EventType.CYCLE_TIMEOUT:
            await manager.handle_event(MeasurementEvent(
                EventType.MACHINE_CLOSED, "M01", session.session_id,
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
        None  # 本轮已失败且本地运行库无待提交记录
    """
    app, manager, session = frequency_context
    session.measurement_frequencies.append(create_measurement(session, 42))

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
            app.database.measurement_repository.write_record = Mock(side_effect=error)

        # 正常关闭后尝试提交，入队成功时执行存储工作任务。
        await manager.handle_machine_close()
        if failure_mode in {"write", "conflict"}:
            worker = asyncio.create_task(app.database.run())
            try:
                await asyncio.wait_for(app.database.queue.join(), 1)
                event = manager.queue.get_nowait()
                await manager.handle_event(event)
                manager.queue.task_done()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            assert app.database.measurement_repository.write_record.call_count == 1
            assert not app.database.queued_records
        elif failure_mode == "queue":
            app.database.queue.get_nowait()
            app.database.queue.task_done()

        # 失败后再次检查不重新入队。
        assert session.state == SessionState.FAILED
        assert manager.current_session is None
        await manager.try_finalize(session)
        assert app.database.queue.empty()

    asyncio.run(submit_and_fail())
    with sqlite3.connect(app.configuration.database_path) as connection:
        records = connection.execute("SELECT COUNT(*) FROM measurements").fetchone()
        assert records == (0,)






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
        session = manager.current_session

        # 退出应用，确认未伪造关闭或保存异常记录。
        await app.stop()
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertIn("CYCLE_INTERRUPTED", session.errors)
        self.assertEqual(self.read_records(), [])
        self.assertIsNone(manager.current_session)
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
        await app.text_recognizer.processing_lock.acquire()
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.current_session
        await self.wait_for_state(lambda: bool(session.measurement_frequencies))
        await app.handle_close("M01")
        self.assertEqual(session.state, SessionState.RUNNING)

        # 等待退出期限，确认失败清理不创建测量记录。
        await app.stop()
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertIn("SHUTDOWN_TIMEOUT", session.errors)
        self.assertIsNone(session.ocr_result)
        self.assertIsNone(manager.current_session)
        self.assertEqual(self.read_records(), [])

    async def test_capture_failure_stops_entire_application(self):
        """验证相机编码故障停止整个应用，不再等待真实关闭或恢复。

        Args:
            无外部参数。

        Returns:
            None  # 故障已传播，全部设备关闭且没有异常测量入库
        """
        # 注入图片编码故障并启动测量。
        app = await self.start_app(capture_window_ms=100)
        manager = app.machine_managers["M01"]
        manager.camera.device.handle.encoding_error = 123
        await app.handle_start("M01")
        session = manager.current_session

        # 等待应用报告故障并完成自动退出。
        with self.assertRaisesRegex(RuntimeError, "BMP"):
            await asyncio.wait_for(app.wait_for_failure(), 2)
        await asyncio.wait_for(app.stop(), 2)
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertFalse(app.accepting_signals)
        self.assertTrue(app.camera_sdk.closed)
        self.assertFalse(app.worker_tasks)
        self.assertEqual(self.read_records(), [])
        self.assertTrue(all(manager.current_session is None for manager in app.machine_managers.values()))


def test_delayed_close_keeps_all_delivered_frames(frequency_context):
    """验证排队的 CLOSE 不再筛除已交付的尾帧。

    Args:
        frequency_context: 已建立的活动周期和独立存储。

    Returns:
        None  # OCR 收到本轮交付的全部帧
    """
    from mvs_sdk import CameraFrame
    from models import CaptureResult

    app, manager, session = frequency_context
    expected_result = session.ocr_result
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None
    manager.camera.device = SimpleNamespace(encode_image=Mock())
    app.text_recognizer.process_session_frames = Mock(return_value=expected_result)
    session.measurement_frequencies.append(create_measurement(session, 42))
    frames = tuple(
        CameraFrame("serial", number, 0, 0, timestamp, 2, 2, 0, 0, b"1234")
        for number, timestamp in ((1, 11), (2, 13))
    )

    async def close_then_deliver():
        """交付旧时间的关闭信号和随后到达的采集结果。

        Args:
            无外部参数。

        Returns:
            None  # 后台 OCR 已执行且保留全部交付帧
        """
        await manager.handle_event(MeasurementEvent(
            EventType.MACHINE_CLOSED, "M01", session.session_id, received_monotonic=12,
        ))
        await manager.handle_event(MeasurementEvent(
            EventType.CAPTURE_COMPLETED, "M01", session.session_id,
            CaptureResult(frames=frames),
        ))
        await asyncio.gather(*tuple((manager.recognition_task,)))
        assert app.text_recognizer.process_session_frames.call_args.args[3] == frames

    asyncio.run(close_then_deliver())
