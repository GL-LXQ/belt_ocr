"""验证多机测量、跨周期归属和异常记录。"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from configuration import MachineConfiguration, MeasurementConfiguration
from measurement_executor import MeasurementExecutor
from models import MeasurementEvent
from storage import StorageRequest


class MeasurementFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # 按正式程序的非调试模式执行异步流程和期限测试。
        asyncio.get_running_loop().set_debug(False)

        # 创建每次测试独立的图片目录和数据库位置。
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output_directory = Path(self.temporary_directory.name)
        self.image_directory = self.output_directory / "images"
        self.image_directory.mkdir()
        self.image_directory.joinpath("frame.ppm").write_bytes(b"P3\n1 1\n255\n1 2 3\n")
        self.executor = None

    async def asyncTearDown(self) -> None:
        # 关闭测试执行器并释放临时文件。
        if self.executor is not None:
            await asyncio.wait_for(self.executor.stop(), 10)
        self.temporary_directory.cleanup()

    async def start_executor(self, **overrides: object) -> MeasurementExecutor:
        """创建三台机器的测试配置并启动执行器。"""
        machines = tuple(
            MachineConfiguration(
                machine_id=f"M{machine_number:02}",
                camera_id=f"CAM{machine_number:02}",
                frequency_source_id=f"FREQ{machine_number:02}",
                image_directory=self.image_directory,
                simulated_lines=(f"MODEL {machine_number}", "SAME", "SAME"),
                simulated_frequencies_hz=(40.0, 40.0, 43.0),
            )
            for machine_number in range(1, 4)
        )
        configuration = MeasurementConfiguration(
            machines=machines,
            database_path=self.output_directory / "measurements.sqlite3",
            evidence_directory=self.output_directory / "evidence",
            capture_window_ms=1000,
            frame_interval_ms=20,
            simulated_ocr_delay_ms=20,
            frequency_interval_ms=150,
            max_frames_per_session=2,
            max_cycle_open_ms=30000,
            ocr_result_timeout_ms=20000,
            storage_retry_delay_ms=5,
            shutdown_timeout_ms=2000,
        )
        self.executor = MeasurementExecutor(replace(configuration, **overrides))
        await self.executor.start()
        return self.executor

    async def wait_for_state(self, predicate, timeout_seconds: float = 20) -> None:
        """等待业务事件推动指定条件成立。"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not predicate():
            self.executor.state_changed.clear()
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            try:
                await asyncio.wait_for(
                    self.executor.state_changed.wait(), remaining_seconds,
                )
            except asyncio.TimeoutError:
                # 输出队列和逐帧状态，定位等待未完成的业务步骤。
                states = [
                    {
                        "machine_id": session.machine_id,
                        "ocr_state": session.ocr_state,
                        "errors": session.errors,
                        "ocr_jobs": session.ocr_jobs,
                        "queued_events": actor.queue.qsize(),
                        "device_faults": sorted(actor.device_faults),
                    }
                    for actor in self.executor.actors.values()
                    for session in actor.sessions.values()
                ]
                workers = [
                    (task.get_name(), task.done(), str(task.get_coro()))
                    for task in self.executor.worker_tasks
                ]
                self.fail(f"等待业务状态超时：{states}；工作任务：{workers}")

    def read_records(self) -> list[dict]:
        """读取已经提交的完整结果。"""
        connection = sqlite3.connect(self.executor.configuration.database_path)
        with closing(connection):
            records = connection.execute(
                "SELECT payload_json FROM measurements ORDER BY start_time"
            ).fetchall()
        return [json.loads(record[0]) for record in records]

    async def test_three_machines_save_independent_records(self) -> None:
        executor = await self.start_executor()

        # 同时采集三台机器并等待各自的 OCR 完成。
        await asyncio.gather(*(
            executor.handle_start(machine_id) for machine_id in executor.actors
        ))
        await self.wait_for_state(lambda: all(
            next(iter(actor.sessions.values())).ocr_done
            for actor in executor.actors.values()
        ))
        self.assertEqual(self.read_records(), [])
        await asyncio.gather(*(
            executor.handle_close(machine_id) for machine_id in executor.actors
        ))
        await executor.wait_until_idle()

        # 检查每条记录的机器、文字、频率身份和证据归属。
        records = self.read_records()
        self.assertEqual(len(records), 3)
        for record in records:
            machine_number = int(record["machine_id"][1:])
            self.assertEqual(record["camera_id"], f"CAM{machine_number:02}")
            self.assertEqual(record["frequency_source_id"], f"FREQ{machine_number:02}")
            self.assertTrue(all(
                frame["camera_id"] == record["camera_id"]
                and frame["session_id"] == record["session_id"]
                for frame in record["selected_frames"]
            ))
            self.assertTrue(all(
                measurement["frequency_source_id"] == record["frequency_source_id"]
                and measurement["session_id"] == record["session_id"]
                for measurement in record["frequency_candidates"]
            ))
            self.assertEqual(record["outcome"], "COMPLETE", record["error_codes"])
            self.assertTrue(record["is_simulated"])
            self.assertEqual(
                record["ordered_lines"], [f"MODEL {machine_number}", "SAME", "SAME"],
            )
            candidates = record["frequency_candidates"]
            latest_measurement = max(
                candidates, key=lambda measurement: measurement["source_sequence"],
            )
            self.assertEqual(
                record["final_measurement_id"], latest_measurement["measurement_id"],
            )
            self.assertEqual(
                record["final_frequency_hz"], latest_measurement["value_hz"],
            )
            self.assertTrue(all(
                Path(image_path).is_file() and record["session_id"] in image_path
                for image_path in record["evidence_refs"]
            ))

    async def test_old_ocr_and_commit_do_not_clear_new_active_session(self) -> None:
        executor = await self.start_executor(simulated_ocr_delay_ms=250)
        actor = executor.actors["M01"]

        # 在第一轮 OCR 结束前关闭并立即启动第二轮。
        await executor.handle_start("M01")
        first_session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: first_session.capture_sealed)
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        second_session_id = actor.active_session_id
        self.assertNotEqual(first_session.session_id, second_session_id)
        await self.wait_for_state(lambda: first_session.commit_state == "COMMITTED")

        # 第一轮提交成功后，第二轮仍保持活动状态。
        self.assertEqual(actor.active_session_id, second_session_id)
        self.assertEqual(self.read_records()[0]["session_id"], first_session.session_id)
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        self.assertEqual(len(self.read_records()), 2)

    async def test_duplicate_signals_and_results_are_idempotent(self) -> None:
        executor = await self.start_executor()
        actor = executor.actors["M01"]

        # 重复启动不会覆盖原来的 Session。
        await executor.handle_start("M01")
        session = actor.sessions[actor.active_session_id]
        await executor.handle_start("M01")
        self.assertEqual(actor.active_session_id, session.session_id)
        self.assertEqual(len(actor.sessions), 1)
        await self.wait_for_state(lambda: session.ocr_done)

        # 重复 OCR 和频率事件不增加候选数量。
        measurement = next(iter(session.frequency_candidates.values()))
        await executor.publish_event(MeasurementEvent(
            "FrequencyMeasured", "M01", session.session_id, measurement,
        ))
        await executor.publish_event(MeasurementEvent(
            "OCRCompleted", "M01", session.session_id, session.ocr_result,
        ))
        await actor.queue.join()
        self.assertEqual(
            session.frequency_candidates[measurement.measurement_id], measurement,
        )
        await executor.handle_close("M01")
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        self.assertEqual(len(self.read_records()), 1)

    async def test_early_close_seals_only_the_old_capture(self) -> None:
        executor = await self.start_executor(capture_window_ms=800)
        actor = executor.actors["M01"]

        # 采到第一帧和频率后提前关闭，再立即打开新窗口。
        await executor.handle_start("M01")
        first_session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: (
            first_session.selected_frames and first_session.frequency_candidates
        ))
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        second_session = actor.sessions[actor.active_session_id]
        actor.camera.seal_capture(first_session.capture_id)
        await self.wait_for_state(lambda: bool(second_session.frequency_candidates))
        self.assertFalse(second_session.capture_sealed)
        self.assertLess(len(first_session.selected_frames), 5)
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        self.assertEqual(len(self.read_records()), 2)

    async def test_delayed_frequency_stays_with_original_session(self) -> None:
        executor = await self.start_executor(frequency_delivery_delay_ms=1000)
        actor = executor.actors["M01"]

        # 在读数尚未送达时关闭第一轮并打开第二轮。
        await executor.handle_start("M01")
        first_session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: (
            first_session.selected_frames
            and actor.frequency.active_window.pending_deliveries
        ))
        self.assertEqual(first_session.frequency_candidates, {})
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        second_session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: first_session.commit_state == "COMMITTED")

        # 已结算的读数全部属于第一轮，第二轮仍然活动。
        first_record = self.read_records()[0]
        self.assertTrue(first_record["frequency_candidates"])
        self.assertTrue(all(
            measurement["session_id"] == first_session.session_id
            for measurement in first_record["frequency_candidates"]
        ))
        self.assertEqual(actor.active_session_id, second_session.session_id)
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        measurement_ids = [
            {
                measurement["measurement_id"]
                for measurement in record["frequency_candidates"]
            }
            for record in self.read_records()
        ]
        self.assertFalse(measurement_ids[0] & measurement_ids[1])

    async def test_equal_frequency_values_have_different_measurement_ids(self) -> None:
        executor = await self.start_executor()
        actor = executor.actors["M01"]
        actor.frequency.machine = replace(
            actor.machine, simulated_frequencies_hz=(42.0,),
        )

        # 收集数值相同但身份不同的新测量。
        await executor.handle_start("M01")
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: len(session.frequency_candidates) >= 3)
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        candidates = self.read_records()[0]["frequency_candidates"]
        self.assertEqual(
            {measurement["value_hz"] for measurement in candidates}, {42.0},
        )
        self.assertEqual(
            len({value["measurement_id"] for value in candidates}), len(candidates),
        )

    async def test_no_valid_frequency_saves_review_record(self) -> None:
        executor = await self.start_executor()
        actor = executor.actors["M01"]
        actor.frequency.machine = replace(
            actor.machine, simulated_frequencies_hz=(0.0, float("nan"), -1.0),
        )

        # OCR 成功后关闭，确认缺频率不会补零或永久等待。
        await executor.handle_start("M01")
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIsNone(record["final_frequency_hz"])
        self.assertIn("FREQUENCY_NO_VALID_MEASUREMENT", record["error_codes"])

    async def test_empty_ocr_waits_for_close_then_saves_review_record(self) -> None:
        executor = await self.start_executor()
        actor = executor.actors["M01"]
        actor.machine = replace(actor.machine, simulated_lines=())

        # 空识别结果先保留失败状态，正常关闭后保存异常记录。
        await executor.handle_start("M01")
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: session.ocr_state == "FAILED")
        self.assertEqual(self.read_records(), [])
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("OCR_NO_VALID_TEXT", record["error_codes"])

    async def test_empty_image_folder_saves_review_record(self) -> None:
        executor = await self.start_executor()
        self.image_directory.joinpath("frame.ppm").unlink()

        # 空文件夹作为本轮取流失败处理。
        with self.assertLogs("camera", level="ERROR"):
            await executor.handle_start("M01")
            actor = executor.actors["M01"]
            session = actor.sessions[actor.active_session_id]
            await self.wait_for_state(lambda: session.ocr_state == "FAILED")
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        self.assertEqual(self.read_records()[0]["outcome"], "REVIEW_REQUIRED")

    async def test_missing_evidence_cannot_be_saved_as_complete(self) -> None:
        executor = await self.start_executor(simulated_ocr_delay_ms=200)
        actor = executor.actors["M01"]

        # 在 OCR 读取前删除已采集的证据文件。
        await executor.handle_start("M01")
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)
        for frame in session.selected_frames.values():
            Path(frame.image_path).unlink()
        with self.assertLogs("ocr", level="ERROR"):
            await executor.handle_close("M01")
            await executor.wait_until_idle()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("OCR_PROCESSING_FAILED", record["error_codes"])

    async def test_ocr_timeout_does_not_block_another_machine(self) -> None:
        executor = await self.start_executor(
            capture_window_ms=30000, ocr_job_timeout_ms=30000,
        )
        recognition_entered = asyncio.Event()
        recognition_release = asyncio.Event()

        # 暂停识别，先等待两台机器都取得图像和有效频率。
        async def hold_recognition(job):
            recognition_entered.set()
            await recognition_release.wait()
            return job.simulated_lines

        with patch.object(executor.ocr, "recognize_frame", hold_recognition):
            try:
                await asyncio.gather(
                    executor.handle_start("M01"), executor.handle_start("M02"),
                )
                sessions = [
                    next(iter(executor.actors[machine_id].sessions.values()))
                    for machine_id in ("M01", "M02")
                ]
                await self.wait_for_state(lambda: all(
                    session.selected_frames and session.frequency_candidates
                    for session in sessions
                ))
                await asyncio.gather(
                    executor.handle_close("M01"), executor.handle_close("M02"),
                )
                await asyncio.wait_for(recognition_entered.wait(), 10)

                # 使用真实期限任务触发超时，分别保存两台机器的异常结果。
                for session in sessions:
                    actor = executor.actors[session.machine_id]
                    deadline_key = (session.session_id, "OCRTimeout")
                    actor.deadline_tasks.pop(deadline_key).cancel()
                    actor.schedule_timeout(session, "OCRTimeout", 50)
                await executor.wait_until_idle()
            finally:
                recognition_release.set()
        self.assertEqual(len(self.read_records()), 2)
        self.assertTrue(all(
            "OCR_TIMEOUT" in record["error_codes"] for record in self.read_records()
        ))

    async def test_frequency_drain_has_a_deadline(self) -> None:
        executor = await self.start_executor(
            frequency_delivery_delay_ms=3000, frequency_drain_timeout_ms=50,
        )
        await executor.handle_start("M01")
        actor = executor.actors["M01"]
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)
        await executor.handle_close("M01")
        await executor.wait_until_idle()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("FREQUENCY_DRAIN_TIMEOUT", record["error_codes"])

    async def test_cycle_timeout_requires_reset_and_never_fakes_close(self) -> None:
        executor = await self.start_executor(max_cycle_open_ms=180)
        await executor.handle_start("M01")
        await executor.wait_until_idle()

        # 超时记录没有正常关闭时间，并等待明确关闭后重新同步。
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "INTERRUPTED")
        self.assertIsNone(record["close_time"])
        actor = executor.actors["M01"]
        await executor.handle_start("M01")
        self.assertIsNone(actor.active_session_id)
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        self.assertIsNotNone(actor.active_session_id)

    async def test_shutdown_records_interruption(self) -> None:
        executor = await self.start_executor()
        await executor.handle_start("M01")
        await executor.stop()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "INTERRUPTED")
        self.assertIsNone(record["close_time"])
        self.assertFalse(executor.worker_tasks)

    async def test_lost_acknowledgement_does_not_duplicate_record(self) -> None:
        executor = await self.start_executor()
        original_write = executor.storage.write_record
        attempt_count = 0

        # 第一次真实写入成功后模拟确认丢失。
        def write_then_lose_acknowledgement(request: StorageRequest) -> None:
            nonlocal attempt_count
            attempt_count += 1
            original_write(request)
            if attempt_count == 1:
                raise OSError("模拟确认丢失")

        await executor.handle_start("M01")
        actor = executor.actors["M01"]
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        with patch.object(
            executor.storage, "write_record", write_then_lose_acknowledgement,
        ):
            with self.assertLogs("storage", level="ERROR"):
                await executor.handle_close("M01")
                await executor.wait_until_idle()
        self.assertEqual(attempt_count, 2)
        self.assertEqual(len(self.read_records()), 1)
        self.assertEqual(self.read_records()[0], json.loads(session.frozen_payload))

    async def test_failed_commit_retains_frozen_payload_for_retry(self) -> None:
        executor = await self.start_executor(storage_retry_attempts=1)
        await executor.handle_start("M01")
        actor = executor.actors["M01"]
        session = actor.sessions[actor.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 写入失败后保留冻结记录，并用同一内容重新提交。
        with patch.object(
            executor.storage, "write_record", side_effect=OSError("模拟写入失败"),
        ):
            with self.assertLogs(level="ERROR"):
                await executor.handle_close("M01")
                await self.wait_for_state(
                    lambda: session.commit_state == "RETRY_PENDING",
                )
        frozen_payload = session.frozen_payload
        self.assertFalse(session.finished)
        await executor.retry_pending_records()
        await executor.wait_until_idle()
        self.assertEqual(session.frozen_payload, frozen_payload)
        self.assertEqual(len(self.read_records()), 1)

    async def test_ocr_capacity_failure_creates_review_record(self) -> None:
        executor = await self.start_executor(
            ocr_queue_capacity=1, simulated_ocr_delay_ms=250, max_frames_per_session=1,
        )
        await asyncio.gather(*(
            executor.handle_start(machine_id) for machine_id in executor.actors
        ))
        await self.wait_for_state(lambda: all(
            next(iter(actor.sessions.values())).capture_sealed
            for actor in executor.actors.values()
        ))
        await asyncio.gather(*(
            executor.handle_close(machine_id) for machine_id in executor.actors
        ))
        await executor.wait_until_idle()
        records = self.read_records()
        self.assertEqual(sum(record["outcome"] == "COMPLETE" for record in records), 1)
        self.assertEqual(
            sum("OCR_QUEUE_FULL" in record["error_codes"] for record in records), 2,
        )

    async def test_unknown_machine_returns_clear_validation_error(self) -> None:
        executor = await self.start_executor()
        with self.assertRaisesRegex(ValueError, "未配置机器"):
            await executor.handle_start("UNKNOWN")


if __name__ == "__main__":
    unittest.main()
