"""验证单机单周期的忙时跳过、保存后重启和失败资源清理。"""

import asyncio
import threading
import unittest
from unittest.mock import patch

import test_measurement_flow as flow_support
from enums import EventType, OCRState, SessionState
from models import MeasurementEvent


class SingleCycleTests(unittest.IsolatedAsyncioTestCase):
    """使用正式业务流程和可控机器验证唯一当前周期。"""

    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    async def test_capture_and_closed_ocr_wait_skip_start_then_allow_next_cycle(self):
        """验证采集中和关闭后等待 OCR 时均跳过 START，完成后可重启。

        Args:
            无外部参数。

        Returns:
            None  # 两轮顺序提交，忙时启动未覆盖当前数据
        """
        app = await self.start_app(capture_window_ms=150)
        manager = app.machine_managers["1"]
        await app.text_recognizer.processing_lock.acquire()
        try:
            # 采集期间的 START 不创建第二个任务。
            await app.handle_start("1")
            session = manager.current_session
            capture = manager.camera.current_capture
            with self.assertLogs("machine_manager", level="WARNING"):
                await app.handle_start("1")
            self.assertIs(manager.current_session, session)
            self.assertIs(manager.camera.current_capture, capture)
            # 正常关闭后等待 OCR，周期继续占用机器。
            await self.wait_for_state(lambda: session.ocr_state == OCRState.RUNNING)
            await app.handle_close("1")
            with self.assertLogs("machine_manager", level="WARNING"):
                await app.handle_start("1")
            self.assertIs(manager.current_session, session)
            self.assertIsNone(manager.camera.current_capture)
            self.assertTrue(app.accepting_signals)
        finally:
            app.text_recognizer.processing_lock.release()
        # 保存完成后自动清空，下一轮可以正常启动和提交。
        await app.wait_until_idle(3)
        self.assertIsNone(manager.current_session)
        await app.handle_start("1")
        following_session = manager.current_session
        self.assertNotEqual(following_session.session_id, session.session_id)
        await self.wait_for_state(lambda: following_session.ocr_state == OCRState.SUCCESS)
        await app.handle_close("1")
        await app.wait_until_idle(3)
        self.assertEqual(len(self.read_records()), 2)

    async def test_storage_wait_keeps_current_cycle_without_blocking_other_machine(self):
        """验证写库未完成时本机跳过 START，其他机器仍可完成 OCR。

        Args:
            无外部参数。

        Returns:
            None  # 保存期间无跨轮重叠，其他机器可以独立测量
        """
        app = await self.start_app()
        manager = app.machine_managers["1"]
        saving = threading.Event()
        release = threading.Event()
        original_persist = app.database.persist_measurement

        def hold_storage(request):
            """暂停编号 1 机器的持久化。

            Args:
                request: 正常测量提交请求。

            Returns:
                None  # 释放等待后完成真实保存
            """
            if request.machine_id == "1":
                saving.set()
                assert release.wait(5)
            original_persist(request)

        with patch.object(app.database, "persist_measurement", side_effect=hold_storage):
            try:
                await app.handle_start("1")
                session = manager.current_session
                await self.wait_for_state(lambda: session.ocr_state == OCRState.SUCCESS)
                await app.handle_close("1")
                self.assertTrue(await asyncio.to_thread(saving.wait, 2))
                with self.assertLogs("machine_manager", level="WARNING"):
                    await app.handle_start("1")
                self.assertIs(manager.current_session, session)
                self.assertEqual(session.state, SessionState.WAITING_COMMIT_DB)
                # 另一台机器的 OCR 不等待本机数据库提交。
                await app.handle_start("2")
                second = app.machine_managers["2"].current_session
                await self.wait_for_state(lambda: second.ocr_state == OCRState.SUCCESS)
                await app.handle_close("2")
            finally:
                release.set()
            await app.wait_until_idle(3)
        self.assertIsNone(manager.current_session)
        self.assertEqual(len(self.read_records()), 2)

    async def test_failure_close_waits_for_actual_worker_release_before_restart(self):
        """验证失败且关闭后仍等在途线程结束，再允许下一轮启动。

        Args:
            无外部参数。

        Returns:
            None  # 失败周期不入库，线程释放后下一轮正常完成
        """
        app = await self.start_app()
        manager = app.machine_managers["1"]
        started = threading.Event()
        release = threading.Event()
        original_process = app.text_recognizer.process_session_frames

        def hold_recognition(*arguments):
            """暂停整轮 OCR 直到测试释放。

            Args:
                arguments: 周期身份、原始帧和编码接口。

            Returns:
                OCRResult  # 正常生成的测试识别结果
            """
            started.set()
            assert release.wait(5)
            return original_process(*arguments)

        with patch.object(app.text_recognizer, "process_session_frames", side_effect=hold_recognition):
            try:
                await app.handle_start("1")
                session = manager.current_session
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await app.publish_event(MeasurementEvent(EventType.OCR_TIMEOUT, "1", session.session_id))
                await self.wait_for_state(lambda: session.state == SessionState.FAILED)
                # 失败前后的 START 均不覆盖尚未释放的周期。
                await app.handle_start("1")
                self.assertIs(manager.current_session, session)
                await app.handle_close("1")
                await app.handle_start("1")
                self.assertIs(manager.current_session, session)
                self.assertTrue(app.text_recognizer.processing_lock.locked())
            finally:
                release.set()
            await app.wait_until_idle(3)
        self.assertIsNone(manager.recognition_task)
        self.assertIsNone(manager.camera.delivery_task)
        self.assertEqual(self.read_records(), [])
        # 清理后下一轮正常提交，旧结果不会回填到新周期。
        await app.handle_start("1")
        following = manager.current_session
        await app.publish_event(MeasurementEvent(EventType.OCR_FAILED, "1", session.session_id, "LATE"))
        await self.wait_for_state(lambda: following.ocr_state == OCRState.SUCCESS)
        await app.handle_close("1")
        await app.wait_until_idle(3)
        self.assertEqual([record["session_id"] for record in self.read_records()], [following.session_id])

    async def test_demo_saves_first_round_before_starting_second_round(self):
        """验证演示主流程等待第一轮保存后再启动第二轮。

        Args:
            无外部参数。

        Returns:
            None  # 三机第一轮和首机第二轮共保存四条记录
        """
        from main import run_measurement_cycles

        app = await self.start_app()
        await asyncio.wait_for(run_measurement_cycles(app), 4)
        records = self.read_records()
        self.assertEqual(len(records), 4)
        self.assertEqual(sum(record["machine_id"] == "1" for record in records), 2)
        self.assertTrue(all(manager.current_session is None for manager in app.machine_managers.values()))
