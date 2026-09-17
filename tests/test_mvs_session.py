"""验证 MVS 相机与 Session 的完整接入。"""

import asyncio
import struct
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import camera
import test_measurement_flow as flow_support
from app import App


class MvsSessionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    async def test_frames_precede_sealing_and_statistics_are_saved(self):
        """验证真实适配器逐帧发布、封口和统计入库顺序。

        Args:
            无外部参数。

        Returns:
            None  # 断言完成
        """
        app = await self.start_app(capture_window_ms=180)
        adapter = app.machine_managers["M01"].camera
        events = []
        original_publish = adapter.publish_event

        async def record_event(event):
            """记录相机事件并送入实际业务队列。

            Args:
                event: 相机事件。

            Returns:
                None  # 事件已入队
            """
            events.append(event)
            await original_publish(event)

        adapter.publish_event = record_event
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)
        await app.handle_close("M01")
        await app.wait_until_idle()

        # 验证图片先交付，最后封口，结果保留采集统计和模拟标记。
        self.assertEqual([event.event_type for event in events], ["FrameSelected", "FrameSelected", "CaptureSealed"])
        self.assertTrue(all(event.session_id == session.session_id for event in events))
        self.assertEqual(events[0].payload.capture_id, session.capture_id)
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "COMPLETE")
        self.assertTrue(record["is_simulated"])
        self.assertEqual(record["capture_statistics"]["selected_frame_count"], 2)
        self.assertTrue(record["capture_statistics"]["camera_stopped"])
        self.assertGreater(record["capture_statistics"]["received_frame_count"], 0)

        # 检查证据的 BMP 文件头、尺寸和像素，确认未使用被 SDK 覆盖的内存。
        for evidence_path in record["evidence_refs"]:
            image_path = Path(evidence_path)
            image_data = image_path.read_bytes()
            self.assertEqual(image_path.suffix, ".bmp")
            self.assertEqual(image_data[:2], b"BM")
            self.assertEqual(struct.unpack_from("<ii", image_data, 18), (2, 2))
            self.assertEqual(struct.unpack_from("<I", image_data, 2)[0], len(image_data))
            self.assertEqual(image_data[54:60], b"\x30\x30\x30\x40\x40\x40")
            self.assertEqual(image_data[62:68], b"\x10\x10\x10\x20\x20\x20")

    async def test_bmp_encoding_failure_saves_review_without_evidence(self):
        """验证 SDK 图片编码失败时保存待复核记录且不生成无效证据。

        Args:
            无外部参数。

        Returns:
            None  # 断言完成
        """
        app = await self.start_app(capture_window_ms=180)
        manager = app.machine_managers["M01"]
        manager.camera.device.handle.encoding_error = 123
        with self.assertLogs("camera", level="ERROR"):
            await app.handle_start("M01")
            session = manager.sessions[manager.active_session_id]
            await self.wait_for_state(lambda: session.capture_sealed)
        await app.handle_close("M01")
        await app.wait_until_idle()

        # 失败信息进入统计，结果没有证据，也没有残留临时文件。
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("CAPTURE_FAILED", record["error_codes"])
        self.assertEqual(record["evidence_refs"], [])
        self.assertGreater(record["capture_statistics"]["failed_frame_count"], 0)
        self.assertIn("SaveImageEx3(BMP)", record["capture_statistics"]["processing_errors"][0])
        self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])
        self.assertEqual(list(self.output_directory.rglob("*.partial")), [])

    async def test_missing_sdk_rejects_measurement_without_creating_session(self):
        """验证缺失 SDK 时应用可启动但不创建正常测量。

        Args:
            无外部参数。

        Returns:
            None  # 断言完成
        """
        with patch("app.load_mvs_sdk", side_effect=OSError("SDK 不存在")):
            with self.assertLogs("app", level="ERROR"):
                app = await self.start_app()
        self.assertEqual(app.machine_managers["M01"].acceptance_state, "FAULT")
        with self.assertLogs("machine_manager", level="ERROR"):
            await app.handle_start("M01")
        self.assertEqual(app.machine_managers["M01"].sessions, {})
        await app.database.queue.join()
        self.assertEqual(self.read_records(), [])

    async def test_unconfigured_serial_disables_only_its_machine(self):
        """验证空序列号只禁用对应机器。

        Args:
            无外部参数。

        Returns:
            None  # 断言完成
        """
        app = await self.start_app()
        configuration = app.configuration
        await app.stop()
        machines = (replace(configuration.machines[0], camera_serial=""), *configuration.machines[1:])
        self.app = App(replace(configuration, machines=machines))
        with self.assertLogs("app", level="ERROR"):
            await self.app.start()
        self.assertEqual(self.app.machine_managers["M01"].acceptance_state, "FAULT")
        self.assertEqual(self.app.machine_managers["M02"].acceptance_state, "READY")

    async def test_next_session_starts_while_previous_evidence_is_saving(self):
        """验证旧轮保存阻塞时关闭仍能释放相机给下一轮。

        Args:
            无外部参数。

        Returns:
            None  # 断言完成
        """
        app = await self.start_app(capture_window_ms=2000)
        manager = app.machine_managers["M01"]
        saving = threading.Event()
        release_saving = threading.Event()
        original_save = camera.save_evidence_image
        first_session_id = None

        def hold_old_evidence(image_data, image_path):
            """只阻塞旧轮证据保存。

            Args:
                image_data: 图片字节。
                image_path: 图片文件路径。

            Returns:
                None  # 图片已保存
            """
            if image_path.parent.name == first_session_id:
                saving.set()
                if not release_saving.wait(5):
                    raise TimeoutError("测试未释放证据保存")
            original_save(image_data, image_path)

        with patch("camera.save_evidence_image", side_effect=hold_old_evidence):
            try:
                await app.handle_start("M01")
                first_session_id = manager.active_session_id
                first_session = manager.sessions[first_session_id]
                self.assertTrue(await asyncio.to_thread(saving.wait, 2))
                await asyncio.wait_for(app.handle_close("M01"), 1)
                await app.handle_start("M01")
                second_session = manager.sessions[manager.active_session_id]
                await self.wait_for_state(lambda: bool(second_session.selected_frames))

                # 旧轮还在保存，下一轮已经独立采集并收到自己的图片。
                self.assertFalse(first_session.capture_sealed)
                self.assertNotEqual(first_session.capture_id, second_session.capture_id)
                await manager.camera.seal_capture(first_session.capture_id)
                self.assertEqual(manager.active_session_id, second_session.session_id)
                await app.handle_close("M01")
            finally:
                release_saving.set()
            await app.wait_until_idle()
        self.assertEqual(len(self.read_records()), 2)

    async def test_shutdown_stops_capture_before_sdk_release(self):
        """验证退出时等待相机任务结束后再释放 SDK。

        Args:
            无外部参数。

        Returns:
            None  # 断言完成
        """
        app = await self.start_app(capture_window_ms=10000)
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.sessions[manager.active_session_id]
        task = manager.camera.windows[session.capture_id].task
        await app.stop()
        self.assertTrue(task.completed.is_set())
        self.assertFalse(manager.camera.windows)
        self.assertTrue(manager.camera.device.closed)
        self.assertTrue(app.camera_sdk.closed)
