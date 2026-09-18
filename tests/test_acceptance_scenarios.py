"""按验收场景验证跨轮回调、设备故障和持久化边界。"""

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_measurement_flow as flow_support
import test_recovery_and_faults as recovery_support
from enums import OCRState, FrequencyState, EventType
from models import CapturedFrame, CaptureSummary, FrequencyMeasurement, MeasurementEvent
from recovery import serialize_value


class AcceptanceScenarioTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records
    read_audit_reasons = recovery_support.RecoveryAndFaultTests.read_audit_reasons

    async def start_controlled_app(self, **overrides):
        """暂停自动图像与频率输入，由测试按确定顺序发布事件。"""
        for target in (
            "camera.SessionCamera.start_capture",
            "fake_frequency.FakeFrequency.listen_measurements",
        ):
            replacement = patch(target)
            replacement.start()
            self.addCleanup(replacement.stop)
        # 为人为暂停的识别步骤设置独立测试期限。
        settings = {
            "ocr_job_timeout_ms": 30000,
            "ocr_result_timeout_ms": 60000,
            "max_cycle_open_ms": 60000,
            **overrides,
        }
        return await self.start_app(**settings)

    async def publish_and_wait(self, event):
        """发布事件并等待机器管理员完成业务处理。

        Args:
            event: 待发布的测量事件。

        Returns:
            None: 收到处理回执，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        acknowledgement = asyncio.get_running_loop().create_future()
        await self.app.publish_event(replace(
            event, acknowledgement=acknowledgement,
        ))
        await asyncio.wait_for(acknowledgement, 10)

    async def supply_valid_inputs(self, session, value_hz=42.0):
        """生成独立证据和新测量，并送入本轮真实业务队列。"""
        frame_id = uuid4().hex
        image_path = self.output_directory / f"{session.session_id}-{frame_id}.ppm"
        image_path.write_bytes(b"P3\n1 1\n255\n1 2 3\n")
        timestamp = datetime.now(timezone.utc).isoformat()
        boundary = asyncio.get_running_loop().time()
        frame = CapturedFrame(
            session.session_id, session.capture_id, session.camera_id,
            frame_id, timestamp, boundary, str(image_path),
            session.capture_id, timestamp,
        )
        measurement = FrequencyMeasurement(
            session.session_id, session.frequency_source_id, uuid4().hex,
            len(session.frequency_candidates) + 1, value_hz,
            timestamp, boundary, timestamp,
        )

        # 依次确认图像与频率已被业务层接收。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FRAME_BATCH_SELECTED, session.machine_id, session.session_id, (frame,),
        ))
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, session.machine_id, session.session_id, measurement,
        ))
        return frame, measurement

    async def close_controlled_cycle(self, session):
        """正常关闭当前周期，并发布图像和频率封口事件。"""
        await self.app.handle_close(session.machine_id)
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_SEALED, session.machine_id, session.session_id, CaptureSummary(
                session.capture_id,
            ),
        ))
        await self.publish_and_wait(MeasurementEvent(
            "FrequencyWindowSealed", session.machine_id, session.session_id,
        ))

    async def test_01_bound_sources_reject_cross_machine_data(self):
        # 同时启动三台机器并取得各自的活动档案。
        app = await self.start_controlled_app()
        await asyncio.gather(*(
            app.handle_start(machine_id) for machine_id in app.machine_managers
        ))
        sessions = [
            machine_manager.sessions[machine_manager.active_session_id]
            for machine_manager in app.machine_managers.values()
        ]
        self.assertEqual(len({session.session_id for session in sessions}), 3)

        # 检查配置绑定，并向同一机器注入错误频率通道。
        for session in sessions:
            machine_number = int(session.machine_id[1:])
            self.assertEqual(session.camera_id, f"CAM{machine_number:02}")
            self.assertEqual(session.frequency_source_id, f"FREQ{machine_number:02}")
            frame, measurement = await self.supply_valid_inputs(session)
            await self.publish_and_wait(MeasurementEvent(
                EventType.FREQUENCY_MEASURED, session.machine_id, session.session_id,
                replace(measurement, frequency_source_id="wrong-frequency"),
            ))
            self.assertEqual(set(session.images_for_final_selection), {frame.frame_id})
            self.assertEqual(session.images_for_final_selection[frame.frame_id].camera_id, session.camera_id)
            self.assertEqual(
                set(session.frequency_candidates), {measurement.measurement_id},
            )

        # 三台机器分别关闭后只保存各自证据和通道。
        await asyncio.gather(*(
            self.close_controlled_cycle(session) for session in sessions
        ))
        await app.wait_until_idle(10)
        self.assertEqual(len(self.read_records()), 3)
        for record in self.read_records():
            self.assertEqual(record["outcome"], "COMPLETE")
            self.assertNotIn("selected_frames", record)
            self.assertEqual(
                record["frequency_candidates"][0]["frequency_source_id"],
                record["frequency_source_id"],
            )
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_audit_reasons())

    async def test_06_repeated_start_input_creates_one_cycle(self):
        # 并发重复启动后，只为唯一活动周期完成一次测量。
        app = await self.start_controlled_app()
        await asyncio.gather(*(app.handle_start("M01") for repeat in range(40)))
        machine_manager = app.machine_managers["M01"]
        self.assertEqual(len(machine_manager.sessions), 1)
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.supply_valid_inputs(session)
        await self.close_controlled_cycle(session)
        await app.wait_until_idle(10)
        self.assertEqual(len(self.read_records()), 1)
        self.assertEqual(self.read_records()[0]["outcome"], "COMPLETE")

    async def test_07_replayed_close_does_not_close_the_next_cycle(self):
        # 保存旧轮关闭事件，并完成该轮图像与频率封口。
        app = await self.start_controlled_app()
        machine_manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.supply_valid_inputs(first_session)
        close_event = MeasurementEvent(EventType.MACHINE_CLOSED, "M01", first_session.session_id)
        await self.publish_and_wait(close_event)
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_SEALED, "M01", first_session.session_id, CaptureSummary(
                first_session.capture_id,
            ),
        ))
        await self.publish_and_wait(MeasurementEvent(
            "FrequencyWindowSealed", "M01", first_session.session_id,
        ))

        # 新轮启动后重放原关闭事件，再用新事件身份重发旧轮关闭。
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.supply_valid_inputs(second_session)
        await self.publish_and_wait(close_event)
        await self.publish_and_wait(replace(close_event, event_id=uuid4().hex))
        self.assertEqual(machine_manager.active_session_id, second_session.session_id)
        self.assertIsNone(second_session.capture_stop_time)
        self.assertFalse(second_session.is_capture_finished)
        self.assertIn("CLOSE_SESSION_MISMATCH", self.read_audit_reasons())

        # 两轮分别结算一次，旧关闭事件不生成第三条记录。
        await self.close_controlled_cycle(second_session)
        await app.wait_until_idle(10)
        self.assertEqual(len(self.read_records()), 2)
        session_ids = {record["session_id"] for record in self.read_records()}
        self.assertEqual(len(session_ids), 2)

    async def test_09_identical_values_in_consecutive_cycles_keep_identity(self):
        # 连续创建两个数值相同、身份不同的测量周期。
        app = await self.start_controlled_app()
        expected_measurements = {}
        for cycle_number in range(2):
            await app.handle_start("M01")
            machine_manager = app.machine_managers["M01"]
            session = machine_manager.sessions[machine_manager.active_session_id]
            frame, measurement = await self.supply_valid_inputs(session, 42.0)
            expected_measurements[session.session_id] = measurement.measurement_id
            await self.close_controlled_cycle(session)
        await app.wait_until_idle(10)

        # 相同数值在相邻两轮中使用不同身份，各自成为本轮最终测量。
        records = self.read_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["final_measurement_id"] for record in records}), 2)
        for record in records:
            self.assertEqual(record["outcome"], "COMPLETE")
            self.assertEqual(record["final_frequency_hz"], 42.0)
            self.assertEqual(
                record["final_measurement_id"],
                expected_measurements[record["session_id"]],
            )

    async def test_10_previous_display_value_is_not_a_new_measurement(self):
        """验证早于新周期的旧读数不能作为新周期测量。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 保存上一轮读数，并打开新的测量窗口。
        app = await self.start_controlled_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        frame, old_measurement = await self.supply_valid_inputs(first_session)
        await self.close_controlled_cycle(first_session)
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]

        # 将旧读数时间设为新周期开始前一秒。
        old_measurement = replace(old_measurement, measured_monotonic=second_session.capture_start_time - 1)

        # 旧显示值沿用旧测量时间，即使重贴新周期身份也不能成为新读数。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", second_session.session_id,
            replace(old_measurement, session_id=second_session.session_id),
        ))
        self.assertEqual(second_session.frequency_candidates, {})
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_audit_reasons())

        # 仅补充新周期图像，关闭后应保存缺少有效频率的待复核记录。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FRAME_BATCH_SELECTED, "M01", second_session.session_id,
            (replace(
                frame, session_id=second_session.session_id,
                capture_id=second_session.capture_id, frame_id=uuid4().hex,
                captured_monotonic=asyncio.get_running_loop().time(),
            ),),
        ))
        await self.close_controlled_cycle(second_session)
        await app.wait_until_idle(10)
        record = next(
            record for record in self.read_records()
            if record["session_id"] == second_session.session_id
        )
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIsNone(record["final_frequency_hz"])
        self.assertEqual(record["frequency_candidates"], [])

    async def test_11_unassigned_delayed_frequency_is_audited_without_guessing(self):
        # 保留关闭后尚未封口的旧轮，同时启动新轮。
        app = await self.start_controlled_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        frame, measurement = await self.supply_valid_inputs(first_session)
        await app.handle_close("M01")
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]

        # 同时存在旧轮和新轮时，注入没有周期归属的延迟读数。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", None,
            replace(measurement, session_id="", measurement_id=uuid4().hex),
        ))
        self.assertEqual(len(first_session.frequency_candidates), 1)
        self.assertEqual(second_session.frequency_candidates, {})
        self.assertEqual(machine_manager.active_session_id, second_session.session_id)
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_audit_reasons())

        # 为两轮分别完成已有的有效输入，不把无归属读数写入任何记录。
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_SEALED, "M01", first_session.session_id, CaptureSummary(
                first_session.capture_id,
            ),
        ))
        await self.publish_and_wait(MeasurementEvent(
            "FrequencyWindowSealed", "M01", first_session.session_id,
        ))
        await self.supply_valid_inputs(second_session)
        await self.close_controlled_cycle(second_session)
        await app.wait_until_idle(10)
        self.assertEqual(len(self.read_records()), 2)
        self.assertTrue(all(
            len(record["frequency_candidates"]) == 1 for record in self.read_records()
        ))

    async def test_11_conflicting_cycle_identity_cannot_produce_normal_record(self):
        # 创建等待旧频率封口的档案和新活动周期。
        app = await self.start_controlled_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        frame, measurement = await self.supply_valid_inputs(first_session)
        await app.handle_close("M01")
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]

        # 外层事件声明旧轮、测量声明新轮，保持旧轮冲突待复核。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", first_session.session_id,
            replace(measurement, session_id=second_session.session_id),
        ))
        self.assertEqual(first_session.frequency_state, FrequencyState.FAILED)
        self.assertEqual(second_session.frequency_candidates, {})
        self.assertEqual(machine_manager.active_session_id, second_session.session_id)
        await self.supply_valid_inputs(second_session)
        await self.close_controlled_cycle(second_session)
        await app.wait_until_idle(10)
        records = {record["session_id"]: record for record in self.read_records()}
        self.assertEqual(
            records[first_session.session_id]["outcome"], "REVIEW_REQUIRED",
        )
        self.assertIn(
            "AMBIGUOUS_MEASUREMENT", records[first_session.session_id]["error_codes"],
        )
        self.assertEqual(records[second_session.session_id]["outcome"], "COMPLETE")

    async def test_15_io_loss_interrupts_cycles_without_fabricating_close(self):
        # 为三台运行中的机器注入共享 IO 故障。
        app = await self.start_controlled_app()
        for machine_id, machine_manager in app.machine_managers.items():
            await app.handle_start(machine_id)
            await self.supply_valid_inputs(machine_manager.sessions[machine_manager.active_session_id])
        await app.report_device_health("IO", False)
        await app.wait_until_idle(10)

        # IO 掉线只形成中断记录，不伪造正常关闭时间。
        records = self.read_records()
        self.assertEqual(len(records), 3)
        self.assertTrue(all(
            record["outcome"] == "INTERRUPTED" and "close_time" not in record
            for record in records
        ))
        await app.report_device_health("IO", True)
        for machine_id, machine_manager in app.machine_managers.items():
            await app.handle_start(machine_id)
            self.assertIsNone(machine_manager.active_session_id)
            await app.synchronize_machine(machine_id, "OPEN")
            await app.handle_start(machine_id)
            self.assertIsNone(machine_manager.active_session_id)

            # 确认本轮结束后才能开始新的完整周期。
            await app.handle_close(machine_id)
            await app.handle_start(machine_id)
            session = machine_manager.sessions[machine_manager.active_session_id]
            await self.supply_valid_inputs(session)
            await self.close_controlled_cycle(session)
        await app.wait_until_idle(10)
        self.assertEqual(sum(
            record["outcome"] == "COMPLETE" for record in self.read_records()
        ), 3)

    async def test_17_20_late_batches_stay_with_original_session(self):
        # 保存旧轮关闭边界，并保留新轮活动采集。
        app = await self.start_controlled_app()
        machine_manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        first_session = machine_manager.sessions[machine_manager.active_session_id]
        frame, measurement = await self.supply_valid_inputs(first_session)
        await app.handle_close("M01")
        await app.handle_start("M01")
        second_session = machine_manager.sessions[machine_manager.active_session_id]

        # 接收采集端筛选后延迟交付的旧轮尾批。
        delayed_frame = replace(frame, frame_id="delayed-before-close")
        await self.publish_and_wait(MeasurementEvent(
            EventType.FRAME_BATCH_SELECTED, "M01", first_session.session_id, (delayed_frame,),
        ))
        self.assertEqual(second_session.images_for_final_selection, {})
        expected_frames = {frame.frame_id, delayed_frame.frame_id}
        self.assertEqual(set(first_session.images_for_final_selection), expected_frames)
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_SEALED, "M01", first_session.session_id, CaptureSummary(
                first_session.capture_id,
            ),
        ))
        self.assertFalse(second_session.is_capture_finished)
        self.assertEqual(machine_manager.active_session_id, second_session.session_id)

        # 尾批和采集封口完成后，继续结算旧轮频率。
        await self.publish_and_wait(MeasurementEvent(
            "FrequencyWindowSealed", "M01", first_session.session_id,
        ))
        await self.supply_valid_inputs(second_session)
        await self.close_controlled_cycle(second_session)
        await app.wait_until_idle(10)
        records = {record["session_id"]: record for record in self.read_records()}
        self.assertNotIn("selected_frames", records[first_session.session_id])
        self.assertNotIn("selected_frames", records[second_session.session_id])
        self.assertTrue(all(
            record["outcome"] == "COMPLETE" for record in records.values()
        ))

    async def test_18_closed_cycle_is_not_restored_after_forced_process_exit(self):
        """验证强制退出后的已关闭周期不会在重启时恢复。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 为崩溃子进程准备独立的持久化配置。
        app = await self.start_app()
        configuration = replace(
            app.configuration, capture_window_ms=120, frequency_interval_ms=20,
        )
        await app.stop()
        configuration_path = self.output_directory / "closed-crash-configuration.json"
        configuration_path.write_text(
            json.dumps(serialize_value(configuration)), encoding="utf-8",
        )

        # 子进程在图片批次入队、关闭和封口事件处理完成后直接退出。
        script = """
import asyncio
import os
import sys
from pathlib import Path
from configuration import load_configuration
from enums import OCRState
from app import App
import app as application_module
sys.path.insert(0, "tests")
from fake_mvs import FakeMvsSdk
application_module.load_mvs_sdk = FakeMvsSdk

async def crash_after_close():
    app = App(load_configuration(Path(sys.argv[1])))
    await app.start()
    await app.handle_start("M01")
    machine_manager = app.machine_managers["M01"]
    session = machine_manager.sessions[machine_manager.active_session_id]
    while not session.is_capture_finished:
        app.state_changed.clear()
        await app.state_changed.wait()
    assert not app.text_recognizer.batch_queue.empty()
    await app.handle_close("M01")
    while not session.frequency_window_sealed:
        app.state_changed.clear()
        await app.state_changed.wait()
    await machine_manager.queue.join()
    assert session.frequency_candidates and session.ocr_state != OCRState.SUCCESS
    print(session.session_id, flush=True)
    os._exit(24)

asyncio.run(crash_after_close())
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", "-c", script, str(configuration_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            standard_output, standard_error = await asyncio.wait_for(
                process.communicate(), 15,
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        self.assertEqual(process.returncode, 24, standard_error.decode("utf-8"))
        session_id = standard_output.decode("utf-8").strip()

        # 重启后不恢复旧 Session，也不生成旧周期的测量结果。
        restarted = await self.start_app()
        await restarted.wait_until_idle(10)
        self.assertTrue(session_id)
        self.assertEqual(restarted.machine_managers["M01"].sessions, {})
        self.assertTrue(restarted.text_recognizer.batch_queue.empty())
        self.assertEqual(self.read_records(), [])

    async def test_19_evidence_write_sync_and_replace_failures_require_review(self):
        # 在同一应用实例上分别验证三个证据保存阶段。
        app = await self.start_app()
        failure_targets = (
            "camera.save_evidence_image", "camera.os.fsync", "camera.os.replace",
        )
        for target in failure_targets:
            with self.subTest(operation=target):
                # 分别在写入、同步和原子替换阶段注入磁盘错误。
                with patch(target, side_effect=OSError("模拟证据保存失败")):
                    with self.assertLogs("camera", level="ERROR"):
                        await app.handle_start("M01")
                        machine_manager = app.machine_managers["M01"]
                        session = machine_manager.sessions[machine_manager.active_session_id]
                        await self.wait_for_state(lambda: session.ocr_state == OCRState.FAILED)
                    await app.handle_close("M01")
                    await app.wait_until_idle(10)

                # 每次失败都保存待复核记录，并清理未发布的临时证据。
                record = next(
                    record for record in self.read_records()
                    if record["session_id"] == session.session_id
                )
                self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
                self.assertIn("CAPTURE_FAILED", record["error_codes"])
                self.assertEqual(record["evidence_refs"], [])
                self.assertEqual(list(self.output_directory.rglob("*.partial")), [])
        self.assertEqual(len(self.read_records()), 3)

    async def test_19_evidence_lost_after_ocr_cannot_be_committed_as_complete(self):
        # 等待本轮 OCR 成功，保留尚未关闭的周期。
        app = await self.start_controlled_app()
        machine_manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        session = machine_manager.sessions[machine_manager.active_session_id]
        frame, measurement = await self.supply_valid_inputs(session)
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_SEALED, "M01", session.session_id, CaptureSummary(session.capture_id),
        ))
        await self.wait_for_state(lambda: session.ocr_state == OCRState.SUCCESS)

        # OCR 已成功后删除证据，提交前重新读取时应转为待复核。
        Path(session.ocr_result.evidence_refs[0]).unlink()
        with self.assertLogs("machine_manager", level="ERROR"):
            await self.close_controlled_cycle(session)
            await app.wait_until_idle(10)
        records = self.read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "REVIEW_REQUIRED")
        self.assertIn("EVIDENCE_UNAVAILABLE", records[0]["error_codes"])

    async def test_frequency_identity_conflict_still_triggers_finalization(self) -> None:
        """验证频率周期身份冲突仍触发一次完成检查并保留无效状态。

        Args:
            无外部参数。

        Returns:
            None: 通过断言验证冲突状态和完成检查次数。
            返回示例：
                None  # 无返回数据
        """
        # 建立受控周期并取得原有频率候选。
        app = await self.start_controlled_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        frame, measurement = await self.supply_valid_inputs(session)

        # 将不同周期的测量送入原档案，验证冲突路径继续结算。
        conflicting_measurement = replace(measurement, session_id="another-session")
        event = MeasurementEvent(
            EventType.FREQUENCY_MEASURED,
            "M01",
            session.session_id,
            conflicting_measurement,
        )
        with patch.object(machine_manager, "try_finalize", new_callable=AsyncMock) as finalize:
            await self.publish_and_wait(event)
            finalize.assert_awaited_once_with(session)

        # 冲突测量不替换原候选，并记录无效频率和审计原因。
        self.assertEqual(session.frequency_state, FrequencyState.FAILED)
        self.assertIn("AMBIGUOUS_MEASUREMENT", session.errors)
        self.assertEqual(session.frequency_candidates[measurement.measurement_id], measurement)
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_audit_reasons())


if __name__ == "__main__":
    unittest.main()
