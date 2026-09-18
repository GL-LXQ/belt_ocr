"""验证整批推理、结果归属和批次异常处理。"""

import asyncio
import threading
import unittest
from unittest.mock import Mock

import test_measurement_flow as flow_support


class TextRecognitionTests(unittest.IsolatedAsyncioTestCase):
    """使用模型替身验证批次消费到 Session 结果保存。"""

    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state

    async def test_batch_inference_preserves_machine_frame_and_raw_results(self) -> None:
        """验证整批调用在线程中执行，原始结果返回所属机器和周期。

        Args:
            无外部参数。

        Returns:
            None  # 完成批量调用、结果结构和归属断言
        """
        # 为两台机器各准备一批两张图片，等待图片全部入队。
        app = await self.start_app(capture_window_ms=180)
        for machine_id in ("M01", "M02"):
            await app.handle_start(machine_id)
        sessions = [
            next(iter(app.machine_managers[machine_id].sessions.values()))
            for machine_id in ("M01", "M02")
        ]
        await self.wait_for_state(lambda: all(session.capture_sealed for session in sessions))

        # 准备包含重复行和空图片结果的模型替身。
        blocks = [{
            "bbox": [1, 2, 30, 40],
            "lines": [
                {
                    "text": "003",
                    "bbox": [1, 2, 10, 12],
                    "confidence": 0.4,
                },
                {
                    "text": "003",
                    "bbox": [1, 13, 10, 23],
                    "confidence": 0.99,
                },
            ],
        }]
        event_loop_thread = threading.get_ident()
        inference_threads = []

        def recognize_images(images: list[bytes]) -> list[dict]:
            """记录推理线程并返回本批两张图片的测试结果。

            Args:
                images: 当前批次的有序内存 BMP 字节。

            Returns:
                [
                    {"blocks": blocks},  # 第一张图片的文字块，含坐标和原始文字行
                    {"blocks": []},  # 第二张图片没有文字
                ]
            """
            # 记录执行线程，确认模型每次接收两张图片。
            inference_threads.append(threading.get_ident())
            self.assertEqual(len(images), 2)

            # 返回保留原始行信息的结果和无文字结果。
            return [
                {"blocks": blocks},
                {"blocks": []},
            ]

        # 手动启动消费者，等待两批结果进入各自机器的事件队列。
        recognizer = app.text_recognizer
        recognizer.recognize_batch = Mock(side_effect=recognize_images)
        listener = asyncio.create_task(recognizer.listen_and_recognize_batches(app.publish_event))
        try:
            await asyncio.wait_for(recognizer.batch_queue.join(), 3)
            for machine_id in ("M01", "M02"):
                await app.machine_managers[machine_id].queue.join()

            # 检查每批只调用一次模型，所有推理均在业务线程之外执行。
            self.assertEqual(recognizer.recognize_batch.call_count, 2)
            self.assertTrue(all(thread_id != event_loop_thread for thread_id in inference_threads))

            # 检查结果归属、图片顺序、重复文字及空文字结果均被保留。
            for session in sessions:
                results = session.recognition_results
                self.assertEqual(len(results), 2)
                self.assertIs(results[0]["blocks"], blocks)
                self.assertEqual(results[1]["blocks"], [])
                self.assertNotEqual(results[0]["frame_id"], results[1]["frame_id"])
                self.assertTrue(all(
                    result["frame_id"] in session.memory_frames for result in results
                ))
                self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])
                self.assertIn(
                    [session.memory_frames[result["frame_id"]].image_data for result in results],
                    [call.args[0] for call in recognizer.recognize_batch.call_args_list],
                )
                self.assertEqual(session.ocr_state, "WAITING")
        finally:
            # 取消等待下一批的监听任务。
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    async def test_failed_batch_does_not_stop_next_batch(self) -> None:
        """验证模型失败只回传本批错误，消费者继续处理下一批。

        Args:
            无外部参数。

        Returns:
            None  # 完成失败归属、后续消费和预留接口断言
        """
        # 检查联调接口返回独立的空结果，再准备满批和尾批的采集。
        app = await self.start_app(capture_window_ms=350, max_frames_per_session=10)
        recognizer = app.text_recognizer
        placeholder_results = recognizer.recognize_batch([b"first", b"second"])
        self.assertEqual(placeholder_results, [{"blocks": []}, {"blocks": []}])
        self.assertIsNot(placeholder_results[0]["blocks"], placeholder_results[1]["blocks"])

        # 启动采集并等待所有图片批次入队。
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.sessions[manager.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)

        # 第一批模拟模型异常，第二批返回两张图片的空文字结果。
        recognizer.recognize_batch = Mock(side_effect=[
            RuntimeError("测试整批推理失败"),
            [{"blocks": []}, {"blocks": []}],
        ])
        listener = asyncio.create_task(recognizer.listen_and_recognize_batches(app.publish_event))
        try:
            await asyncio.wait_for(recognizer.batch_queue.join(), 3)
            await manager.queue.join()

            # 检查失败批次未重试，尾批结果已保存，整轮未被标为完成。
            self.assertEqual(recognizer.recognize_batch.call_count, 2)
            self.assertEqual(session.errors, ["测试整批推理失败"])
            self.assertEqual(len(session.recognition_results), 2)
            self.assertEqual(session.ocr_state, "WAITING")
        finally:
            # 取消监听任务，结束本次测试的消费流程。
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
