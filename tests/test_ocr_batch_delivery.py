"""验证图片批次交付到 OCR 队列的阶段边界。"""

import struct
import unittest

from enums import OCRState

import test_measurement_flow as flow_support


class RecognitionBatchDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """通过正式相机适配器验证批次接收，不执行 OCR。"""

    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state

    async def test_full_batch_and_tail_are_forwarded_without_frame_jobs(self) -> None:
        """验证满批和尾批按顺序入队，封口不重复提交。

        Args:
            无外部参数。

        Returns:
            None  # 完成批次归属、内存图片和阶段边界断言
        """
        # 启动假 SDK 支持的正式采集流程，准备十张图片。
        app = await self.start_app(capture_window_ms=350, max_frames_per_session=10)
        manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        session = manager.sessions[manager.active_session_id]

        # 等待采集封口，从 OCR 队列读取满批和尾批。
        await self.wait_for_state(lambda: session.is_capture_finished)
        first_batch = app.text_recognizer.batch_queue.get_nowait()
        tail_batch = app.text_recognizer.batch_queue.get_nowait()

        # 检查每个批次的机器、周期、图片数量和内存图片。
        self.assertEqual(len(first_batch.frames), 8)
        self.assertEqual(len(tail_batch.frames), 2)
        for batch in (first_batch, tail_batch):
            self.assertEqual(batch.machine_id, "M01")
            self.assertEqual(batch.session_id, session.session_id)
            for frame in batch.frames:
                self.assertEqual(frame.session_id, session.session_id)
                self.assertTrue(frame.image_data.startswith(b"BM"))
                self.assertEqual(struct.unpack_from("<ii", frame.image_data, 18), (2, 2))
                self.assertEqual(frame.image_data[54:60], b"000@@@")

        # 检查业务层持有图片但没有落盘，也没有启动或重复提交旧识别流程。
        self.assertEqual(len(session.images_for_final_selection), 10)
        self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])
        self.assertEqual(session.ocr_state, OCRState.WAITING)
        self.assertIsNone(session.ocr_result)
        self.assertTrue(app.text_recognizer.batch_queue.empty())
        self.assertFalse(any(task.get_name() == "OCR" for task in app.worker_tasks))

    async def test_full_queue_rejects_tail_and_preserves_first_batch(self) -> None:
        """验证队列满时拒收尾批，保留已入队图片并登记错误。

        Args:
            无外部参数。

        Returns:
            None  # 完成队列容量和拒收状态断言
        """
        # 将队列容量限制为一批，启动可产生满批和尾批的采集。
        app = await self.start_app(
            capture_window_ms=350,
            max_frames_per_session=10,
            ocr_queue_capacity=1,
        )
        manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        session = manager.sessions[manager.active_session_id]

        # 等待封口，检查拒收不会覆盖已入队批次。
        await self.wait_for_state(lambda: session.is_capture_finished)
        self.assertEqual(app.text_recognizer.batch_queue.qsize(), 1)
        self.assertEqual(len(app.text_recognizer.batch_queue.get_nowait().frames), 8)
        self.assertEqual(session.ocr_state, OCRState.WAITING)
        self.assertIn("OCR_BATCH_REJECTED", session.errors)

    async def test_stopped_receiver_rejects_batch_without_enqueueing(self) -> None:
        """验证停止接收后批次不进入 OCR 队列。

        Args:
            无外部参数。

        Returns:
            None  # 完成停止接收和错误登记断言
        """
        # 启动应用并停止 OCR 批次接收。
        app = await self.start_app(capture_window_ms=180)
        app.text_recognizer.accepting_batches = False
        manager = app.machine_managers["M01"]

        # 采集一轮图片，等待批次交付和封口。
        await app.handle_start("M01")
        session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: session.is_capture_finished)

        # 检查批次未入队，并记录本轮拒收错误。
        self.assertTrue(app.text_recognizer.batch_queue.empty())
        self.assertEqual(session.ocr_state, OCRState.WAITING)
        self.assertIn("OCR_BATCH_REJECTED", session.errors)

    async def test_close_keeps_images_until_shutdown(self) -> None:
        """验证正常关闭和新轮启动保留旧图，退出后清空内存和队列。

        Args:
            无外部参数。

        Returns:
            None  # 完成跨轮保留和退出释放断言
        """
        # 完成第一轮采集，保留一张原图用于核对跨轮内容。
        app = await self.start_app(capture_window_ms=180, shutdown_timeout_ms=100)
        manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        first_session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: first_session.is_capture_finished)
        first_frame = next(iter(first_session.images_for_final_selection.values()))
        await app.handle_close("M01")

        # 新轮独立采集，旧轮原图引用和内容保持不变。
        await app.handle_start("M01")
        next_session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: next_session.is_capture_finished)
        self.assertIs(first_session.images_for_final_selection[first_frame.frame_id], first_frame)
        self.assertNotIn(first_frame.frame_id, next_session.images_for_final_selection)
        self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])

        # 退出清理两个周期的内存引用和未消费批次，不产生图片文件。
        await app.stop()
        self.assertEqual(first_session.images_for_final_selection, {})
        self.assertEqual(next_session.images_for_final_selection, {})
        self.assertTrue(app.text_recognizer.batch_queue.empty())
        self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])
