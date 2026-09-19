"""验证重启清理、进程锁、事件审计和故障退出。"""

import asyncio
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_measurement_flow as flow_support
from app import App
from enums import OCRState, EventType
from models import MeasurementEvent
from recovery import serialize_value


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

    async def test_startup_clears_old_work_before_capacity_check(self) -> None:
        """验证启动清理旧积压和旧检查点表，同时保留历史审计。

        Args:
            无外部参数。

        Returns:
            None: 完成断言，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 保存一条历史审计后停止应用实例。
        app = await self.start_app()
        app.recovery.audit("PREVIOUS_RUN_AUDIT", machine_id="M01")
        await app.stop()

        # 直接写入旧版待提交数据，并创建旧版本检查点表。
        with closing(sqlite3.connect(app.configuration.recovery_path)) as connection:
            with connection:
                connection.executemany(
                    "INSERT INTO pending_records "
                    "(record_id, machine_id, record_type, payload_json, payload_hash, blocked) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ("old_measurement", "M01", "measurement", "{}", "hash-1", 0),
                        ("old_conflict", "M01", "measurement", "{}", "hash-2", 1),
                        ("old_rejection", "M01", "rejected_cycle", "{}", "hash-3", 0),
                    ),
                )
                connection.execute("CREATE TABLE machine_checkpoints (machine_id TEXT PRIMARY KEY, payload_json TEXT)")
                connection.execute("INSERT INTO machine_checkpoints VALUES (?, ?)", ("OLD_MACHINE", "{}"))

        # 使用低于旧积压数量的容量上限启动，确认先清理再检查容量。
        restarted = await self.restart_app(max_persistent_records=1)
        self.assertEqual(restarted.machine_managers["M01"].acceptance_state, "READY")

        # 确认旧待提交数据和检查点表已清理，机器档案只存在于内存。
        with closing(sqlite3.connect(restarted.configuration.recovery_path)) as connection:
            pending_count = connection.execute("SELECT COUNT(*) FROM pending_records").fetchone()[0]
            checkpoint_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'machine_checkpoints'"
            ).fetchone()
        self.assertEqual(pending_count, 0)
        self.assertIsNone(checkpoint_table)
        self.assertTrue(all(not manager.sessions for manager in restarted.machine_managers.values()))

        # 确认历史审计仍然保留。
        self.assertIn("PREVIOUS_RUN_AUDIT", self.read_audit_reasons())

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
        event = MeasurementEvent(EventType.MACHINE_STARTED, "M01")
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
            shutdown_timeout_ms=100,
        )
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        session = machine_manager.sessions[machine_manager.active_session_id]
        await self.wait_for_state(lambda: session.is_capture_finished)
        await app.handle_close("M01")
        await self.wait_for_state(lambda: session.frequency_window_sealed)
        self.assertNotEqual(session.ocr_state, OCRState.SUCCESS)

        # 在 OCR 完成前退出，再启动新的应用实例。
        with self.assertLogs(level="WARNING"):
            restarted = await self.restart_app(
                shutdown_timeout_ms=2000,
            )
        await restarted.wait_until_idle(10)

        # 确认旧档案和任务没有恢复，也没有生成旧周期的结果。
        restarted_machine_manager = restarted.machine_managers["M01"]
        self.assertEqual(restarted_machine_manager.sessions, {})
        self.assertIsNone(restarted_machine_manager.active_session_id)
        self.assertEqual(restarted_machine_manager.deadline_tasks, {})
        self.assertEqual(restarted_machine_manager.background_tasks, set())
        self.assertTrue(restarted.text_recognizer.batch_queue.empty())
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
import app as application_module
sys.path.insert(0, "tests")
from fake_mvs import FakeMvsSdk
application_module.load_mvs_sdk = FakeMvsSdk

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
        event = MeasurementEvent(EventType.MACHINE_STARTED, "M01")
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
            EventType.MACHINE_CLOSED, "M01", old_session_id,
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
            EventType.FREQUENCY_MEASURED, "M01", session.session_id,
            replace(measurement, value_hz=measurement.value_hz + 1),
        ))
        await app.handle_close("M01")
        await app.wait_until_idle(10)
        record = self.read_records()[0]
        self.assertEqual(record["outcome"], "REVIEW_REQUIRED")
        self.assertIn("AMBIGUOUS_MEASUREMENT", record["error_codes"])
        self.assertIn("MEASUREMENT_ID_CONFLICT", self.read_audit_reasons())

    async def test_device_fault_stops_all_machines(self) -> None:
        """验证单台设备故障停止全部机器并释放资源。

        Args:
            无外部参数。

        Returns:
            None  # 全部机器已停止，应用不可继续受理测量
        """
        # 启动两台机器并报告相机故障。
        app = await self.start_app()
        await app.handle_start("M01")
        await app.handle_start("M02")
        app.report_failure(OSError("相机断开"), "machine_id=M01 camera_id=CAM01")

        # 等待统一清理，确认其他机器也已停止。
        await app.stop()
        self.assertFalse(app.accepting_signals)
        self.assertTrue(app.camera_sdk.closed)
        self.assertTrue(all(not manager.sessions for manager in app.machine_managers.values()))

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

    async def test_repeated_cycles_remain_independent(self) -> None:
        app = await self.start_app(max_frames_per_session=1)

        # 连续执行多轮三机采集，并让每轮后台结果自行提交。
        for cycle_number in range(8):
            await asyncio.gather(*(
                app.handle_start(machine_id) for machine_id in app.machine_managers
            ))
            await self.wait_for_state(lambda: all(
                machine_manager.sessions[machine_manager.active_session_id].images_for_final_selection
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
