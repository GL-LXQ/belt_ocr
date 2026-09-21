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

from config_util import MachineConfig, AppConfig
from app import App
from enums import OCRState
from models import MeasurementEvent, OCRResult
from fake_mvs import FakeMvsSdk
from fake_frequency import FakeFrequency


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

        # 以测试机器替身提供频率输入，生产适配器保留黑盒接口。
        frequency_replacement = patch("app.FrequencyAdapter", side_effect=FakeFrequency)
        frequency_replacement.start()
        self.addCleanup(frequency_replacement.stop)

    async def asyncTearDown(self) -> None:
        # 关闭测试应用实例并释放临时文件。
        if self.app is not None:
            await asyncio.wait_for(self.app.stop(), 10)
        self.temporary_directory.cleanup()

    async def start_app(self, **overrides: object) -> App:
        """创建三台机器的测试配置并启动应用实例。"""
        machines = tuple(
            MachineConfig(
                machine_id=f"M{machine_number:02}",
                frequency_meter_serial=f"FREQ{machine_number:02}",
                camera_serial=f"SERIAL{machine_number:02}",
                simulated_frequencies_hz=(40.0, 40.0, 43.0),
            )
            for machine_number in range(1, 4)
        )
        config = AppConfig(
            machines=machines,
            database_path=self.output_directory / "measurements.sqlite3",
            evidence_directory=self.output_directory / "evidence",
            mvs_development_directory=Path("test-sdk"),
            capture_window_ms=180,
            frequency_interval_ms=150,
            max_cycle_open_ms=30000,
            ocr_result_timeout_ms=20000,
            shutdown_timeout_ms=2000,
        )
        self.app = App(replace(config, **overrides))
        # 仅在测试中提供确定性识别和终选，生产黑盒继续保持未实现。
        self.app.text_recognizer.recognize_images = lambda images: [{"blocks": []} for image in images]
        self.app.text_recognizer.generate_final_text_and_images = lambda results, frames: OCRResult(
            ordered_lines=("MODEL",),
            selected_frames=(frames[0],),
            line_frame_ids=((frames[0].frame_id,),),
        )
        await self.app.start()
        return self.app

    async def wait_for_state(self, predicate, timeout_seconds: float = 3) -> None:
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
                    }
                    for machine_manager in self.app.machine_managers.values()
                    for session in (machine_manager.current_session,) if session is not None
                ]
                workers = [
                    (task.get_name(), task.done(), str(task.get_coro()))
                    for task in self.app.worker_tasks
                ]
                self.fail(f"等待业务状态超时：{states}；工作任务：{workers}")

    def read_records(self) -> list[dict]:
        """读取测量表业务字段并解码列表。

        Args:
            无外部参数。

        Returns:
            list[dict]: 测量记录列表。
            返回示例：
                [{
                    "session_id": "session-1",  # 周期编号
                    "machine_id": "M01",  # 机器编号
                    "start_time": "2026-09-20T00:00:00+00:00",  # 开始时间
                    "finish_time": "2026-09-20T00:00:01+00:00",  # 结算时间
                    "ordered_lines": ["MODEL"],  # 识别文字
                    "final_frequency_hz": 42.0,  # 最终频率
                    "evidence_refs": ["frame.bmp"],  # 图片路径
                    "measurement_frequencies": [],  # 频率明细
                }]
        """
        # 按列名读取测量记录。
        connection = sqlite3.connect(self.app.config.database_path)
        connection.row_factory = sqlite3.Row
        with closing(connection):
            records = connection.execute(
                "SELECT * FROM measurements ORDER BY start_time"
            ).fetchall()
        # 解码各业务列表列。
        measurements = [dict(record) for record in records]
        for measurement in measurements:
            for field_name in ("ordered_lines", "evidence_refs", "measurement_frequencies"):
                measurement[field_name] = json.loads(measurement[field_name])
        return measurements


    async def test_three_machines_save_independent_records(self):
        """验证三台机器分别收集完整帧并在 CLOSE 后保存结果。

        Args:
            无外部参数。

        Returns:
            None  # 三机身份、图片与频率归属已验证
        """
        app = await self.start_app(capture_window_ms=400)
        await asyncio.gather(*(app.handle_start(identity) for identity in app.machine_managers))
        await self.wait_for_state(lambda: all(
            manager.current_session.ocr_state == OCRState.SUCCESS
            for manager in app.machine_managers.values()
        ))
        # 检查每台机器的采集帧数。
        for manager in app.machine_managers.values():
            self.assertGreater(
                manager.current_session.capture_summary["retained_frame_count"],
                5,
            )
        # OCR 完成时图片仍在内存，没有提前写入。
        self.assertEqual(self.read_records(), [])
        self.assertEqual(list(self.output_directory.rglob("*.bmp")), [])
        await asyncio.gather(*(app.handle_close(identity) for identity in app.machine_managers))
        await app.wait_until_idle()
        records = self.read_records()
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(record["ordered_lines"], ["MODEL"])
            self.assertTrue(all(Path(path).read_bytes().startswith(b"BM") for path in record["evidence_refs"]))
            self.assertTrue(all(
                value["session_id"] == record["session_id"] for value in record["measurement_frequencies"]
            ))

    async def test_early_close_and_sequential_cycles_keep_ownership(self):
        """验证提前关闭和立即重启不会混用相机帧与 OCR 结果。

        Args:
            无外部参数。

        Returns:
            None  # 两轮各自产生一次正确归属的记录
        """
        app = await self.start_app(capture_window_ms=1000)
        manager = app.machine_managers["M01"]
        for cycle_number in range(2):
            await app.handle_start("M01")
            session = manager.current_session
            await self.wait_for_state(lambda: bool(session.measurement_frequencies))
            await app.handle_close("M01")
            await app.wait_until_idle()
        await app.wait_until_idle()
        records = self.read_records()
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["session_id"], records[1]["session_id"])
        for record in records:
            self.assertTrue(all(record["session_id"] in path for path in record["evidence_refs"]))

    async def test_unimplemented_model_fails_without_waiting_for_timeout(self):
        """验证正式占位模型立即失败且等待真实 CLOSE 释放现场身份。

        Args:
            无外部参数。

        Returns:
            None  # 未实现算法未伪造成功或写入记录
        """
        from text_recognition import TextRecognizer
        from enums import SessionState

        app = await self.start_app()
        app.text_recognizer.recognize_images = TextRecognizer().recognize_images
        await app.handle_start("M01")
        manager = app.machine_managers["M01"]
        session = manager.current_session
        await self.wait_for_state(lambda: session.state == SessionState.FAILED)
        self.assertIn("OCR_MODEL_NOT_IMPLEMENTED", session.errors)
        self.assertEqual(manager.current_session.session_id, session.session_id)
        await app.handle_close("M01")
        await app.wait_until_idle()
        self.assertEqual(self.read_records(), [])
