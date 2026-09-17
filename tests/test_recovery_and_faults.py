"""验证重启清理、故障隔离、本次运行内自动补交和事件审计。"""

import asyncio
import json
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_measurement_flow as flow_support
from measurement_executor import MeasurementExecutor
from models import MeasurementEvent
from recovery import serialize_value
from database import DatabaseRequest


class RecoveryAndFaultTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_executor = flow_support.MeasurementFlowTests.start_executor
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    def read_audit_reasons(self) -> list[str]:
        """读取恢复库中的异常审计原因。"""
        connection = sqlite3.connect(self.executor.configuration.recovery_path)
        with closing(connection):
            rows = connection.execute("SELECT reason FROM audit_entries").fetchall()
        return [row[0] for row in rows]

    async def restart_executor(self, **overrides) -> MeasurementExecutor:
        """使用同一数据库和证据目录重启系统。"""
        configuration = replace(self.executor.configuration, **overrides)
        await self.executor.stop()
        self.executor = MeasurementExecutor(configuration)
        await self.executor.start()
        return self.executor

    async def test_database_failure_is_automatically_retried(self) -> None:
        executor = await self.start_executor(
            storage_retry_attempts=1, storage_retry_interval_ms=100,
        )
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 暂时禁止最终库写入，确认记录先进入本地待提交区。
        with patch.object(
            executor.database, "write_record", side_effect=OSError("模拟断库"),
        ):
            with self.assertLogs(level="ERROR"):
                await executor.handle_close("M01")
                await self.wait_for_state(
                    lambda: session.commit_state == "RETRY_PENDING",
                )
            self.assertEqual(executor.recovery.pending_count(), 1)
            frozen_payload = session.frozen_payload

        # 故障解除后自动补交，不调用手动重试入口。
        await executor.wait_until_idle(10)
        self.assertEqual(self.read_records()[0], json.loads(frozen_payload))
        self.assertEqual(executor.recovery.pending_count(), 0)

    async def test_restart_discards_pending_payload_and_accepts_new_cycle(self) -> None:
        """验证重启清理旧待提交记录并正常保存新周期。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 创建待写入的完整测量结果。
        executor = await self.start_executor(
            storage_retry_attempts=1, shutdown_timeout_ms=100,
        )
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 持续写入失败后退出，保存同一份冻结内容。
        with patch.object(
            executor.database, "write_record", side_effect=OSError("模拟断库"),
        ):
            with self.assertLogs(level="WARNING"):
                await executor.handle_close("M01")
                await self.wait_for_state(
                    lambda: session.commit_state == "RETRY_PENDING",
                )
                await executor.stop()
        # 确认退出时旧结果仍在待提交区，并保留证据路径。
        self.assertEqual(executor.recovery.pending_count(), 1)
        evidence_paths = [frame.image_path for frame in session.selected_frames.values()]
        restarted = await self.restart_executor(shutdown_timeout_ms=2000)

        # 确认旧记录已清理，多次补交检查也不会生成旧结果。
        self.assertEqual(restarted.recovery.pending_count(), 0)
        self.assertEqual(restarted.machine_managers["M01"].sessions, {})
        await restarted.database.enqueue_pending_records()
        await restarted.database.enqueue_pending_records()
        await restarted.wait_until_idle(10)
        self.assertEqual(self.read_records(), [])
        self.assertTrue(all(Path(image_path).is_file() for image_path in evidence_paths))

        # 接收并保存本次运行的新周期。
        await restarted.handle_start("M01")
        machine_manager = restarted.machine_managers["M01"]
        new_session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: new_session.ocr_done)
        await restarted.handle_close("M01")
        await restarted.wait_until_idle(10)
        self.assertEqual([record["session_id"] for record in self.read_records()], [new_session.session_id])

    async def test_startup_clears_old_work_before_capacity_check(self) -> None:
        """验证启动先清理旧积压和检查点，并保留历史结果与审计。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 保存一轮历史结果和审计，然后停止执行器。
        executor = await self.start_executor()
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        await executor.handle_close("M01")
        await executor.wait_until_idle(10)
        expected_records = self.read_records()
        executor.recovery.audit("PREVIOUS_RUN_AUDIT", machine_id="M01")
        await executor.stop()

        # 登记普通、冲突和未受理类型的旧待提交记录。
        for record_id, record_type in (
            ("old_measurement", "measurement"),
            ("old_conflict", "measurement"),
            ("old_rejection", "rejected_cycle"),
        ):
            executor.recovery.stage_record(DatabaseRequest("M01", record_id, "{}", record_id, record_type))
        executor.recovery.delay_record("old_conflict", 0, blocked=True)

        # 保存不在本次配置中的旧机器检查点。
        with closing(sqlite3.connect(executor.configuration.recovery_path)) as connection:
            with connection:
                connection.execute("INSERT INTO machine_checkpoints VALUES (?, ?)", ("OLD_MACHINE", "{}"))

        # 使用低于旧积压数量的容量上限启动，确认先清理再检查容量。
        restarted = await self.restart_executor(max_persistent_records=1)
        self.assertEqual(restarted.recovery.pending_count(), 0)
        self.assertEqual(restarted.machine_managers["M01"].acceptance_state, "READY")
        checkpoints = restarted.recovery.load_checkpoints()
        self.assertEqual(set(checkpoints), set(restarted.machine_managers))
        self.assertTrue(all(not checkpoint["sessions"] for checkpoint in checkpoints.values()))

        # 确认历史结果、提交身份、审计和证据仍然保留。
        self.assertEqual(self.read_records(), expected_records)
        self.assertTrue(restarted.recovery.record_status(session.session_id)["committed"])
        self.assertIn("PREVIOUS_RUN_AUDIT", self.read_audit_reasons())
        self.assertTrue(all(Path(image_path).is_file() for image_path in expected_records[0]["evidence_refs"]))

    async def test_initial_unknown_state_requires_close_or_synchronization(self) -> None:
        """验证未知初始状态在有效关闭或关闭状态同步后接收新周期。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 初始状态未知时忽略启动，明确关闭后清除未知状态故障。
        executor = await self.start_executor(initial_machine_state="UNKNOWN")
        machine_manager = executor.machine_managers["M01"]
        await executor.handle_start("M01")
        self.assertEqual(machine_manager.sessions, {})
        self.assertEqual(machine_manager.acceptance_state, "FAULT")
        await executor.handle_close("M01")
        self.assertEqual(machine_manager.acceptance_state, "READY")
        await executor.handle_start("M01")
        self.assertIsNotNone(machine_manager.active_session_id)

        # 其他机器同步为运行中时仍等待关闭，不创建半轮测量。
        await executor.synchronize_machine("M02", "OPEN")
        await executor.handle_start("M02")
        self.assertEqual(executor.machine_managers["M02"].acceptance_state, "WAIT_CYCLE_RESET")
        self.assertEqual(executor.machine_managers["M02"].sessions, {})
        await executor.handle_close("M02")
        self.assertEqual(executor.machine_managers["M02"].acceptance_state, "READY")

        # 同步为关闭状态后允许下一次启动。
        await executor.synchronize_machine("M03", "CLOSED")
        self.assertEqual(executor.machine_managers["M03"].acceptance_state, "READY")
        await executor.handle_start("M03")
        self.assertIsNotNone(executor.machine_managers["M03"].active_session_id)

    async def test_closed_session_does_not_resume_unfinished_ocr(self) -> None:
        """验证重启后不恢复已关闭周期的 OCR 和超时任务。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 创建已关闭但 OCR 尚未完成的测量档案。
        executor = await self.start_executor(
            simulated_ocr_delay_ms=1200, shutdown_timeout_ms=100,
        )
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)
        await executor.handle_close("M01")
        await self.wait_for_state(lambda: session.frequency_window_sealed)
        self.assertFalse(session.ocr_done)

        # 在 OCR 完成前退出，再启动新的执行器。
        with self.assertLogs(level="WARNING"):
            restarted = await self.restart_executor(
                simulated_ocr_delay_ms=10, shutdown_timeout_ms=2000,
            )
        await restarted.wait_until_idle(10)

        # 确认旧档案和任务没有恢复，也没有生成旧周期的结果。
        restarted_machine_manager = restarted.machine_managers["M01"]
        self.assertEqual(restarted_machine_manager.sessions, {})
        self.assertIsNone(restarted_machine_manager.active_session_id)
        self.assertEqual(restarted_machine_manager.deadline_tasks, {})
        self.assertEqual(restarted_machine_manager.background_tasks, set())
        self.assertEqual(restarted.ocr.pending_count, 0)
        self.assertEqual(self.read_records(), [])

    async def test_process_crash_discards_previous_open_cycle(self) -> None:
        """验证异常退出后不恢复旧周期，并按本次初始状态等待复位。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 保存子进程使用的测量配置。
        executor = await self.start_executor()
        configuration = executor.configuration
        await executor.stop()
        configuration_path = self.output_directory / "crash-configuration.json"
        configuration_path.write_text(
            json.dumps(serialize_value(configuration)), encoding="utf-8",
        )

        # 子进程确认启动检查点后直接异常退出。
        script = """
import asyncio
import os
import sys
from pathlib import Path
from configuration import load_configuration
from measurement_executor import MeasurementExecutor

async def crash_after_start():
    executor = MeasurementExecutor(load_configuration(Path(sys.argv[1])))
    await executor.start()
    await executor.handle_start('M01')
    os._exit(23)

asyncio.run(crash_after_start())
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", "-c", script, str(configuration_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 23)

        # 重启时使用运行中状态，确认不恢复旧周期。
        restarted = await self.restart_executor(initial_machine_state="OPEN")
        await restarted.wait_until_idle(10)
        self.assertEqual(self.read_records(), [])
        self.assertEqual(restarted.machine_managers["M01"].sessions, {})
        self.assertEqual(restarted.machine_managers["M01"].acceptance_state, "WAIT_CYCLE_RESET")

        # 运行中忽略启动，关闭后允许接收下一轮启动。
        await restarted.handle_start("M01")
        self.assertIsNone(restarted.machine_managers["M01"].active_session_id)
        await restarted.handle_close("M01")
        self.assertEqual(restarted.machine_managers["M01"].acceptance_state, "READY")
        await restarted.handle_start("M01")
        self.assertIsNotNone(restarted.machine_managers["M01"].active_session_id)

    async def test_duplicate_event_is_rejected_after_restart(self) -> None:
        executor = await self.start_executor()
        event = MeasurementEvent("MachineStarted", "M01")
        await executor.publish_event(event)
        await executor.machine_managers["M01"].queue.join()
        await executor.handle_close("M01")
        await executor.wait_until_idle(10)

        # 重放上次运行已确认的启动事件。
        restarted = await self.restart_executor()
        await restarted.publish_event(event)
        await restarted.machine_managers["M01"].queue.join()
        self.assertIsNone(restarted.machine_managers["M01"].active_session_id)
        self.assertIn("DUPLICATE", self.read_audit_reasons())
        self.assertEqual(len(self.read_records()), 1)

    async def test_stale_close_cannot_close_a_new_cycle(self) -> None:
        executor = await self.start_executor()
        await executor.handle_start("M01")
        old_session_id = executor.machine_managers["M01"].active_session_id
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        new_session_id = executor.machine_managers["M01"].active_session_id

        # 把带旧 Session 身份的关闭事件送回业务队列。
        await executor.publish_event(MeasurementEvent(
            "MachineClosed", "M01", old_session_id,
        ))
        await executor.machine_managers["M01"].queue.join()
        self.assertEqual(executor.machine_managers["M01"].active_session_id, new_session_id)
        self.assertIn("CLOSE_SESSION_MISMATCH", self.read_audit_reasons())

    async def test_source_sequence_and_epoch_require_resynchronization(self) -> None:
        executor = await self.start_executor()
        machine_manager = executor.machine_managers["M01"]

        # 同一来源的旧序号和未经确认的新批次均被隔离。
        event = MeasurementEvent(
            "MachineStarted", "M01", source_id="test-input",
            source_epoch="first", source_sequence=10,
        )
        await executor.publish_event(event)
        await machine_manager.queue.join()
        first_session_id = machine_manager.active_session_id
        await executor.publish_event(MeasurementEvent(
            "MachineClosed", "M01", first_session_id, source_id="test-input",
            source_epoch="first", source_sequence=9,
        ))
        await executor.publish_event(MeasurementEvent(
            "MachineClosed", "M01", first_session_id, source_id="test-input",
            source_epoch="second", source_sequence=1,
        ))
        await machine_manager.queue.join()
        self.assertEqual(machine_manager.active_session_id, first_session_id)
        self.assertIn("STALE_SOURCE_SEQUENCE", self.read_audit_reasons())
        self.assertIn("SOURCE_EPOCH_MISMATCH", self.read_audit_reasons())

        # 确认来源新批次和机器关闭状态后允许下一轮。
        await executor.synchronize_source("M01", "test-input", "second")
        await executor.synchronize_machine("M01", "CLOSED")
        await executor.publish_event(MeasurementEvent(
            "MachineStarted", "M01", source_id="test-input",
            source_epoch="second", source_sequence=1,
        ))
        await machine_manager.queue.join()
        self.assertIsNotNone(machine_manager.active_session_id)
        self.assertNotEqual(machine_manager.active_session_id, first_session_id)

    async def test_frequency_identity_conflict_requires_review(self) -> None:
        executor = await self.start_executor()
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: bool(session.frequency_candidates))
        measurement = next(iter(session.frequency_candidates.values()))

        # 对同一测量身份注入不同数值。
        await executor.publish_event(MeasurementEvent(
            "FrequencyMeasured", "M01", session.session_id,
            replace(measurement, value_hz=measurement.value_hz + 1),
        ))
        await executor.handle_close("M01")
        await executor.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("AMBIGUOUS_MEASUREMENT", record["error_codes"])
        self.assertIn("MEASUREMENT_ID_CONFLICT", self.read_audit_reasons())

    async def test_device_fault_isolated_to_its_machine(self) -> None:
        executor = await self.start_executor()
        await executor.handle_start("M01")
        await executor.handle_start("M02")
        second_session_id = executor.machine_managers["M02"].active_session_id

        # 单台相机故障只中断绑定机器。
        await executor.report_device_health("CAM01", False)
        self.assertEqual(executor.machine_managers["M01"].acceptance_state, "FAULT")
        self.assertEqual(executor.machine_managers["M02"].active_session_id, second_session_id)
        await executor.report_device_health("CAM01", True)
        self.assertEqual(executor.machine_managers["M01"].acceptance_state, "WAIT_CYCLE_RESET")
        await executor.synchronize_machine("M01", "CLOSED")
        self.assertEqual(executor.machine_managers["M01"].acceptance_state, "READY")

    async def test_initial_open_state_does_not_create_midcycle_session(self) -> None:
        executor = await self.start_executor(initial_machine_state="OPEN")
        await executor.handle_start("M01")
        self.assertIsNone(executor.machine_managers["M01"].active_session_id)
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        self.assertIsNotNone(executor.machine_managers["M01"].active_session_id)

    async def test_disk_capacity_blocks_new_cycles_and_recovers(self) -> None:
        executor = await self.start_executor(minimum_free_disk_bytes=10**30)
        machine_manager = executor.machine_managers["M01"]
        await self.wait_for_state(lambda: machine_manager.acceptance_state == "DEGRADED")
        await executor.handle_start("M01")
        self.assertIsNone(machine_manager.active_session_id)

        # 容量恢复后仍须确认被拒收周期已经关闭。
        executor.configuration = replace(
            executor.configuration, minimum_free_disk_bytes=0,
        )
        await self.wait_for_state(lambda: machine_manager.capacity_available)
        self.assertTrue(machine_manager.waiting_cycle_reset)
        await executor.handle_close("M01")
        await executor.handle_start("M01")
        self.assertIsNotNone(machine_manager.active_session_id)

    async def test_second_process_instance_cannot_share_recovery_store(self) -> None:
        executor = await self.start_executor()
        second_executor = MeasurementExecutor(executor.configuration)
        with self.assertLogs(level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "初始化失败"):
                await second_executor.start()
        self.assertTrue(executor.accepting_signals)

    async def test_frame_retry_is_persisted_and_not_counted_twice(self) -> None:
        executor = await self.start_executor(max_frames_per_session=1)
        original_recognize = executor.ocr.recognize_frame
        attempt_count = 0

        # 第一帧第一次识别失败，第二次成功。
        async def fail_once(job):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                raise OSError("模拟 OCR 临时失败")
            return await original_recognize(job)

        with patch.object(executor.ocr, "recognize_frame", fail_once):
            with self.assertLogs("ocr", level="ERROR"):
                await executor.handle_start("M01")
                machine_manager = executor.machine_managers["M01"]
                session = machine_manager.sessions[machine_manager.active_session_id]
                await self.wait_for_state(lambda: session.ocr_done)
            await executor.handle_close("M01")
            await executor.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(attempt_count, 2)
        self.assertEqual(len(record["ocr_jobs"]), 1)
        self.assertEqual(next(iter(record["ocr_jobs"].values()))["attempt"], 2)
        self.assertEqual(record["ordered_lines"], ["MODEL 1", "SAME", "SAME"])

    async def test_database_unavailable_at_startup_uses_local_spool(self) -> None:
        blocked_directory = self.output_directory / "blocked"
        blocked_directory.write_text("模拟不可用路径", encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            executor = await self.start_executor(
                database_path=blocked_directory / "measurements.sqlite3",
                recovery_database_path=self.output_directory / "recovery.sqlite3",
                storage_retry_attempts=1, storage_retry_interval_ms=100,
            )
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        with self.assertLogs(level="ERROR"):
            await executor.handle_close("M01")
            await self.wait_for_state(lambda: session.commit_state == "RETRY_PENDING")
        self.assertEqual(executor.recovery.pending_count(), 1)

        # 恢复最终库路径后自动创建数据库并补交。
        blocked_directory.unlink()
        blocked_directory.mkdir()
        await executor.wait_until_idle(10)
        self.assertEqual(self.read_records()[0]["outcome"], "COMPLETE")

    async def test_storage_queue_full_keeps_all_records_durable(self) -> None:
        executor = await self.start_executor(
            storage_queue_capacity=1, max_frames_per_session=1,
        )
        original_write = executor.database.write_record
        write_release = threading.Event()

        # 暂停真实写库，等待三份记录全部进入持久化待提交区。
        def hold_write(request):
            if not write_release.wait(30):
                raise TimeoutError("测试写入等待超时。")
            original_write(request)

        await asyncio.gather(*(
            executor.handle_start(machine_id) for machine_id in executor.machine_managers
        ))
        await self.wait_for_state(lambda: all(
            next(iter(machine_manager.sessions.values())).ocr_done
            for machine_manager in executor.machine_managers.values()
        ))
        with patch.object(executor.database, "write_record", hold_write):
            try:
                await asyncio.gather(*(
                    executor.handle_close(machine_id) for machine_id in executor.machine_managers
                ))
                await self.wait_for_state(lambda: (
                    executor.recovery.pending_count() == 3
                    and any(
                        session.commit_state == "RETRY_PENDING"
                        for machine_manager in executor.machine_managers.values()
                        for session in machine_manager.sessions.values()
                    )
                ))
                self.assertEqual(executor.database.queue.qsize(), 1)
                self.assertEqual(self.read_records(), [])
            finally:
                write_release.set()

            # 释放写库后，内存队列外的记录也会自动补交。
            await executor.wait_until_idle(10)
        self.assertEqual(len(self.read_records()), 3)
        self.assertEqual(executor.recovery.pending_count(), 0)

    async def test_conflicting_commit_is_retained_without_overwrite(self) -> None:
        executor = await self.start_executor(shutdown_timeout_ms=100)
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 模拟目标库检测到同一 Session 的不同内容。
        with patch.object(
            executor.database, "write_record", side_effect=ValueError("冲突"),
        ):
            with self.assertLogs(level="ERROR"):
                await executor.handle_close("M01")
                await self.wait_for_state(lambda: session.commit_state == "CONFLICT")
        status = executor.recovery.record_status(session.session_id)
        self.assertEqual(status["pending"]["blocked"], 1)
        self.assertEqual(self.read_records(), [])
        self.assertIn("COMMIT_INTEGRITY_CONFLICT", self.read_audit_reasons())

    async def test_ocr_worker_restart_preserves_closed_frame_job(self) -> None:
        executor = await self.start_executor(
            max_frames_per_session=1, simulated_ocr_delay_ms=600,
            storage_retry_interval_ms=50,
        )
        await executor.handle_start("M01")
        machine_manager = executor.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_state == "RUNNING")
        await executor.handle_close("M01")
        await self.wait_for_state(lambda: session.frequency_window_sealed)

        # 取消受监督的 OCR 工作任务，检查同一帧重试后仍只形成一条结果。
        worker = next(
            task for task in executor.worker_tasks
            if task.get_name() == "OCR"
        )
        with self.assertLogs(level="ERROR"):
            worker.cancel()
            await self.wait_for_state(lambda: "OCR" in machine_manager.device_faults)
        await executor.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "COMPLETE")
        self.assertEqual(record["session_id"], session.session_id)
        self.assertIn("OCR_WORKER_EXITED", self.read_audit_reasons())

    async def test_repeated_cycles_remain_independent(self) -> None:
        executor = await self.start_executor(
            max_frames_per_session=1, simulated_ocr_delay_ms=5,
        )

        # 连续执行多轮三机采集，并让每轮后台结果自行提交。
        for cycle_number in range(8):
            await asyncio.gather(*(
                executor.handle_start(machine_id) for machine_id in executor.machine_managers
            ))
            await self.wait_for_state(lambda: all(
                machine_manager.sessions[machine_manager.active_session_id].selected_frames
                and machine_manager.sessions[machine_manager.active_session_id].frequency_candidates
                for machine_manager in executor.machine_managers.values()
            ))
            await asyncio.gather(*(
                executor.handle_close(machine_id) for machine_id in executor.machine_managers
            ))
            await executor.wait_until_idle(20)
        await executor.wait_until_idle(20)
        records = self.read_records()
        self.assertEqual(len(records), 24)
        self.assertEqual(len({record["session_id"] for record in records}), 24)
        self.assertTrue(all(record["outcome"] == "COMPLETE" for record in records))


if __name__ == "__main__":
    unittest.main()
