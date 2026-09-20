"""验证整轮采集交付、共享 OCR 调度和退出资源顺序。"""

import asyncio
import ctypes
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import test_measurement_flow as flow_support
from enums import EventType, OCRState, SessionState
from models import MeasurementEvent
from mvs_sdk import MvsError
from fake_mvs import FakeCameraHandle


class MvsSessionTests(unittest.IsolatedAsyncioTestCase):
    """通过 SDK 替身运行正式采集与业务流程。"""

    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    async def test_three_simultaneous_cameras_keep_image_contents_isolated(self):
        """验证三机并发取帧、OCR 和落盘图片的内容及周期归属。

        Args:
            无外部参数。

        Returns:
            None  # 三台相机的原始帧、OCR 图片和落盘图片均未混用
        """
        # 为三台相机设置不同灰度值，并同步第一帧读取线程。
        app = await self.start_app(capture_window_ms=400)
        capture_barrier = threading.Barrier(3)
        pixel_values = {"M01": 32, "M02": 96, "M03": 160}
        handle_pixels = {}
        publishers = {}
        for machine_id, manager in app.machine_managers.items():
            handle_pixels[manager.camera.device.handle] = pixel_values[machine_id]
            publisher = AsyncMock(wraps=manager.camera.publish_event)
            manager.camera.publish_event = publisher
            publishers[machine_id] = publisher
        original_read = FakeCameraHandle.MV_CC_GetImageBuffer

        def read_distinct_camera_image(handle, frame, timeout_ms):
            """同步三机首帧读取并向各自 SDK 缓存写入独有像素。

            Args:
                handle: 当前相机的测试 SDK 句柄。
                frame: 接收 SDK 缓存地址及元数据的对象。
                timeout_ms: 单次读取等待毫秒数。

            Returns:
                0  # 成功返回独有像素，失败时返回原 SDK 状态码
            """
            # 等待三条采集线程同时进入首帧读取。
            if handle.frame_number == 0:
                capture_barrier.wait(timeout=3)
            return_code = original_read(handle, frame, timeout_ms)
            # 在正式封装复制缓存前写入相机专属像素。
            if return_code == 0:
                ctypes.memset(frame.pBufAddr, handle_pixels[handle], 4)
            return return_code

        # 并发启动三机，记录进入 OCR 的全部图片并等待处理成功。
        frame_filter = Mock(wraps=app.text_recognizer.filter_qualified_frames)
        app.text_recognizer.filter_qualified_frames = frame_filter
        with patch.object(
            FakeCameraHandle, "MV_CC_GetImageBuffer", read_distinct_camera_image
        ):
            await asyncio.gather(*(
                app.handle_start(machine_id) for machine_id in app.machine_managers
            ))
            sessions = {
                machine_id: manager.current_session
                for machine_id, manager in app.machine_managers.items()
            }
            await self.wait_for_state(lambda: all(
                session.ocr_state == OCRState.SUCCESS for session in sessions.values()
            ))

        # 核对整轮交付的机器、周期、序列号及每一帧的实际像素。
        self.assertEqual(len({session.session_id for session in sessions.values()}), 3)
        for machine_id, publisher in publishers.items():
            publisher.assert_awaited_once()
            event = publisher.call_args.args[0]
            session = sessions[machine_id]
            self.assertEqual(event.machine_id, machine_id)
            self.assertEqual(event.session_id, session.session_id)
            self.assertGreater(len(event.payload.frames), 1)
            device = app.machine_managers[machine_id].camera.device
            for frame in event.payload.frames:
                self.assertEqual(frame.camera_serial, device.serial)
                self.assertEqual(frame.data, bytes([pixel_values[machine_id]]) * 4)

        # 检查 OCR 全部输入的周期身份和 BMP 像素内容。
        self.assertEqual(frame_filter.call_count, 3)
        sessions_by_id = {session.session_id: session for session in sessions.values()}
        observed_sessions = set()
        expected_images = {}
        for invocation in frame_filter.call_args_list:
            frames = invocation.args[0]
            session = sessions_by_id[frames[0].session_id]
            observed_sessions.add(session.session_id)
            pixel_value = pixel_values[session.machine_id]
            expected_pixels = (bytes([pixel_value]) * 6 + b"\0\0") * 2
            for frame in frames:
                self.assertEqual(frame.session_id, session.session_id)
                self.assertEqual(frame.camera_id, session.camera_id)
                self.assertEqual(frame.capture_id, session.capture_id)
                pixel_offset = int.from_bytes(frame.image_data[10:14], "little")
                self.assertEqual(frame.image_data[pixel_offset:], expected_pixels)
            expected_images[session.machine_id] = frames[0].image_data
        self.assertEqual(observed_sessions, set(sessions_by_id))

        # 同时关闭三机，检查数据库图片引用和落盘字节的归属。
        await asyncio.gather(*(
            app.handle_close(machine_id) for machine_id in app.machine_managers
        ))
        await app.wait_until_idle(3)
        records = self.read_records()
        self.assertEqual(len(records), 3)
        self.assertEqual({record["machine_id"] for record in records}, set(sessions))
        for record in records:
            session = sessions[record["machine_id"]]
            self.assertEqual(record["session_id"], session.session_id)
            self.assertEqual(len(record["evidence_refs"]), 1)
            image_path = Path(record["evidence_refs"][0])
            self.assertEqual(image_path.parent.name, session.session_id)
            self.assertEqual(image_path.parent.parent.name, session.machine_id)
            self.assertEqual(image_path.read_bytes(), expected_images[session.machine_id])

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
                b"BM..."  # 完整 BMP 文件字节，示例省略图片内容
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
                b"BM..."  # 完整 BMP 文件字节，示例省略图片内容
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

    async def test_encoding_failure_after_timeout_stops_application(self):
        """验证取消后的编码故障只记录一次，并停止应用释放设备。

        Args:
            无外部参数。

        Returns:
            None  # 原始故障已传播，设备关闭且没有生成测量记录
        """
        # 建立可控编码线程和设备故障观察入口。
        app = await self.start_app()
        manager = app.machine_managers["M01"]
        encoding_started = threading.Event()
        release_encoding = threading.Event()
        encoding_failure = MvsError("测试编码设备故障")
        failure_reporter = Mock(wraps=manager.camera.report_failure)
        manager.camera.report_failure = failure_reporter

        def fail_encoding_after_release(frame):
            """等待测试释放后抛出编码设备故障。

            Args:
                frame: 本轮待编码的原始帧。

            Returns:
                无返回值  # 等待结束后抛出 MvsError
            """
            encoding_started.set()
            assert release_encoding.wait(5)
            assert not manager.camera.device.closed
            raise encoding_failure

        manager.camera.device.encode_image = fail_encoding_after_release
        with self.assertLogs(level="ERROR") as captured_logs:
            try:
                # 等待编码开始，注入 OCR 超时并确认线程仍占用设备。
                await app.handle_start("M01")
                session = manager.current_session
                self.assertTrue(await asyncio.to_thread(encoding_started.wait, 2))
                await app.publish_event(MeasurementEvent(EventType.OCR_TIMEOUT, "M01", session.session_id))
                await self.wait_for_state(lambda: session.state == SessionState.FAILED)
                self.assertFalse(manager.recognition_task.done())
                self.assertTrue(app.text_recognizer.processing_lock.locked())
                self.assertFalse(manager.camera.device.closed)

                # 取消中的编码操作报错后，等待自动故障退出。
                release_encoding.set()
                await asyncio.wait_for(app.failure_event.wait(), 2)
                self.assertFalse(app.accepting_signals)
                await asyncio.wait_for(app.stop(), 2)
            finally:
                release_encoding.set()

        # 核对异常身份、唯一日志、资源释放和测量结果。
        self.assertIs(app.failure, encoding_failure)
        failure_reporter.assert_called_once_with(encoding_failure)
        failure_logs = [record for record in captured_logs.records if record.exc_info]
        self.assertEqual(len(failure_logs), 1)
        self.assertIs(failure_logs[0].exc_info[1], encoding_failure)
        self.assertTrue(manager.camera.device.closed)
        self.assertTrue(app.camera_sdk.closed)
        self.assertFalse(app.text_recognizer.processing_lock.locked())
        self.assertIsNone(manager.recognition_task)
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
