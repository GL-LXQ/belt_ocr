"""验证整轮采集交付、共享 OCR 调度和退出资源顺序。"""

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock

import test_measurement_flow as flow_support
from enums import EventType, OCRState, SessionState
from models import MeasurementEvent


class MvsSessionTests(unittest.IsolatedAsyncioTestCase):
    """通过 SDK 替身运行正式采集与业务流程。"""

    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    async def test_capture_delivers_once_before_encoding(self):
        """验证整轮只交付一个事件且编码时已经停采。

        Args:
            无外部参数。

        Returns:
            None  # 交付顺序和全部帧数量已验证
        """
        app = await self.start_app(capture_window_ms=400)
        manager = app.machine_managers["M01"]
        publisher = AsyncMock(wraps=manager.camera.publish_event)
        manager.camera.publish_event = publisher
        original_encode = manager.camera.device.encode_image
        encoded_count = 0

        def encode_after_capture(frame):
            """检查相机停止后才执行编码。

            Args:
                frame: 本轮原始帧。

            Returns:
                (".bmp", image_data)  # BMP 扩展名及图片字节
            """
            nonlocal encoded_count
            assert not manager.camera.is_capturing
            encoded_count += 1
            return original_encode(frame)

        manager.camera.device.encode_image = encode_after_capture
        await app.handle_start("M01")
        session = manager.current_session
        await self.wait_for_state(lambda: session.ocr_state == OCRState.SUCCESS)
        events = [call.args[0] for call in publisher.call_args_list]
        self.assertEqual([event.event_type for event in events], [EventType.CAPTURE_COMPLETED])
        self.assertEqual(encoded_count, len(events[0].payload.frames))
        self.assertGreater(encoded_count, 5)
        self.assertEqual(session.capture_summary["retained_frame_count"], encoded_count)

    async def test_serial_processing_and_timeout_keep_lock_until_thread_finishes(self):
        """验证超时不提前释放模型锁，其他机器继续接收启停和频率。

        Args:
            无外部参数。

        Returns:
            None  # 超时结果隔离与共享串行执行已验证
        """
        app = await self.start_app()
        started = threading.Event()
        release = threading.Event()
        original_process = app.text_recognizer.process_session_frames
        processed_sessions = []

        def block_first_session(*arguments):
            """阻塞首轮并记录实际开始处理的周期。

            Args:
                arguments: 正式 OCR 主流程参数。

            Returns:
                OCRResult  # 正式主流程生成的测试结果
            """
            processed_sessions.append(arguments[0])
            if len(processed_sessions) == 1:
                started.set()
                assert release.wait(5)
            return original_process(*arguments)

        app.text_recognizer.process_session_frames = block_first_session
        try:
            await app.handle_start("M01")
            first_manager = app.machine_managers["M01"]
            first = first_manager.current_session
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await app.handle_start("M02")
            second_manager = app.machine_managers["M02"]
            second = second_manager.current_session
            await self.wait_for_state(lambda: second.ocr_state == OCRState.RUNNING)
            # 首轮超时后保留锁，第二轮可以正常关闭并等待处理。
            await app.publish_event(MeasurementEvent(EventType.OCR_TIMEOUT, "M01", first.session_id))
            await self.wait_for_state(lambda: first.state == SessionState.FAILED)
            await asyncio.wait_for(app.handle_close("M01"), 1)
            await asyncio.wait_for(app.handle_close("M02"), 1)
            self.assertEqual(processed_sessions, [first.session_id])
            self.assertTrue(app.text_recognizer.processing_lock.locked())
            self.assertTrue(second.measurement_frequencies)
            # 第三轮在锁上等待时失败，永远不调用模型。
            await app.handle_start("M03")
            third_manager = app.machine_managers["M03"]
            third = third_manager.current_session
            await self.wait_for_state(lambda: third.ocr_state == OCRState.RUNNING)
            await app.publish_event(MeasurementEvent(EventType.OCR_TIMEOUT, "M03", third.session_id))
            await self.wait_for_state(lambda: third.state == SessionState.FAILED)
            await app.handle_close("M03")
        finally:
            release.set()
        await app.wait_until_idle()
        self.assertEqual(processed_sessions, [first.session_id, second.session_id])
        self.assertEqual([record["session_id"] for record in self.read_records()], [second.session_id])
        self.assertFalse(first_manager.recognition_task)
        self.assertFalse(third_manager.recognition_task)

    async def test_shutdown_waits_for_encoder_before_closing_sdk(self):
        """验证重复取消时仍等待在途编码结束后才关闭 SDK。

        Args:
            无外部参数。

        Returns:
            None  # 锁和设备的释放顺序已验证
        """
        app = await self.start_app(shutdown_timeout_ms=100)
        manager = app.machine_managers["M01"]
        started = threading.Event()
        release = threading.Event()
        original_encode = manager.camera.device.encode_image

        def block_encoding(frame):
            """等待测试释放并确认编码期间设备保持打开。

            Args:
                frame: 原始帧。

            Returns:
                (".bmp", image_data)  # 图片编码结果
            """
            started.set()
            assert release.wait(5)
            assert not manager.camera.device.closed
            return original_encode(frame)

        manager.camera.device.encode_image = block_encoding
        await app.handle_start("M01")
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        shutdown = asyncio.create_task(app.stop())
        try:
            await asyncio.sleep(0.2)
            self.assertFalse(shutdown.done())
            self.assertFalse(app.camera_sdk.closed)
            self.assertTrue(app.text_recognizer.processing_lock.locked())
        finally:
            release.set()
        await asyncio.wait_for(shutdown, 2)
        self.assertTrue(app.camera_sdk.closed)
        self.assertFalse(app.text_recognizer.processing_lock.locked())
        self.assertEqual(self.read_records(), [])

    async def test_empty_capture_and_empty_filter_fail_without_saving(self):
        """验证无帧与无合格帧均立即结束且不保存图片。

        Args:
            无外部参数。

        Returns:
            None  # 两种空结果均失败且无测量记录
        """
        app = await self.start_app()
        app.machine_managers["M01"].camera.device.handle.return_no_data = True
        app.text_recognizer.filter_qualified_frames = lambda frames: ()
        await asyncio.gather(app.handle_start("M01"), app.handle_start("M02"))
        sessions = [manager.current_session for manager in list(app.machine_managers.values())[:2]]
        await self.wait_for_state(lambda: all(session.state == SessionState.FAILED for session in sessions))
        self.assertIn("CAPTURE_NO_FRAMES", sessions[0].errors)
        self.assertIn("OCR_NO_QUALIFIED_FRAMES", sessions[1].errors)
        await asyncio.gather(app.handle_close("M01"), app.handle_close("M02"))
        await app.wait_until_idle()
        self.assertEqual(self.read_records(), [])
        self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])
