"""按验收场景验证跨轮回调、设备故障和持久化边界。"""

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_measurement_flow as flow_support
import test_recovery_and_faults as recovery_support
from enums import OCRState, FrequencyState, EventType
from mvs_sdk import CameraFrame
from models import CapturedFrame, CaptureResult, FrequencyMeasurement, MeasurementEvent
from database import serialize_value


class AcceptanceScenarioTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow_support.MeasurementFlowTests.asyncSetUp
    asyncTearDown = flow_support.MeasurementFlowTests.asyncTearDown
    start_app = flow_support.MeasurementFlowTests.start_app
    wait_for_state = flow_support.MeasurementFlowTests.wait_for_state
    read_records = flow_support.MeasurementFlowTests.read_records
    read_abnormal_event_reasons = recovery_support.RecoveryAndFaultTests.read_abnormal_event_reasons

    async def start_controlled_app(self, **overrides):
        """暂停自动图像与频率输入，由测试按确定顺序发布事件。"""
        async def wait_for_cancellation():
            """等待取消以保持频率监听任务存活。

            Args:
                无外部参数。

            Returns:
                None  # 仅在任务取消时结束
            """
            await asyncio.Future()

        def start_controlled_capture(camera, session_id, capture_start_time):
            """登记不自动交付图片的测试采集任务。

            Args:
                camera: 本机相机适配器。
                session_id: 测量周期编号。
                capture_start_time: 采集窗口起点。

            Returns:
                None  # 任务已创建，结束后清理引用，图片由测试主动交付
            """
            # 模拟启动成功后的任务引用及结束回调。
            camera.delivery_task = asyncio.create_task(asyncio.sleep(0))
            camera.delivery_task.add_done_callback(camera.handle_capture_task_finished)

        for target in (
            "camera.SessionCamera.start_capture",
            "fake_frequency.FakeFrequency.listen_measurements",
        ):
            replacement = (
                patch(target, side_effect=wait_for_cancellation)
                if target.endswith("listen_measurements")
                else patch(target, autospec=True, side_effect=start_controlled_capture)
            )
            replacement.start()
            self.addCleanup(replacement.stop)
        # 为人为暂停的识别步骤设置独立测试期限。
        settings = {
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
        boundary = asyncio.get_running_loop().time()
        frame = CameraFrame("serial", 1, 0, 0, boundary, 2, 2, 17301505, 0, b"1234")
        measurement = FrequencyMeasurement(
            session.session_id, session.frequency_meter_serial, value_hz,
        )

        # 依次确认图像与频率已被业务层接收。
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_COMPLETED, session.machine_id, session.session_id,
            CaptureResult(frames=(frame,)),
        ))
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, session.machine_id, session.session_id, measurement,
        ))
        return frame, measurement

    async def close_controlled_cycle(self, session):
        """正常关闭当前周期，并发布图像和频率封口事件。"""
        await self.app.handle_close(session.machine_id)

    async def test_01_bound_sources_reject_cross_machine_data(self):
        # 同时启动三台机器并取得各自的活动档案。
        app = await self.start_controlled_app()
        await asyncio.gather(*(
            app.handle_start(machine_id) for machine_id in app.machine_managers
        ))
        sessions = [
            machine_manager.current_session
            for machine_manager in app.machine_managers.values()
        ]
        self.assertEqual(len({session.session_id for session in sessions}), 3)

        # 检查配置绑定，并向同一机器注入错误频率通道。
        for session in sessions:
            machine_number = int(session.machine_id[1:])
            self.assertEqual(session.camera_serial, f"SERIAL{machine_number:02}")
            self.assertEqual(session.frequency_meter_serial, f"FREQ{machine_number:02}")
            frame, measurement = await self.supply_valid_inputs(session)
            await self.publish_and_wait(MeasurementEvent(
                EventType.FREQUENCY_MEASURED, session.machine_id, session.session_id,
                replace(measurement, frequency_meter_serial="wrong-frequency"),
            ))
            await self.wait_for_state(lambda: session.ocr_state == OCRState.SUCCESS)
            self.assertEqual(session.ocr_result.selected_frames[0].camera_serial, session.camera_serial)
            self.assertIn(measurement, session.frequency_candidates.values())

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
                record["frequency_candidates"][0]["frequency_meter_serial"],
                record["frequency_meter_serial"],
            )
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_abnormal_event_reasons())

    async def test_06_repeated_start_input_creates_one_cycle(self):
        # 并发重复启动后，只为唯一活动周期完成一次测量。
        app = await self.start_controlled_app()
        await asyncio.gather(*(app.handle_start("M01") for repeat in range(40)))
        machine_manager = app.machine_managers["M01"]
        self.assertIsNotNone(machine_manager.current_session)
        session = machine_manager.current_session
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
        first_session = machine_manager.current_session
        await self.supply_valid_inputs(first_session)
        close_event = MeasurementEvent(EventType.MACHINE_CLOSED, "M01", first_session.session_id)
        await self.publish_and_wait(close_event)
        await app.wait_until_idle(3)

        # 新轮启动后重放原关闭事件，再用新事件身份重发旧轮关闭。
        await app.handle_start("M01")
        second_session = machine_manager.current_session
        await self.supply_valid_inputs(second_session)
        await self.publish_and_wait(close_event)
        await self.publish_and_wait(replace(close_event, event_id=uuid4().hex))
        self.assertEqual(machine_manager.current_session.session_id, second_session.session_id)
        self.assertIsNone(second_session.capture_stop_time)
        self.assertFalse(second_session.frequency_window_sealed)
        self.assertIn("CLOSE_SESSION_MISMATCH", self.read_abnormal_event_reasons())

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
            session = machine_manager.current_session
            frame, measurement = await self.supply_valid_inputs(session, 42.0)
            expected_measurements[session.session_id] = measurement.value_hz
            await self.close_controlled_cycle(session)
            await app.wait_until_idle(10)
        await app.wait_until_idle(10)

        # 相同数值按周期编号分别保存，各自成为本轮最终频率。
        records = self.read_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            {record["session_id"] for record in records}, set(expected_measurements),
        )
        for record in records:
            self.assertEqual(record["outcome"], "COMPLETE")
            self.assertEqual(record["final_frequency_hz"], 42.0)
            self.assertEqual(
                record["final_frequency_hz"],
                expected_measurements[record["session_id"]],
            )

    async def test_10_old_cycle_reading_does_not_enter_new_cycle(self):
        """验证旧周期读数不会进入新周期，无有效读数的新周期不入库。

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
        first_session = machine_manager.current_session
        frame, old_measurement = await self.supply_valid_inputs(first_session)
        await self.close_controlled_cycle(first_session)
        await app.wait_until_idle(3)
        await app.handle_start("M01")
        second_session = machine_manager.current_session

        # 保留旧读数的周期归属，向机器队列交付迟到事件。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", first_session.session_id,
            old_measurement,
        ))
        self.assertEqual(second_session.measurement_frequencies, [])

        # 仅补充新周期图像，关闭后确认缺少有效频率的周期未入库。
        await self.publish_and_wait(MeasurementEvent(
            EventType.CAPTURE_COMPLETED, "M01", second_session.session_id,
            CaptureResult(
                frames=(replace(frame, received_monotonic=asyncio.get_running_loop().time()),),
            ),
        ))
        await self.close_controlled_cycle(second_session)
        await app.wait_until_idle(10)
        self.assertEqual(second_session.frequency_state, FrequencyState.FAILED)
        self.assertEqual(
            [record["session_id"] for record in self.read_records()],
            [first_session.session_id],
        )

    async def test_11_unassigned_delayed_frequency_is_recorded_without_guessing(self):
        # 保留关闭后尚未封口的旧轮，同时启动新轮。
        app = await self.start_controlled_app()
        await app.handle_start("M01")
        machine_manager = app.machine_managers["M01"]
        first_session = machine_manager.current_session
        frame, measurement = await self.supply_valid_inputs(first_session)
        await app.handle_close("M01")
        await app.wait_until_idle(3)
        await app.handle_start("M01")
        second_session = machine_manager.current_session

        # 同时存在旧轮和新轮时，注入没有周期归属的延迟读数。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", None,
            replace(measurement, session_id=""),
        ))
        self.assertEqual(len(first_session.frequency_candidates), 1)
        self.assertEqual(second_session.frequency_candidates, {})
        self.assertEqual(machine_manager.current_session.session_id, second_session.session_id)
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_abnormal_event_reasons())

        # 为两轮分别完成已有的有效输入，不把无归属读数写入任何记录。
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
        first_session = machine_manager.current_session
        frame, measurement = await self.supply_valid_inputs(first_session)
        await app.handle_close("M01")
        await app.wait_until_idle(3)
        await app.handle_start("M01")
        second_session = machine_manager.current_session

        # 外层事件声明旧轮、测量声明新轮，保持旧轮冲突待复核。
        await self.publish_and_wait(MeasurementEvent(
            EventType.FREQUENCY_MEASURED, "M01", first_session.session_id,
            replace(measurement, session_id=second_session.session_id),
        ))
        self.assertEqual(first_session.frequency_state, FrequencyState.FAILED)
        self.assertEqual(second_session.frequency_candidates, {})
        self.assertEqual(machine_manager.current_session.session_id, second_session.session_id)
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

    async def test_15_io_loss_stops_application(self):
        """验证共享 IO 故障停止应用且不伪造正常结果。

        Args:
            无外部参数。

        Returns:
            None  # 全部测量已停止且无异常记录入库
        """
        # 启动机器并通知应用发生 IO 故障。
        app = await self.start_app()
        for machine_id in app.machine_managers:
            await app.handle_start(machine_id)
        app.report_failure(OSError("IO 连接断开"))

        # 等待退出，确认不会继续恢复或接收测量。
        await app.stop()
        self.assertFalse(app.accepting_signals)
        self.assertTrue(app.camera_sdk.closed)
        self.assertEqual(self.read_records(), [])


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
# 添加子进程使用的后端模块目录。
sys.path.insert(0, "src")
from configuration import load_configuration
from enums import OCRState
from app import App
import app as application_module
sys.path.insert(0, "tests")
from fake_mvs import FakeMvsSdk
application_module.load_mvs_sdk = FakeMvsSdk

async def crash_after_close():
    # 将子进程的设备写入业务库，再通过正式配置入口读取。
    import json
    import sqlite3
    from contextlib import closing
    from dataclasses import replace
    from repo.machine_repo import MachineRepo
    settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    database_path = Path(settings["database_path"])
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
    repository = MachineRepo(database_path)
    for machine in settings["machines"]:
        repository.insert(machine["machine_id"], machine["camera_serial"], machine["frequency_meter_serial"])
    configuration = load_configuration(Path(sys.argv[1]))
    # 为崩溃测试显式注入频率替身读数。
    configuration = replace(configuration, machines=tuple(
        replace(machine, simulated_frequencies_hz=tuple(original["simulated_frequencies_hz"]))
        for machine, original in zip(configuration.machines, settings["machines"])
    ))
    app = App(configuration)
    await app.start()
    await app.text_recognizer.processing_lock.acquire()
    await app.handle_start("1")
    machine_manager = app.machine_managers["1"]
    session = machine_manager.current_session
    while session.ocr_state != OCRState.RUNNING:
        app.state_changed.clear()
        await app.state_changed.wait()
    assert machine_manager.recognition_task
    await app.handle_close("1")
    while not session.frequency_window_sealed:
        app.state_changed.clear()
        await app.state_changed.wait()
    await machine_manager.queue.join()
    assert session.measurement_frequencies and session.ocr_state != OCRState.SUCCESS
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
        self.assertEqual(restarted.machine_managers["M01"].current_session, None)
        self.assertFalse(restarted.machine_managers["M01"].recognition_task)
        self.assertEqual(self.read_records(), [])



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
        session = machine_manager.current_session
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

        # 冲突测量不替换原候选，并记录无效频率和异常原因。
        self.assertEqual(session.frequency_state, FrequencyState.FAILED)
        self.assertIn("AMBIGUOUS_MEASUREMENT", session.errors)
        self.assertIn(measurement, session.frequency_candidates.values())
        self.assertIn("AMBIGUOUS_MEASUREMENT", self.read_abnormal_event_reasons())


if __name__ == "__main__":
    unittest.main()
