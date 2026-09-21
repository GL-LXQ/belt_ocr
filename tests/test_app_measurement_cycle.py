"""验证启动阶段建立的逐机处理器能跑完整轮采集、识别、频率与存储闭环。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from itertools import cycle
from pathlib import Path
from unittest.mock import patch

from app import App
from config_util import AppConfig, MachineConfig
from enums import EventType
from frequency_adapter import FrequencyAdapter
from models import FrequencyMeasurement, MeasurementEvent, OCRResult, PublishEvent
from local_test_support import FakeMvsSdk, build_config, create_machine_database


class FixedFrequencyAdapter(FrequencyAdapter):
    """持续交付固定顺序的测试读数，并记录已交付值。"""

    def __init__(self, machine: MachineConfig, config: AppConfig, publish_event: PublishEvent) -> None:
        """登记频率仪配置、事件入口和已交付读数列表。

        Args:
            machine: 当前机器与频率来源绑定。
            config: 频率读取间隔等公共参数。
            publish_event: 与 START/CLOSE 共用机器 FIFO 队列的事件入口。

        Returns:
            返回示例：
                None  # 适配器已初始化，已交付读数为空列表
        """
        super().__init__(machine, config, publish_event)
        self.delivered_values_hz: list[float] = []

    async def listen_measurements(self) -> None:
        """按固定顺序交付当前周期的读数，直到任务取消。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 持续交付读数直到任务取消
        """
        # 按固定顺序准备测试读数。
        values_hz = cycle((40.0, 43.0))
        while True:
            await asyncio.sleep(self.config.frequency_interval_ms / 1000)

            # 无活动周期时不交付数据，有周期时记录并交付本次读数。
            session_id = self.active_session_id
            if session_id is None:
                continue
            value_hz = next(values_hz)
            self.delivered_values_hz.append(value_hz)
            await self.publish_event(MeasurementEvent(
                EventType.FREQUENCY_MEASURED,
                self.machine.machine_id,
                session_id,
                FrequencyMeasurement(session_id, self.machine.frequency_meter_serial, value_hz),
            ))


def stub_text_recognition(application: App) -> None:
    """替换尚未实现的识别与终选黑盒，返回确定性的文字和图片。

    Args:
        application: 待替换识别黑盒的应用实例。

    Returns:
        返回示例：
            None  # 识别与终选已改为确定性替身
    """
    # 识别按输入图片数量返回空文字块，终选固定取第一张图片。
    application.text_recognizer.recognize_images = lambda images: [{"blocks": []} for _ in images]
    application.text_recognizer.generate_final_text_and_images = lambda frame_results, frames: OCRResult(
        ordered_lines=("MODEL-1",),
        selected_frames=(frames[0],),
        line_frame_ids=((frames[0].frame_id,),),
    )


def test_two_machines_finish_cycle_with_evidence_and_records(tmp_path: Path) -> None:
    """验证两台机器各自完成采集、识别、频率结算与图片和记录存储。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        None  # 每台机器各写入一条测量记录，并落盘一张选中图片
    """
    # 建好两台启用机器的业务库，并按测试参数组装公共配置。
    config = build_config(
        tmp_path,
        capture_window_ms=200,
        camera_timeout_ms=20,
        frequency_interval_ms=20,
    )
    create_machine_database(config.database_path, [
        {
            "machine_name": "一号皮带机",  # 机器名称
            "camera_serial": "CAM-A",  # 相机序列号
            "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
        },
        {
            "machine_name": "二号皮带机",  # 机器名称
            "camera_serial": "CAM-B",  # 相机序列号
            "frequency_meter_serial": "FREQ-B",  # 频率仪序列号
        },
    ])
    sdk = FakeMvsSdk()

    # 用相机与频率仪替身启动应用，仅替换未实现的识别黑盒。
    with patch("app.load_mvs_sdk", lambda *arguments: sdk), \
            patch("app.FrequencyAdapter", FixedFrequencyAdapter):
        application = App(config)
        stub_text_recognition(application)

        async def run_cycle() -> None:
            """启动两台机器，等待采集窗口结束后正常关闭并释放资源。

            Args:
                无外部参数。

            Returns:
                返回示例：
                    None  # 本轮测量已结算，资源已释放
            """
            await application.start()
            machine_ids = list(application.machine_managers)
            await asyncio.gather(*(
                application.handle_start(machine_id) for machine_id in machine_ids
            ))

            # 等待采集窗口结束并累计频率读数，再发送正常关闭。
            await asyncio.sleep(0.4)
            await asyncio.gather(*(
                application.handle_close(machine_id) for machine_id in machine_ids
            ))

            # 等待识别和存储完成本轮结算，然后释放资源。
            await application.wait_until_idle(10)
            await application.stop()

        asyncio.run(run_cycle())

    # 读取测量记录，核对机器身份、最终文字和图片引用。
    with closing(sqlite3.connect(config.database_path)) as connection:
        records = connection.execute(
            "SELECT machine_id, ordered_lines, final_frequency_hz, evidence_refs "
            "FROM measurements ORDER BY machine_id"
        ).fetchall()
    assert [record[0] for record in records] == ["1", "2"]

    # 逐台核对最终文字、最后交付频率、选中图片文件与相机状态。
    for machine_id, ordered_lines, final_frequency_hz, evidence_refs in records:
        manager = application.machine_managers[machine_id]
        delivered_values_hz = manager.frequency_adapter.delivered_values_hz
        assert json.loads(ordered_lines) == ["MODEL-1"]
        assert delivered_values_hz
        assert final_frequency_hz == delivered_values_hz[-1]
        assert len(json.loads(evidence_refs)) == 1
        assert manager.camera.device.received_frame_count > 0
        assert manager.camera.device.closed

    # 证据图片按机器编号与周期编号落盘，内容非空。
    saved_images = list(config.evidence_directory.rglob("*.bmp"))
    assert len(saved_images) == 2
    assert all(image_path.stat().st_size > 0 for image_path in saved_images)
    assert sdk.closed
    assert not application.database.lock_acquired
