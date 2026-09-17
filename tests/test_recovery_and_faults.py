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
from app import App
from models import MeasurementEvent
from recovery import serialize_value
from database import DatabaseRequest


class RecoveryAndFaultTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records

    def read_audit_reasons(self) -> list[str]:
        """读取恢复库中的异常审计原因。"""
        connection = sqlite3.connect(self.app.configuration.recovery_path)
        with closing(connection):
            rows = connection.execute("SELECT reason FROM audit_entries").fetchall()
        return [row[0] for row in rows]

    async def restart_app(self, **overrides) -> App:
        """使用同一数据库和证据目录重启系统。"""
        configuration = replace(self.app.configuration, **overrides)
        await self.app.stop()
        self.app = App(configuration)
        await self.app.start()
        return self.app

    async def test_database_failure_is_automatically_retried(self) -> None:
        app = await self.start_app(
            storage_retry_attempts=1, storage_retry_interval_ms=100,
        )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 暂时禁止最终库写入，确认记录先进入本地待提交区。
        with patch.object(
            app.database, "write_record", side_effect=OSError("模拟断库"),
        ):
            with self.assertLogs(level="ERROR"):
                await app.handle_close("M01")
                await self.wait_for_state(
                    lambda: session.commit_state == "RETRY_PENDING",
                )
            self.assertEqual(app.recovery.pending_count(), 1)
            frozen_payload = session.frozen_payload

        # 故障解除后自动补交，不调用手动重试入口。
        await app.wait_until_idle(10)
        self.assertEqual(self.read_records()[0], json.loads(frozen_payload))
        self.assertEqual(app.recovery.pending_count(), 0)

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
        app = await self.start_app(
            storage_retry_attempts=1, shutdown_timeout_ms=100,
        )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 持续写入失败后退出，保存同一份冻结内容。
        with patch.object(
            app.database, "write_record", side_effect=OSError("模拟断库"),
        ):
            with self.assertLogs(level="WARNING"):
                await app.handle_close("M01")
                await self.wait_for_state(
                    lambda: session.commit_state == "RETRY_PENDING",
                )
                await app.stop()
        # 确认退出时旧结果仍在待提交区，并保留证据路径。
        self.assertEqual(app.recovery.pending_count(), 1)
        evidence_paths = [frame.image_path for frame in session.selected_frames.values()]
        restarted = await self.restart_app(shutdown_timeout_ms=2000)

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
        """验证启动清理旧积压并移除旧检查点表，保留历史结果与审计。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 保存一轮历史结果和审计，然后停止应用实例。
        app = await self.start_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        await app.handle_close("M01")
        await app.wait_until_idle(10)
        expected_records = self.read_records()
        app.recovery.audit("PREVIOUS_RUN_AUDIT", machine_id="M01")
        await app.stop()

        # 登记普通、冲突和未受理类型的旧待提交记录。
        for record_id, record_type in (
            ("old_measurement", "measurement"),
            ("old_conflict", "measurement"),
            ("old_rejection", "rejected_cycle"),
        ):
            app.recovery.stage_record(DatabaseRequest("M01", record_id, "{}", record_id, record_type))
        app.recovery.delay_record("old_conflict", 0, blocked=True)

        # 创建旧版本检查点表并保存旧机器状态。
        with closing(sqlite3.connect(app.configuration.recovery_path)) as connection:
            with connection:
                connection.execute("CREATE TABLE machine_checkpoints (machine_id TEXT PRIMARY KEY, payload_json TEXT)")
                connection.execute("INSERT INTO machine_checkpoints VALUES (?, ?)", ("OLD_MACHINE", "{}"))

        # 使用低于旧积压数量的容量上限启动，确认先清理再检查容量。
        restarted = await self.restart_app(max_persistent_records=1)
        self.assertEqual(restarted.recovery.pending_count(), 0)
        self.assertEqual(restarted.machine_managers["M01"].acceptance_state, "READY")

        # 确认旧检查点表已删除，机器档案只存在于内存。
        with closing(sqlite3.connect(restarted.configuration.recovery_path)) as connection:
            checkpoint_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'machine_checkpoints'"
            ).fetchone()
        self.assertIsNone(checkpoint_table)
        self.assertTrue(all(not manager.sessions for manager in restarted.machine_managers.values()))

        # 确认历史结果、提交身份、审计和证据仍然保留。
        self.assertEqual(self.read_records(), expected_records)
        self.assertTrue(restarted.recovery.record_status(session.session_id)["committed"])
        self.assertIn("PREVIOUS_RUN_AUDIT", self.read_audit_reasons())
        self.assertTrue(all(Path(image_path).is_file() for image_path in expected_records[0]["evidence_refs"]))

    async def test_machine_state_stays_in_memory_without_event_receipts(self) -> None:
        """验证机器状态只保留在内存，且不创建事件去重表。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 处理启动事件并取得内存中的活动档案。
        app = await self.start_app()
        event = MeasurementEvent("MachineStarted", "M01")
        await app.publish_event(event)
        machine_manager = app.machine_managers["M01"]
        await machine_manager.queue.join()
        session_id = machine_manager.active_session_id
        self.assertIn(session_id, machine_manager.sessions)

        # 确认运行期间不创建事件去重表和机器检查点表。
        with closing(sqlite3.connect(app.configuration.recovery_path)) as connection:
            receipt_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'event_receipts'"
            ).fetchone()
            checkpoint_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'machine_checkpoints'"
            ).fetchone()
        self.assertIsNone(receipt_table)
        self.assertIsNone(checkpoint_table)

        # 活动周期内重放启动事件，确认机器状态阻止创建新周期。
        await app.publish_event(event)
        await machine_manager.queue.join()
        self.assertEqual(machine_manager.active_session_id, session_id)
        self.assertNotIn("DUPLICATE", self.read_audit_reasons())

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
        app = await self.start_app(initial_machine_state="UNKNOWN")
        machine_manager = app.machine_managers["M01"]
        await app.handle_start("M01")
        self.assertEqual(machine_manager.sessions, {})
        self.assertEqual(machine_manager.acceptance_state, "FAULT")
        await app.handle_close("M01")
        self.assertEqual(machine_manager.acceptance_state, "READY")
        await app.handle_start("M01")
        self.assertIsNotNone(machine_manager.active_session_id)

        # 其他机器同步为运行中时仍等待关闭，不创建半轮测量。
        await app.synchronize_machine("M02", "OPEN")
        await app.handle_start("M02")
        self.assertEqual(app.machine_managers["M02"].acceptance_state, "WAIT_CYCLE_RESET")
        self.assertEqual(app.machine_managers["M02"].sessions, {})
        await app.handle_close("M02")
        self.assertEqual(app.machine_managers["M02"].acceptance_state, "READY")

        # 同步为关闭状态后允许下一次启动。
        await app.synchronize_machine("M03", "CLOSED")
        self.assertEqual(app.machine_managers["M03"].acceptance_state, "READY")
        await app.handle_start("M03")
        self.assertIsNotNone(app.machine_managers["M03"].active_session_id)

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
        app = await self.start_app(
            simulated_ocr_delay_ms=1200, shutdown_timeout_ms=100,
        )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.capture_sealed)
        await app.handle_close("M01")
        await self.wait_for_state(lambda: session.frequency_window_sealed)
        self.assertFalse(session.ocr_done)

        # 在 OCR 完成前退出，再启动新的应用实例。
        with self.assertLogs(level="WARNING"):
            restarted = await self.restart_app(
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
        app = await self.start_app()
        configuration = app.configuration
        await app.stop()
        configuration_path = self.output_directory / "crash-configuration.json"
        configuration_path.write_text(
            json.dumps(serialize_value(configuration)), encoding="utf-8",
        )

        # 子进程确认启动事件处理完成后直接异常退出。
        script = """
import asyncio
import os
import sys
from pathlib import Path
from configuration import load_configuration
from app import App

async def crash_after_start():
    app = App(load_configuration(Path(sys.argv[1])))
    await app.start()
    await app.handle_start('M01')
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
        restarted = await self.restart_app(initial_machine_state="OPEN")
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

    async def test_event_identity_does_not_block_start_after_restart(self) -> None:
        """验证重启后不会按旧事件编号拦截有效启动。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 完成首次运行的测量并保留原启动事件。
        app = await self.start_app()
        event = MeasurementEvent("MachineStarted", "M01")
        await app.publish_event(event)
        await app.machine_managers["M01"].queue.join()
        await app.handle_close("M01")
        await app.wait_until_idle(10)

        # 重放上次运行已确认的启动事件。
        restarted = await self.restart_app()
        await restarted.publish_event(event)
        await restarted.machine_managers["M01"].queue.join()
        self.assertIsNotNone(restarted.machine_managers["M01"].active_session_id)
        self.assertNotIn("DUPLICATE", self.read_audit_reasons())
        self.assertEqual(len(self.read_records()), 1)

    async def test_stale_close_cannot_close_a_new_cycle(self) -> None:
        app = await self.start_app()
        await app.handle_start("M01")
        old_session_id = app.machine_managers["M01"].active_session_id
        await app.handle_close("M01")
        await app.handle_start("M01")
        new_session_id = app.machine_managers["M01"].active_session_id

        # 把带旧 Session 身份的关闭事件送回业务队列。
        await app.publish_event(MeasurementEvent(
            "MachineClosed", "M01", old_session_id,
        ))
        await app.machine_managers["M01"].queue.join()
        self.assertEqual(app.machine_managers["M01"].active_session_id, new_session_id)
        self.assertIn("CLOSE_SESSION_MISMATCH", self.read_audit_reasons())

    async def test_frequency_identity_conflict_requires_review(self) -> None:
        app = await self.start_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: bool(session.frequency_candidates))
        measurement = next(iter(session.frequency_candidates.values()))

        # 对同一测量身份注入不同数值。
        await app.publish_event(MeasurementEvent(
            "FrequencyMeasured", "M01", session.session_id,
            replace(measurement, value_hz=measurement.value_hz + 1),
        ))
        await app.handle_close("M01")
        await app.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("AMBIGUOUS_MEASUREMENT", record["error_codes"])
        self.assertIn("MEASUREMENT_ID_CONFLICT", self.read_audit_reasons())

    async def test_device_fault_isolated_to_its_machine(self) -> None:
        app = await self.start_app()
        await app.handle_start("M01")
        await app.handle_start("M02")
        second_session_id = app.machine_managers["M02"].active_session_id

        # 单台相机故障只中断绑定机器。
        await app.report_device_health("CAM01", False)
        self.assertEqual(app.machine_managers["M01"].acceptance_state, "FAULT")
        self.assertEqual(app.machine_managers["M02"].active_session_id, second_session_id)
        await app.report_device_health("CAM01", True)
        self.assertEqual(app.machine_managers["M01"].acceptance_state, "WAIT_CYCLE_RESET")
        await app.synchronize_machine("M01", "CLOSED")
        self.assertEqual(app.machine_managers["M01"].acceptance_state, "READY")

    async def test_initial_open_state_does_not_create_midcycle_session(self) -> None:
        app = await self.start_app(initial_machine_state="OPEN")
        await app.handle_start("M01")
        self.assertIsNone(app.machine_managers["M01"].active_session_id)
        await app.handle_close("M01")
        await app.handle_start("M01")
        self.assertIsNotNone(app.machine_managers["M01"].active_session_id)

    async def test_disk_capacity_blocks_new_cycles_and_recovers(self) -> None:
        app = await self.start_app(minimum_free_disk_bytes=10**30)
        machine_manager = app.machine_managers["M01"]
        await self.wait_for_state(lambda: machine_manager.acceptance_state == "DEGRADED")
        await app.handle_start("M01")
        self.assertIsNone(machine_manager.active_session_id)

        # 容量恢复后仍须确认被拒收周期已经关闭。
        app.configuration = replace(
            app.configuration, minimum_free_disk_bytes=0,
        )
        await self.wait_for_state(lambda: machine_manager.capacity_available)
        self.assertTrue(machine_manager.waiting_cycle_reset)
        await app.handle_close("M01")
        await app.handle_start("M01")
        self.assertIsNotNone(machine_manager.active_session_id)

    async def test_second_process_instance_cannot_share_recovery_store(self) -> None:
        app = await self.start_app()
        second_app = App(app.configuration)
        with self.assertLogs(level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "初始化失败"):
                await second_app.start()
        self.assertTrue(app.accepting_signals)

    async def test_frame_retry_keeps_attempt_and_is_not_counted_twice(self) -> None:
        app = await self.start_app(max_frames_per_session=1)
        original_recognize = app.ocr.recognize_frame
        attempt_count = 0

        # 第一帧第一次识别失败，第二次成功。
        async def fail_once(job):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                raise OSError("模拟 OCR 临时失败")
            return await original_recognize(job)

        with patch.object(app.ocr, "recognize_frame", fail_once):
            with self.assertLogs("ocr", level="ERROR"):
                await app.handle_start("M01")
                machine_manager = app.machine_managers["M01"]
                session = machine_manager.sessions[machine_manager.active_session_id]
                await self.wait_for_state(lambda: session.ocr_done)
            await app.handle_close("M01")
            await app.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(attempt_count, 2)
        self.assertEqual(len(record["ocr_jobs"]), 1)
        self.assertEqual(next(iter(record["ocr_jobs"].values()))["attempt"], 2)
        self.assertEqual(record["ordered_lines"], ["MODEL 1", "SAME", "SAME"])

    async def test_database_unavailable_at_startup_uses_local_spool(self) -> None:
        blocked_directory = self.output_directory / "blocked"
        blocked_directory.write_text("模拟不可用路径", encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            app = await self.start_app(
                database_path=blocked_directory / "measurements.sqlite3",
                recovery_database_path=self.output_directory / "recovery.sqlite3",
                storage_retry_attempts=1, storage_retry_interval_ms=100,
            )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)
        with self.assertLogs(level="ERROR"):
            await app.handle_close("M01")
            await self.wait_for_state(lambda: session.commit_state == "RETRY_PENDING")
        self.assertEqual(app.recovery.pending_count(), 1)

        # 恢复最终库路径后自动创建数据库并补交。
        blocked_directory.unlink()
        blocked_directory.mkdir()
        await app.wait_until_idle(10)
        self.assertEqual(self.read_records()[0]["outcome"], "COMPLETE")

    async def test_storage_queue_full_keeps_all_records_durable(self) -> None:
        app = await self.start_app(
            storage_queue_capacity=1, max_frames_per_session=1,
        )
        original_write = app.database.write_record
        write_release = threading.Event()

        # 暂停真实写库，等待三份记录全部进入持久化待提交区。
        def hold_write(request):
            if not write_release.wait(30):
                raise TimeoutError("测试写入等待超时。")
            original_write(request)

        await asyncio.gather(*(
            app.handle_start(machine_id) for machine_id in app.machine_managers
        ))
        await self.wait_for_state(lambda: all(
            next(iter(machine_manager.sessions.values())).ocr_done
            for machine_manager in app.machine_managers.values()
        ))
        with patch.object(app.database, "write_record", hold_write):
            try:
                await asyncio.gather(*(
                    app.handle_close(machine_id) for machine_id in app.machine_managers
                ))
                await self.wait_for_state(lambda: (
                    app.recovery.pending_count() == 3
                    and any(
                        session.commit_state == "RETRY_PENDING"
                        for machine_manager in app.machine_managers.values()
                        for session in machine_manager.sessions.values()
                    )
                ))
                self.assertEqual(app.database.queue.qsize(), 1)
                self.assertEqual(self.read_records(), [])
            finally:
                write_release.set()

            # 释放写库后，内存队列外的记录也会自动补交。
            await app.wait_until_idle(10)
        self.assertEqual(len(self.read_records()), 3)
        self.assertEqual(app.recovery.pending_count(), 0)

    async def test_conflicting_commit_is_retained_without_overwrite(self) -> None:
        app = await self.start_app(shutdown_timeout_ms=100)
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_done)

        # 模拟目标库检测到同一 Session 的不同内容。
        with patch.object(
            app.database, "write_record", side_effect=ValueError("冲突"),
        ):
            with self.assertLogs(level="ERROR"):
                await app.handle_close("M01")
                await self.wait_for_state(lambda: session.commit_state == "CONFLICT")
        status = app.recovery.record_status(session.session_id)
        self.assertEqual(status["pending"]["blocked"], 1)
        self.assertEqual(self.read_records(), [])
        self.assertIn("COMMIT_INTEGRITY_CONFLICT", self.read_audit_reasons())

    async def test_ocr_worker_restart_preserves_closed_frame_job(self) -> None:
        app = await self.start_app(
            max_frames_per_session=1, simulated_ocr_delay_ms=600,
            storage_retry_interval_ms=50,
        )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.ocr_state == "RUNNING")
        await app.handle_close("M01")
        await self.wait_for_state(lambda: session.frequency_window_sealed)

        # 取消受监督的 OCR 工作任务，检查同一帧重试后仍只形成一条结果。
        worker = next(
            task for task in app.worker_tasks
            if task.get_name() == "OCR"
        )
        with self.assertLogs(level="ERROR"):
            worker.cancel()
            await self.wait_for_state(lambda: "OCR" in machine_manager.device_faults)
        await app.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "COMPLETE")
        self.assertEqual(record["session_id"], session.session_id)
        self.assertIn("OCR_WORKER_EXITED", self.read_audit_reasons())

    async def test_repeated_cycles_remain_independent(self) -> None:
        app = await self.start_app(
            max_frames_per_session=1, simulated_ocr_delay_ms=5,
        )

        # 连续执行多轮三机采集，并让每轮后台结果自行提交。
        for cycle_number in range(8):
            await asyncio.gather(*(
                app.handle_start(machine_id) for machine_id in app.machine_managers
            ))
            await self.wait_for_state(lambda: all(
                machine_manager.sessions[machine_manager.active_session_id].selected_frames
                and machine_manager.sessions[machine_manager.active_session_id].frequency_candidates
                for machine_manager in app.machine_managers.values()
            ))
            await asyncio.gather(*(
                app.handle_close(machine_id) for machine_id in app.machine_managers
            ))
            await app.wait_until_idle(20)
        await app.wait_until_idle(20)
        records = self.read_records()
        self.assertEqual(len(records), 24)
        self.assertEqual(len({record["session_id"] for record in records}), 24)
        self.assertTrue(all(record["outcome"] == "COMPLETE" for record in records))


if __name__ == "__main__":
    unittest.main()
