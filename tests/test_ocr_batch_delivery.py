"""验证图片批次交付到 OCR 队列的阶段边界。"""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_measurement_flow as flow_support


class OCRBatchDeliveryTests(unittest.IsolatedAsyncioTestCase):
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
            None  # 完成批次归属、证据文件和阶段边界断言
        """
        # 启动假 SDK 支持的正式采集流程，准备十张图片。
        app = await self.start_app(capture_window_ms=350, max_frames_per_session=10)
        manager = app.machine_managers["M01"]
        manager.submit_ocr_frames = AsyncMock()
        await app.handle_start("M01")
        session = manager.sessions[manager.active_session_id]

        # 等待采集封口，从 OCR 队列读取满批和尾批。
        await self.wait_for_state(lambda: session.capture_sealed)
        first_batch = app.ocr.batch_queue.get_nowait()
        tail_batch = app.ocr.batch_queue.get_nowait()

        # 检查每个批次的机器、周期、图片数量和实际证据。
        self.assertEqual(len(first_batch.frames), 8)
        self.assertEqual(len(tail_batch.frames), 2)
        for batch in (first_batch, tail_batch):
            self.assertEqual(batch.machine_id, "M01")
            self.assertEqual(batch.session_id, session.session_id)
            for frame in batch.frames:
                self.assertEqual(frame.session_id, session.session_id)
                self.assertTrue(Path(frame.image_path).read_bytes().startswith(b"BM"))

        # 检查业务层没有登记图片和任务，也没有启动或重复提交旧识别流程。
        self.assertEqual(session.selected_frames, {})
        self.assertEqual(session.ocr_jobs, {})
        self.assertEqual(session.ocr_state, "WAITING")
        self.assertIsNone(session.ocr_result)
        self.assertTrue(app.ocr.batch_queue.empty())
        self.assertFalse(any(task.get_name() == "OCR" for task in app.worker_tasks))
        manager.submit_ocr_frames.assert_not_awaited()

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
        await self.wait_for_state(lambda: session.capture_sealed)
        self.assertEqual(app.ocr.batch_queue.qsize(), 1)
        self.assertEqual(len(app.ocr.batch_queue.get_nowait().frames), 8)
        self.assertEqual(session.ocr_state, "FAILED")
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
        app.ocr.accepting_jobs = False
        manager = app.machine_managers["M01"]

        # 采集一轮图片，等待批次交付和封口。
        await app.handle_start("M01")
        session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)

        # 检查批次未入队，并记录本轮拒收错误。
        self.assertTrue(app.ocr.batch_queue.empty())
        self.assertEqual(session.ocr_state, "FAILED")
        self.assertIn("OCR_BATCH_REJECTED", session.errors)
