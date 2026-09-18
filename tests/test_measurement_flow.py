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
from app import App
from models import MeasurementEvent
from database import DatabaseRequest
from fake_mvs import FakeMvsSdk


class MeasurementFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # 按正式程序的非调试模式执行异步流程和期限测试。
        asyncio.get_running_loop().set_debug(False)

        # 创建每次测试独立的证据目录和数据库位置。
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output_directory = Path(self.temporary_directory.name)
        self.app = None

        # 仅在测试中替换 SDK，业务相机适配器仍使用正式实现。
        replacement = patch("app.load_mvs_sdk", side_effect=FakeMvsSdk)
        replacement.start()
        self.addCleanup(replacement.stop)

    async def asyncTearDown(self) -> None:
        # 关闭测试应用实例并释放临时文件。
        if self.app is not None:
            await asyncio.wait_for(self.app.stop(), 10)
        self.temporary_directory.cleanup()

    async def start_app(self, **overrides: object) -> App:
        """创建三台机器的测试配置并启动应用实例。"""
        machines = tuple(
            MachineConfiguration(
                machine_id=f"M{machine_number:02}",
                camera_id=f"CAM{machine_number:02}",
                frequency_source_id=f"FREQ{machine_number:02}",
                camera_serial=f"SERIAL{machine_number:02}",
                simulated_lines=(f"MODEL {machine_number}", "SAME", "SAME"),
                simulated_frequencies_hz=(40.0, 40.0, 43.0),
            )
            for machine_number in range(1, 4)
        )
        configuration = MeasurementConfiguration(
            machines=machines,
            database_path=self.output_directory / "measurements.sqlite3",
            evidence_directory=self.output_directory / "evidence",
            mvs_development_directory=Path("test-sdk"),
            capture_window_ms=1000,
            simulated_ocr_delay_ms=20,
            frequency_interval_ms=150,
            max_frames_per_session=2,
            max_cycle_open_ms=30000,
            ocr_result_timeout_ms=20000,
            storage_retry_delay_ms=5,
            shutdown_timeout_ms=2000,
        )
        self.app = App(replace(configuration, **overrides))
        await self.app.start()
        return self.app

    async def wait_for_state(self, predicate, timeout_seconds: float = 20) -> None:
        """等待业务事件推动指定条件成立。"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not predicate():
            self.app.state_changed.clear()
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            try:
                await asyncio.wait_for(
                    self.app.state_changed.wait(), remaining_seconds,
                )
            except asyncio.TimeoutError:
                # 输出队列和逐帧状态，定位等待未完成的业务步骤。
                states = [
                    {
                        "machine_id": session.machine_id,
                        "ocr_state": session.ocr_state,
                        "errors": session.errors,
                        "queued_events": machine_manager.queue.qsize(),
                        "device_faults": sorted(machine_manager.device_faults),
                    }
                    for machine_manager in self.app.machine_managers.values()
                    for session in machine_manager.sessions.values()
                ]
                workers = [
                    (task.get_name(), task.done(), str(task.get_coro()))
                    for task in self.app.worker_tasks
                ]
                self.fail(f"等待业务状态超时：{states}；工作任务：{workers}")

    def read_records(self) -> list[dict]:
        """读取已经提交的完整结果。"""
        connection = sqlite3.connect(self.app.configuration.database_path)
        with closing(connection):
            records = connection.execute(
                "SELECT payload_json FROM measurements ORDER BY start_time"
            ).fetchall()
        return [json.loads(record[0]) for record in records]

    async def test_three_machines_save_independent_records(self) -> None:
        app = await self.start_app()

        # 同时采集三台机器并等待各自的 OCR 完成。
        await asyncio.gather(*(
            app.handle_start(machine_id) for machine_id in app.machine_managers
        ))
        await self.wait_for_state(lambda: all(
            next(iter(machine_manager.sessions.values())).ocr_done
            for machine_manager in app.machine_managers.values()
        ))
        self.assertEqual(self.read_records(), [])
        await asyncio.gather(*(
            app.handle_close(machine_id) for machine_id in app.machine_managers
        ))
        await app.wait_until_idle()

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
        app = await self.start_app(simulated_ocr_delay_ms=250)
        machine_manager = app.machine_managers["M01"]

        # 在第一轮 OCR 结束前关闭并立即启动第二轮。
        await app.handle_start("M01")
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: first_session.capture_sealed)
        await app.handle_close("M01")
        await app.handle_start("M01")
        second_session_id = machine_manager.active_session_id
        self.assertNotEqual(first_session.session_id, second_session_id)
        await self.wait_for_state(lambda: first_session.commit_state == "COMMITTED")

        # 第一轮提交成功后，第二轮仍保持活动状态。
        self.assertEqual(machine_manager.active_session_id, second_session_id)
        self.assertEqual(self.read_records()[0]["session_id"], first_session.session_id)
        await app.handle_close("M01")
        await app.wait_until_idle()
        self.assertEqual(len(self.read_records()), 2)

    async def test_early_close_seals_only_the_old_capture(self) -> None:
        app = await self.start_app(capture_window_ms=800)
        machine_manager = app.machine_managers["M01"]

        # 采到第一帧和频率后提前关闭，再立即打开新窗口。
        await app.handle_start("M01")
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: (
            first_session.selected_frames and first_session.frequency_candidates
        ))
        await app.handle_close("M01")
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]
        await machine_manager.camera.seal_capture(first_session.capture_id)
        await self.wait_for_state(lambda: bool(second_session.frequency_candidates))
        self.assertFalse(second_session.capture_sealed)
        self.assertLess(len(first_session.selected_frames), 5)
        await app.handle_close("M01")
        await app.wait_until_idle()
        self.assertEqual(len(self.read_records()), 2)

    async def test_delayed_frequency_stays_with_original_session(self) -> None:
        app = await self.start_app(frequency_delivery_delay_ms=1000)
        machine_manager = app.machine_managers["M01"]

        # 在读数尚未送达时关闭第一轮并打开第二轮。
        await app.handle_start("M01")
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: (
            first_session.selected_frames
            and machine_manager.frequency.active_window.pending_deliveries
        ))
        self.assertEqual(first_session.frequency_candidates, {})
        await app.handle_close("M01")
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: first_session.commit_state == "COMMITTED")

        # 已结算的读数全部属于第一轮，第二轮仍然活动。
        first_record = self.read_records()[0]
        self.assertTrue(first_record["frequency_candidates"])
        self.assertTrue(all(
            measurement["session_id"] == first_session.session_id
            for measurement in first_record["frequency_candidates"]
        ))
        self.assertEqual(machine_manager.active_session_id, second_session.session_id)
        await app.handle_close("M01")
        await app.wait_until_idle()
        measurement_ids = [
            {
                measurement["measurement_id"]
                for measurement in record["frequency_candidates"]
            }
            for record in self.read_records()
        ]
        self.assertFalse(measurement_ids[0] & measurement_ids[1])

    async def test_equal_frequency_values_have_different_measurement_ids(self) -> None:
        app = await self.start_app()
        machine_manager = app.machine_managers["M01"]
        machine_manager.frequency.machine = replace(
            machine_manager.machine, simulated_frequencies_hz=(42.0,),
        )

        # 收集数值相同但身份不同的新测量。
        await app.handle_start("M01")
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: len(session.frequency_candidates) >= 3)
        await app.handle_close("M01")
        await app.wait_until_idle()
        candidates = self.read_records()[0]["frequency_candidates"]
        self.assertEqual(
            {measurement["value_hz"] for measurement in candidates}, {42.0},
        )
        self.assertEqual(
            len({value["measurement_id"] for value in candidates}), len(candidates),
        )

    async def test_no_valid_frequency_saves_review_record(self) -> None:
        app = await self.start_app()
        machine_manager = app.machine_managers["M01"]
        machine_manager.frequency.machine = replace(
            machine_manager.machine, simulated_frequencies_hz=(0.0, float("nan"), -1.0),
        )

        # OCR 成功后关闭，确认缺频率不会补零或永久等待。
        await app.handle_start("M01")
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        await app.handle_close("M01")
        await app.wait_until_idle()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIsNone(record["final_frequency_hz"])
        self.assertIn("FREQUENCY_NO_VALID_MEASUREMENT", record["error_codes"])

    async def test_camera_acquisition_failure_saves_review_record(self) -> None:
        app = await self.start_app()
        app.machine_managers["M01"].camera.device.handle.failure = True

        # SDK 取帧失败时保存本轮待复核记录。
        with self.assertLogs("camera", level="ERROR"):
            await app.handle_start("M01")
            machine_manager = app.machine_managers["M01"]
            session = machine_manager.sessions[machine_manager.active_session_id]
            await self.wait_for_state(lambda: session.ocr_state == "FAILED")
        await app.handle_close("M01")
        await app.wait_until_idle()
        self.assertEqual(self.read_records()[0]["outcome"], "REVIEW_REQUIRED")

    async def test_frequency_drain_has_a_deadline(self) -> None:
        app = await self.start_app(
            frequency_delivery_delay_ms=3000, frequency_drain_timeout_ms=50,
        )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)
        await app.handle_close("M01")
        await app.wait_until_idle()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("FREQUENCY_DRAIN_TIMEOUT", record["error_codes"])

    async def test_cycle_timeout_requires_reset_and_never_fakes_close(self) -> None:
        app = await self.start_app(max_cycle_open_ms=180)
        await app.handle_start("M01")
        await app.wait_until_idle()

        # 超时记录没有正常关闭时间，并等待明确关闭后重新同步。
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "INTERRUPTED")
        self.assertIsNone(record["close_time"])
        machine_manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        self.assertIsNone(machine_manager.active_session_id)
        await app.handle_close("M01")
        await app.handle_start("M01")
        self.assertIsNotNone(machine_manager.active_session_id)

    async def test_shutdown_records_interruption(self) -> None:
        app = await self.start_app()
        await app.handle_start("M01")
        await app.stop()
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "INTERRUPTED")
        self.assertIsNone(record["close_time"])
        self.assertFalse(app.worker_tasks)

    async def test_lost_acknowledgement_does_not_duplicate_record(self) -> None:
        app = await self.start_app()
        original_write = app.database.write_record
        attempt_count = 0

        # 第一次真实写入成功后模拟确认丢失。
        def write_then_lose_acknowledgement(request: DatabaseRequest) -> None:
            nonlocal attempt_count
            attempt_count += 1
            original_write(request)
            if attempt_count == 1:
                raise OSError("模拟确认丢失")

        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        with patch.object(
            app.database, "write_record", write_then_lose_acknowledgement,
        ):
            with self.assertLogs("database", level="ERROR"):
                await app.handle_close("M01")
                await app.wait_until_idle()
        self.assertEqual(attempt_count, 2)
        self.assertEqual(len(self.read_records()), 1)
        self.assertEqual(self.read_records()[0], json.loads(session.frozen_payload))

    async def test_failed_commit_retains_frozen_payload_for_retry(self) -> None:
        app = await self.start_app(storage_retry_attempts=1)
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 写入失败后保留冻结记录，并用同一内容重新提交。
        with patch.object(
            app.database, "write_record", side_effect=OSError("模拟写入失败"),
        ):
            with self.assertLogs(level="ERROR"):
                await app.handle_close("M01")
                await self.wait_for_state(
                    lambda: session.commit_state == "RETRY_PENDING",
                )
        frozen_payload = session.frozen_payload
        self.assertFalse(session.finished)
        await app.retry_pending_records()
        await app.wait_until_idle()
        self.assertEqual(session.frozen_payload, frozen_payload)
        self.assertEqual(len(self.read_records()), 1)

    async def test_ocr_capacity_failure_creates_review_record(self) -> None:
        app = await self.start_app(
            ocr_queue_capacity=1, simulated_ocr_delay_ms=250, max_frames_per_session=1,
        )
        await asyncio.gather(*(
            app.handle_start(machine_id) for machine_id in app.machine_managers
        ))
        await self.wait_for_state(lambda: all(
            next(iter(machine_manager.sessions.values())).capture_sealed
            for machine_manager in app.machine_managers.values()
        ))
        await asyncio.gather(*(
            app.handle_close(machine_id) for machine_id in app.machine_managers
        ))
        await app.wait_until_idle()
        records = self.read_records()
        self.assertEqual(sum(record["outcome"] == "COMPLETE" for record in records), 1)
        self.assertEqual(
            sum("OCR_QUEUE_FULL" in record["error_codes"] for record in records), 2,
        )

    async def test_unknown_machine_returns_clear_validation_error(self) -> None:
        app = await self.start_app()
        with self.assertRaisesRegex(ValueError, "未配置机器"):
            await app.handle_start("UNKNOWN")


if __name__ == "__main__":
    unittest.main()
