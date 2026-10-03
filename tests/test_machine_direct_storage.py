"""验证机器测量结果保存与 OCR 任务生命周期。"""

import asyncio
import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from async_utils import run_blocking_operation
from camera.camera import Camera, CaptureTask
from config_util import AppConfig, MachineConfig, load_config
from camera.hikrobot_sdk import MvsError
from database import CommitIntegrityConflictError, Database, MeasurementRecord
from enums import (
    EventType,
    FrequencyState,
    MachineOverallStatus,
    OCRState,
    ProgressStage,
    ProgressStatus,
    SessionState,
)
from runtime.machine_runtime import (
    CycleContext, EvidenceWriteError, ImageEncodingError, MachineRuntime,
)
from runtime.system_runtime import SystemRuntime
from models import (
    MeasurementSession,
    CaptureResult,
    MeasurementFrame,
    FrequencyMeasurement,
    OCRResult,
    RuntimeEvent,
)
from text_recognizer import (
    OCRProcessingError,
    OCRResourceWaitTimeoutError,
    TextRecognizer,
)


TEST_SESSION_START_TIME = datetime(2026, 9, 23, 12).astimezone().isoformat()
TEST_LOCAL_START_DATE = "20260923"


async def publish_event(event: object) -> None:
    """接收测试机器发出的事件。

    Args:
        event: 本轮测试发出的事件。

    Returns:
        返回示例：
            None  # 测试事件已接收
    """


async def hold_ocr_processing_resource(
    recognizer: TextRecognizer,
    acquired: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """占用共享 OCR 处理资源直到测试放行。

    Args:
        recognizer: 本轮共用的 OCR 处理器。
        acquired: 已取得资源的通知事件。
        release: 允许释放资源的通知事件。

    Returns:
        返回示例：
            None  # 已释放共享 OCR 处理资源
    """
    # 取得资源并等待测试放行。
    async with recognizer.acquire_ocr_access(1):
        acquired.set()
        await release.wait()


def create_frame(
    session_id: str, frame_id: str, image_bytes: bytes
) -> MeasurementFrame:
    """建立带图片内容的测试帧。

    Args:
        session_id: 测量周期编号。
        frame_id: 图片编号。
        image_bytes: 图片测试内容。

    Returns:
        返回示例：
            MeasurementFrame(...)  # 带编号和图片内容的测试帧
    """
    return MeasurementFrame(
        session_id=session_id,
        capture_id="capture-1",
        camera_serial="camera-1",
        frame_id=frame_id,
        captured_at="2026-09-23T00:00:00+00:00",
        captured_monotonic=0.0,
        camera_frame=SimpleNamespace(image_bytes=image_bytes),
    )


def create_machine(
    temporary_directory: Path,
    selected_frames: tuple[MeasurementFrame, ...],
    review_frames: tuple[MeasurementFrame, ...] = (),
    review_reason: str | None = None,
) -> tuple[MachineRuntime, Database, MeasurementSession, list, list]:
    """建立可直接结算的机器、数据库和周期。

    Args:
        temporary_directory: 测试数据库和图片的根目录。
        selected_frames: 正常结算时保存的证据帧。
        review_frames: 待复核时保存的原始帧。
        review_reason: 待复核原因。

    Returns:
        返回示例：
            (
                MachineRuntime(...),  # 测试机器运行时实例
                Database(...),  # 测试数据库
                MeasurementSession(...),  # 当前测量周期
                [],  # 进度通知记录
                [],  # 编码线程编号记录
            )
    """
    # 初始化测试库并准备相机编码函数。
    config = AppConfig(
        database_path=temporary_directory / "measurements.sqlite3",
        evidence_directory=temporary_directory / "evidence",
        mvs_development_directory=temporary_directory,
    )
    database = Database(config)
    database.initialize_result_database()

    # 创建异常事件测试表。
    with sqlite3.connect(config.recovery_path) as connection:
        database.abnormal_event_repo.create_table(connection)

    encoding_threads = []

    def encode_image(camera_frame: object) -> bytes:
        """记录编码线程并返回测试图片内容。

        Args:
            camera_frame: 带图片字节的相机帧。

        Returns:
            返回示例：
                b"image"  # 测试图片字节
        """
        encoding_threads.append(threading.get_ident())
        return camera_frame.image_bytes

    # 组装机器依赖并记录进度通知。
    camera = SimpleNamespace(
        sdk_camera=SimpleNamespace(encode_image=encode_image),
        available=True,
        is_capturing=False,
        delivery_task=None,
        unfinished_delivery_tasks=set(),
        stop=AsyncMock(),
        inform_capture_workflow_stop=AsyncMock(),
    )
    progress_updates = []

    def record_progress(
        machine_id: str,
        session_id: str,
        stage: object,
        status: object,
    ) -> None:
        """记录本轮阶段状态。

        Args:
            machine_id: 机器编号。
            session_id: 测量周期编号。
            stage: 测量阶段。
            status: 阶段状态。

        Returns:
            返回示例：
                None  # 阶段状态已记录
        """
        progress_updates.append((machine_id, session_id, stage, status))

    machine = MachineRuntime(
        machine_config=MachineConfig("1", "1号皮带机", "camera-1", "meter-1"),
        config=config,
        camera=camera,
        frequency_adapter=SimpleNamespace(active_session_id=None),
        text_recognizer=SimpleNamespace(),
        database=database,
        publish_event=publish_event,
        notify_measurement_progress=record_progress,
        on_system_failure=Mock(),
        state_changed=asyncio.Event(),
    )

    # 建立已关闭且 OCR 完成的周期。
    session = MeasurementSession(
        session_id="session-1",
        machine_id="1",
        camera_serial="camera-1",
        frequency_meter_serial="meter-1",
        capture_id="capture-1",
        start_time=TEST_SESSION_START_TIME,
        capture_start_time=0.0,
        capture_stop_time=1.0,
        ocr_state=OCRState.COMPLETED,
        ocr_result=OCRResult(
            recognized_lines=() if review_reason else ("AB123456",),
            selected_frames=selected_frames,
            line_frame_ids=() if review_reason else ((selected_frames[0].frame_id,),),
            review_frames=review_frames,
            review_reason=review_reason,
        ),
    )
    machine.cycles[session.session_id] = CycleContext(session)
    machine.camera.start_capture = Mock(
        side_effect=lambda session_id, started: start_test_capture(camera, started)
    )
    return machine, database, session, progress_updates, encoding_threads


def start_test_capture(
    camera: object,
    started: float,
) -> tuple[CaptureTask, asyncio.Future]:
    """返回已停流且尚可交付数据的测试采集任务。

    Args:
        camera: 持有模拟交付任务的相机。
        started: 本轮采集开始的单调时间。

    Returns:
        返回示例：
            (
                CaptureTask(...),  # 已停流的采集任务
                asyncio.Future(),  # 本轮模拟交付
            )
    """
    # 登记已停流的所属采集任务。
    capture = CaptureTask(None, started, 1.0, 50)
    capture.capture_finished.set()
    delivery = camera.delivery_task
    if delivery is None:
        delivery = asyncio.get_running_loop().create_future()
        delivery.set_result(None)
    return capture, delivery


async def wait_for_failure_audit(machine: MachineRuntime) -> None:
    """等待测试机器的失败记录保存和故障回调完成。

    Args:
        machine: 已经触发失败收尾的测试机器。

    Returns:
        返回示例：
            None  # 保存任务已结束，保存异常由机器原有故障回调记录
    """
    # 等待仍在运行的审计任务，保留回调处理保存异常。
    audit_tasks = [
        cycle.failure_audit_task for cycle in tuple(machine.cycles.values())
        if cycle.failure_audit_task is not None
    ]
    await asyncio.gather(*audit_tasks, return_exceptions=True)


def read_abnormal_events(database: Database) -> list[tuple]:
    """读取测试运行库中的异常事件。

    Args:
        database: 持有运行库路径的测试数据库。

    Returns:
        返回示例：
            [
                (
                    "1",  # 机器编号
                    "session-1",  # 周期编号
                    "此次相机没有采集到任何帧",  # 异常原因
                    '{"session_errors": ["此次相机没有采集到任何帧"]}',  # 事件内容
                ),
            ]
    """
    # 按写入顺序读取异常事件身份、原因和内容。
    with sqlite3.connect(database.config.recovery_path) as connection:
        return connection.execute(
            "SELECT machine_id, session_id, reason, payload_json "
            "FROM abnormal_events ORDER BY abnormal_event_id"
        ).fetchall()


def test_machine_overall_status_follows_runtime_availability(tmp_path: Path) -> None:
    """确认机器整体状态按初始化、机器故障和相机可用性顺序判断。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 在线、离线、故障及停止后的状态优先级均已核对
    """
    # 建立尚未初始化的机器。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    try:
        assert machine.overall_status == MachineOverallStatus.OFFLINE

        # 初始化后相机可用且没有机器故障时显示在线。
        machine.initialized = True
        machine.machine_failure_reason = None
        machine.camera = SimpleNamespace(available=True)
        assert machine.overall_status == MachineOverallStatus.ONLINE

        # 相机不可用且没有机器故障时显示离线。
        machine.camera.available = False
        assert machine.overall_status == MachineOverallStatus.OFFLINE

        # 已登记的机器故障优先于相机可用性。
        machine.camera.available = True
        machine.machine_failure_reason = "相机采集失败"
        assert machine.overall_status == MachineOverallStatus.FAULT
        machine.camera.available = False
        assert machine.overall_status == MachineOverallStatus.FAULT

        # 停止后保留故障原因并显示离线。
        machine.initialized = False
        assert machine.overall_status == MachineOverallStatus.OFFLINE
    finally:
        database.close()


@pytest.mark.asyncio
async def test_faulted_machine_release_notifies_offline(tmp_path: Path) -> None:
    """确认故障机器释放资源后发布离线状态并保留故障原因。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 资源已释放，离线通知已发送，机器故障原因保持原样
    """
    # 建立已故障且没有当前周期的机器。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    machine.initialized = True
    machine.machine_failure_reason = "相机采集失败"
    machine.cycles.clear()
    machine.active_session_id = None
    machine.camera.stop = AsyncMock()
    machine_status_notification = Mock()
    machine.notify_machine_status = machine_status_notification

    # 释放资源并核对离线通知。
    try:
        await machine.release_resources("测试结束")
        assert not machine.initialized
        assert machine.overall_status == MachineOverallStatus.OFFLINE
        assert machine.machine_failure_reason == "相机采集失败"
        machine_status_notification.assert_called_once_with("1", "offline")
    finally:
        database.close()


@pytest.mark.asyncio
async def test_unavailable_camera_start_notifies_offline(tmp_path: Path) -> None:
    """确认相机不可用时拒绝新周期并同步离线状态。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 没有创建周期，离线通知已发送且机器未登记故障
    """
    # 建立已初始化但相机不可用的机器。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    machine.initialized = True
    machine.cycles.clear()
    machine.active_session_id = None
    machine.camera.available = False
    machine.camera.is_capturing = False
    machine_status_notification = Mock()
    machine.notify_machine_status = machine_status_notification

    # 尝试启动并核对状态通知与接收结果。
    try:
        await machine.handle_machine_start()
        machine_status_notification.assert_called_once_with("1", "offline")
        assert machine.active_session is None
        assert machine.waiting_cycle_reset
        assert machine.machine_failure_reason is None
    finally:
        database.close()


def test_ocr_lock_wait_timeout_configuration(tmp_path: Path) -> None:
    """确认共享 OCR 处理资源等待期限从配置读取并要求正数。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 配置值为 10000 毫秒，零值被拒绝
    """
    # 将待现场填写的相机枚举值替换为测试用值。
    source_directory = Path(__file__).resolve().parents[1] / "config"
    configuration_directory = tmp_path / "config"
    configuration_directory.mkdir()
    configuration_text = (source_directory / "config.yaml").read_text(encoding="utf-8")
    configuration_text = configuration_text.replace(
        "camera_line_selector: null", "camera_line_selector: Line2"
    )
    configuration_text = configuration_text.replace(
        "camera_line_source: null", "camera_line_source: ExposureStartActive"
    )
    (configuration_directory / "config.yaml").write_text(
        configuration_text, encoding="utf-8"
    )

    # 读取测试配置并核对共享锁等待期限。
    configuration = load_config(configuration_directory)
    assert configuration.ocr_lock_wait_timeout_ms == 10000

    # 核对共享锁等待期限必须为正数。
    with pytest.raises(ValueError, match="ocr_lock_wait_timeout_ms"):
        replace(configuration, ocr_lock_wait_timeout_ms=0).validate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frequency_state",
    [FrequencyState.SUCCESS, FrequencyState.FAILED],
)
async def test_late_frequency_after_close_is_audited(
    tmp_path: Path,
    frequency_state: FrequencyState,
) -> None:
    """确认频率采集成功或失败结束后，同轮迟到读数仅写入异常事件。

    Args:
        tmp_path: pytest 提供的临时目录。
        frequency_state: CLOSE 后预期的频率采集结果。

    Returns:
        返回示例：
            None  # 迟到读数未加入本轮明细，最终频率保持不变且异常已记录
    """
    # 建立仍在等待 OCR 的活动周期。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None
    machine.frequency_adapter.active_session_id = session.session_id
    try:
        # 成功场景先交付有效读数，失败场景保持本轮无有效频率。
        if frequency_state == FrequencyState.SUCCESS:
            measurement = FrequencyMeasurement(session.session_id, "meter-1", 50.0)
            await machine.handle_event(RuntimeEvent(
                EventType.FREQUENCY_MEASURED,
                "1",
                session.session_id,
                measurement,
            ))

        # 真实 CLOSE 结束频率采集并保留仍在等待 OCR 的周期。
        await machine.handle_machine_close()
        assert session.frequency_state == frequency_state
        assert machine.frequency_adapter.active_session_id is None
        assert session.session_id in machine.cycles

        # 保存频率采集结束时的明细和最终结果。
        accepted_frequencies = list(session.measurement_frequencies)
        final_frequency = session.final_frequency

        # 再交付同轮频率，核对明细和最终结果保持不变。
        late_measurement = FrequencyMeasurement(session.session_id, "meter-1", 60.0)
        await machine.handle_event(RuntimeEvent(
            EventType.FREQUENCY_MEASURED,
            "1",
            session.session_id,
            late_measurement,
        ))
        assert session.measurement_frequencies == accepted_frequencies
        assert session.final_frequency is final_frequency
        assert session.frequency_state == frequency_state

        # 核对迟到读数记录到当前机器和周期的异常事件中。
        abnormal_events = read_abnormal_events(database)
        assert len(abnormal_events) == 1
        assert abnormal_events[0][:3] == ("1", session.session_id, "迟到的频率读数")
        machine.on_system_failure.assert_not_called()
    finally:
        # 关闭测试数据库。
        database.close()


@pytest.mark.asyncio
async def test_finalize_saves_images_before_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """确认正常证据先落盘，随后直接写入测量记录。

    Args:
        tmp_path: pytest 提供的临时目录。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 图片和数据库记录均已核对
    """
    # 记录保存与释放阶段的关键节点日志。
    caplog.set_level(logging.INFO)

    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, encoding_threads = create_machine(
        tmp_path, (frame,)
    )
    cycle = machine.cycles[session.session_id]

    # 使用早于结算日的本地开始时间检查日期目录。
    session.start_time = datetime(2024, 9, 22, 12).astimezone().isoformat()

    # 保存本轮有效频率。
    frequency = FrequencyMeasurement("session-1", "meter-1", 50.0)
    session.final_frequency = frequency
    session.measurement_frequencies.append(frequency)
    event_loop_thread = threading.get_ident()

    # 写入时确认图片已经存在。
    original_write_record = database.write_measurement_record

    def write_record_after_image(record: object) -> None:
        """检查证据图片后写入数据库。

        Args:
            record: 待写入的测量记录。

        Returns:
            返回示例：
                None  # 测量记录已写入
        """
        assert (record.evidence_directory / "frame-1.jpg").read_bytes() == b"image-one"
        original_write_record(record)

    database.write_measurement_record = write_record_after_image
    await machine.try_finalize(session)
    await cycle.result_storage_task

    # 核对保存状态、后台线程和数据库内容。
    assert session.state == SessionState.COMMITTED
    assert machine.active_session is None
    assert cycle.result_storage_task is None
    assert session.session_id not in machine.cycles
    assert len(encoding_threads) == 1
    assert encoding_threads[0] != event_loop_thread
    assert progress_updates[-1][2:] == (
        ProgressStage.EVIDENCE_STORAGE,
        ProgressStatus.SUCCESS,
    )
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT recognized_lines, final_frequency_hz, "
            "evidence_directory, needs_review "
            "FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert json.loads(record[0]) == ["AB123456"]
    assert record[1] == 50.0
    expected_directory = tmp_path / "evidence/20240922/1/session-1"
    assert record[2] == str(expected_directory)
    assert record[3] == 0

    # 核对保存开始与测量周期结束日志及先后顺序。
    log_messages = [log_record.getMessage() for log_record in caplog.records]
    save_message = (
        "1号皮带机 开始保存本轮测量结果 machine_id=1 session_id=session-1 "
        "evidence_frame_count=1 needs_review=False final_frequency_hz=50.0"
    )
    release_message = (
        "1号皮带机 当前测量周期已结束，运行时状态已释放 "
        "machine_id=1 session_id=session-1 final_state=COMMITTED"
    )
    assert save_message in log_messages
    assert release_message in log_messages
    assert log_messages.index(save_message) < log_messages.index(release_message)


@pytest.mark.asyncio
async def test_finalize_saves_all_review_frames(tmp_path: Path) -> None:
    """确认 OCR 待复核时保存全部原始帧和空频率。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 待复核图片和数据库字段均已核对
    """
    first_frame = create_frame("session-1", "frame-1", b"image-one")
    second_frame = create_frame("session-1", "frame-2", b"image-two")
    machine, database, session, _, _ = create_machine(
        tmp_path, (), (first_frame, second_frame), "没有最终文字"
    )
    cycle = machine.cycles[session.session_id]
    await machine.try_finalize(session)
    await cycle.result_storage_task

    # 核对两张图片和合并后的复核原因。
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert (expected_directory / "frame-1.jpg").read_bytes() == b"image-one"
    assert (expected_directory / "frame-2.jpg").read_bytes() == b"image-two"
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT recognized_lines, final_frequency_hz, evidence_directory, "
            "needs_review, review_reason "
            "FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert json.loads(record[0]) == []
    assert record[1] is None
    assert record[2] == str(expected_directory)
    assert record[3] == 1
    assert record[4] == "没有最终文字；没有找到最终频率，请人工复核。"


@pytest.mark.asyncio
async def test_finalize_preserves_reliable_text_for_review(tmp_path: Path) -> None:
    """确认部分文字可靠时保存文字并标记待复核。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 可靠文字和待复核状态已写入测量记录
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(
        tmp_path, (evidence_frame,), (evidence_frame,), "没有可靠的 20 位文字"
    )
    cycle = machine.cycles[session.session_id]
    session.ocr_result = OCRResult(
        recognized_lines=("123",),
        selected_frames=(evidence_frame,),
        line_frame_ids=((evidence_frame.frame_id,),),
        review_frames=(evidence_frame,),
        review_reason="没有可靠的 20 位文字",
    )

    # 结算后读取测量记录。
    await machine.try_finalize(session)
    await cycle.result_storage_task
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT recognized_lines, needs_review, review_reason "
            "FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()

    # 核对可靠文字和待复核原因。
    assert json.loads(record[0]) == ["123"]
    assert record[1] == 1
    assert "没有可靠的 20 位文字" in record[2]
    assert session.state == SessionState.COMMITTED
    assert read_abnormal_events(database) == []


@pytest.mark.asyncio
async def test_shutdown_preserves_committed_session(tmp_path: Path) -> None:
    """确认退出清理不会把已入库且暂留的周期改为失败。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 已入库周期保持成功且没有退出失败事件
    """
    # 建立测量周期并保留已结束的采集交付任务引用。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    delivery_task = asyncio.create_task(asyncio.sleep(0))
    machine.camera.delivery_task = delivery_task
    machine.camera.stop = AsyncMock()
    await delivery_task
    cycle.capture_result_pending = True

    # 保存测量记录并确认周期仍由机器持有。
    await machine.try_finalize(session)
    await cycle.result_storage_task
    assert session.state == SessionState.COMMITTED
    assert session.session_id in machine.cycles

    # 执行退出清理并核对成功状态和异常事件。
    await machine.release_resources("程序退出超时")
    assert session.state == SessionState.COMMITTED
    assert session.errors == []
    assert machine.active_session is None
    assert read_abnormal_events(database) == []
    with sqlite3.connect(database.config.database_path) as connection:
        saved_record = connection.execute(
            "SELECT session_id FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert saved_record == (session.session_id,)


@pytest.mark.asyncio
async def test_shutdown_fails_unfinished_session(tmp_path: Path) -> None:
    """确认退出清理仍将未完成的周期登记为失败。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 未完成周期已失败并记录退出原因
    """
    # 建立未完成的测量周期并提供相机退出入口。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    machine.camera.stop = AsyncMock()

    # 执行退出清理并核对失败状态和异常事件。
    await machine.release_resources("程序退出超时")
    assert session.state == SessionState.FAILED
    assert session.errors == ["程序退出超时"]
    assert machine.active_session is None
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "程序退出超时"


@pytest.mark.asyncio
async def test_mvs_encoding_failure_only_fails_current_session(tmp_path: Path) -> None:
    """确认 MVS 证据图片编码失败只结束本轮并保留相机可用状态。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 本轮失败且相机仍可受理下一轮
    """
    first_frame = create_frame("session-1", "frame-1", b"image-one")
    second_frame = create_frame("session-1", "frame-2", b"image-two")
    machine, database, session, progress_updates, _ = create_machine(tmp_path, (first_frame, second_frame))
    cycle = machine.cycles[session.session_id]

    def encode_image(camera_frame: object) -> bytes:
        """首帧正常编码，第二帧返回 MVS 编码错误。

        Args:
            camera_frame: 当前相机帧。

        Returns:
            返回示例：
                b"image-one"  # 首帧编码结果
        """
        if camera_frame.image_bytes == b"image-two":
            raise MvsError("SaveImageEx3 编码失败")
        return camera_frame.image_bytes

    # 使用真实相机可用状态检查本轮失败后的机器状态。
    sdk_camera = SimpleNamespace(
        encode_image=encode_image,
        closed=False,
        faulted=False,
        capture_lock=threading.Lock(),
    )
    camera = Camera(
        "1", "1号皮带机", 1000, 50, publish_event, machine.on_system_failure
    )
    camera.sdk_camera = sdk_camera
    machine.camera = camera
    machine.initialized = True

    # 执行本轮保存并核对失败审计和证据清理。
    await machine.try_finalize(session)
    await cycle.result_storage_task
    await wait_for_failure_audit(machine)
    assert session.state == SessionState.FAILED
    assert session.errors == ["SaveImageEx3 编码失败", "证据图片编码失败"]
    assert machine.active_session is None
    abnormal_events = read_abnormal_events(database)
    assert len(abnormal_events) == 1
    assert abnormal_events[0][2] == "证据图片编码失败"
    assert json.loads(abnormal_events[0][3])["session_errors"] == session.errors
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert not (expected_directory / "frame-1.jpg").exists()
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute("SELECT COUNT(*) FROM measurement_records").fetchone()[0]
    assert record_count == 0

    # 核对相机和机器仍可接收下一轮。
    assert not sdk_camera.faulted
    assert camera.available
    assert machine.machine_failure_reason is None
    assert not machine.waiting_cycle_reset
    assert machine.active_session is None
    machine.on_system_failure.assert_not_called()
    assert progress_updates[-1][2:] == (ProgressStage.EVIDENCE_STORAGE, ProgressStatus.FAILED)


@pytest.mark.asyncio
async def test_unknown_encoding_failure_removes_new_images(tmp_path: Path) -> None:
    """确认未知编码异常继续抛出，并清理本轮已保存的图片。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 新图片已清理且数据库没有测量记录
    """
    first_frame = create_frame("session-1", "frame-1", b"image-one")
    second_frame = create_frame("session-1", "frame-2", b"image-two")
    machine, database, session, _, _ = create_machine(
        tmp_path, (first_frame, second_frame)
    )
    cycle = machine.cycles[session.session_id]

    def encode_first_image(camera_frame: object) -> bytes:
        """编码首帧并让第二帧失败。

        Args:
            camera_frame: 当前相机帧。

        Returns:
            返回示例：
                b"image-one"  # 首帧的图片字节
        """
        if camera_frame.image_bytes == b"image-two":
            raise RuntimeError("编码失败")
        return camera_frame.image_bytes

    machine.camera.sdk_camera.encode_image = encode_first_image
    with pytest.raises(ImageEncodingError):
        await machine.try_finalize(session)
        await cycle.result_storage_task

    # 核对新图片与数据库均未留下结果。
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert not (expected_directory / "frame-1.jpg").exists()
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurement_records"
        ).fetchone()[0]
    assert record_count == 0
    assert session.state != SessionState.FAILED
    assert read_abnormal_events(database) == []
    assert "测量结果入库失败" not in session.errors


@pytest.mark.asyncio
async def test_image_write_failure_skips_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认图片写入失败时记录本轮并通知全局故障入口。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 本轮失败、数据库未写入且故障已上报
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    write_error = OSError("图片写入失败")
    monkeypatch.setattr(
        "runtime.machine_runtime.save_evidence_image",
        Mock(side_effect=write_error),
    )
    await machine.try_finalize(session)
    await cycle.result_storage_task
    await wait_for_failure_audit(machine)

    # 核对失败状态和数据库内容。
    assert session.state == SessionState.FAILED
    assert "证据图片保存失败" in session.errors
    assert "测量结果入库失败" not in session.errors
    assert "测量记录提交冲突" not in session.errors
    assert read_abnormal_events(database)[0][2] == "证据图片保存失败"
    machine.on_system_failure.assert_called_once()
    system_error = machine.on_system_failure.call_args.args[0]
    assert isinstance(system_error, EvidenceWriteError)
    assert system_error.__cause__ is write_error
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurement_records"
        ).fetchone()[0]
    assert record_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_unknown_image_write_error_is_not_database_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """确认未知图片保存异常原样上抛，不记为存储失败。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        error_type: 注入图片阶段的未知异常类型。

    Returns:
        返回示例：
            None  # 未知异常已上抛，未生成数据库失败记录
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    unknown_error = error_type("未知图片保存错误")
    monkeypatch.setattr(
        "runtime.machine_runtime.save_evidence_image",
        Mock(side_effect=unknown_error),
    )

    # 执行图片保存并核对未知异常保持原样上抛。
    with pytest.raises(error_type, match="未知图片保存错误") as captured_error:
        await machine.try_finalize(session)
        await cycle.result_storage_task
    assert captured_error.value is unknown_error
    assert "测量结果入库失败" not in session.errors
    assert "证据图片保存失败" not in session.errors
    assert "测量记录提交冲突" not in session.errors
    assert read_abnormal_events(database) == []
    machine.on_system_failure.assert_called_once_with(unknown_error)


@pytest.mark.asyncio
async def test_database_failure_keeps_saved_images(tmp_path: Path) -> None:
    """确认数据库写入失败时保留图片并通知全局故障入口。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 周期失败且已保存的图片保留
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    write_error = sqlite3.OperationalError("database is locked")
    database.write_measurement_record = Mock(side_effect=write_error)
    await machine.try_finalize(session)
    await cycle.result_storage_task
    await wait_for_failure_audit(machine)

    # 核对本轮失败状态与已保存的图片。
    assert session.state == SessionState.FAILED
    assert "测量结果入库失败" in session.errors
    assert "证据图片保存失败" not in session.errors
    assert read_abnormal_events(database)[0][2] == "测量结果入库失败"
    machine.on_system_failure.assert_called_once_with(write_error)
    assert machine.active_session is None
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert (expected_directory / "frame-1.jpg").read_bytes() == b"image-one"
    assert progress_updates[-1][2:] == (
        ProgressStage.EVIDENCE_STORAGE,
        ProgressStatus.FAILED,
    )


@pytest.mark.asyncio
async def test_database_conflict_keeps_conflict_reason(tmp_path: Path) -> None:
    """确认同一 Session 内容冲突不会覆盖原记录并通知全局故障入口。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 原记录保留且本轮冲突已上报
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    existing_record = MeasurementRecord(
        machine_id=session.machine_id,
        session_id=session.session_id,
        start_time=session.start_time,
        finish_time="2026-09-23T00:00:01+00:00",
        recognized_lines=("EXISTING",),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=tmp_path / "existing",
        needs_review=False,
        review_reason=None,
    )
    database.write_measurement_record(existing_record)

    # 执行数据库提交并核对冲突分类。
    await machine.try_finalize(session)
    await cycle.result_storage_task
    await wait_for_failure_audit(machine)
    assert session.state == SessionState.FAILED
    assert "测量记录提交冲突" in session.errors
    assert "测量结果入库失败" not in session.errors
    assert read_abnormal_events(database)[0][2] == "测量记录提交冲突"
    machine.on_system_failure.assert_called_once()
    conflict_error = machine.on_system_failure.call_args.args[0]
    assert isinstance(conflict_error, CommitIntegrityConflictError)

    # 核对数据库仍保留首次提交的内容。
    with sqlite3.connect(database.config.database_path) as connection:
        saved_record = connection.execute(
            "SELECT recognized_lines, evidence_directory "
            "FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert saved_record == (
        json.dumps(existing_record.recognized_lines),
        str(existing_record.evidence_directory),
    )


@pytest.mark.asyncio
async def test_unknown_database_value_error_is_not_commit_conflict(
    tmp_path: Path,
) -> None:
    """确认普通数据库 ValueError 原样上抛且不记为提交冲突。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 未知异常已上抛，未生成失败分类
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    unknown_error = ValueError("未知数据库错误")
    database.write_measurement_record = Mock(side_effect=unknown_error)

    # 执行数据库提交并核对未知异常保持原样上抛。
    with pytest.raises(ValueError, match="未知数据库错误") as captured_error:
        await machine.try_finalize(session)
        await cycle.result_storage_task
    assert captured_error.value is unknown_error
    assert "测量记录提交冲突" not in session.errors
    assert "DATABASE_WRITE_FAILED" not in session.errors
    assert read_abnormal_events(database) == []
    machine.on_system_failure.assert_called_once_with(unknown_error)


def test_database_compares_evidence_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """确认同一周期重复写入只接受相同证据目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 相同目录重复成功，不同目录触发冲突
    """
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    database = Database(config)
    database.initialize_result_database()
    record = MeasurementRecord(
        machine_id="1",
        session_id="session-1",
        start_time="2026-09-23T00:00:00+00:00",
        finish_time="2026-09-23T00:00:01+00:00",
        recognized_lines=("AB123456",),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1",
        needs_review=False,
        review_reason=None,
    )

    # 重复保存相同记录。
    database.write_measurement_record(record)
    database.write_measurement_record(record)

    # 核对重复写入只记录一条跳过告警。
    skip_warnings = [
        log_record
        for log_record in caplog.records
        if "跳过重复写入" in log_record.getMessage()
    ]
    assert len(skip_warnings) == 1
    assert skip_warnings[0].levelname == "WARNING"

    # 拒绝同一周期使用不同证据目录。
    with pytest.raises(CommitIntegrityConflictError):
        database.write_measurement_record(
            replace(record, evidence_directory=tmp_path / "other")
        )


def test_abnormal_event_accepts_event_and_session_fields(tmp_path: Path) -> None:
    """确认异常记录兼容业务事件和独立 Session 字段。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 两种异常记录均已写入现有表
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    _, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    event = RuntimeEvent(EventType.OCR_FAILED, "1", "session-1", "模型失败")

    # 保存原有事件格式和 Session 失败格式。
    database.save_abnormal_event("EVENT_ERROR", event)
    database.save_abnormal_event(
        "此次相机没有采集到任何帧",
        machine_id="1",
        session_id="session-1",
        payload={"session_errors": ["此次相机没有采集到任何帧"]},
    )

    # 核对事件身份、原因和失败明细。
    events = read_abnormal_events(database)
    assert [(row[0], row[1], row[2]) for row in events] == [
        ("1", "session-1", "EVENT_ERROR"),
        ("1", "session-1", "此次相机没有采集到任何帧"),
    ]
    assert json.loads(events[1][3])["session_errors"] == ["此次相机没有采集到任何帧"]


@pytest.mark.asyncio
async def test_empty_capture_fails_and_waits_for_close(tmp_path: Path) -> None:
    """确认无采集帧时审计失败并保留尚未关闭的周期。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 无帧失败、审计和现场关闭后释放已核对
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, _ = create_machine(
        tmp_path, (evidence_frame,)
    )
    camera_state_notification = Mock()
    machine.notify_camera_state = camera_state_notification
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None

    # 交付空采集结果并尝试启动下一轮。
    capture_result = CaptureResult(frames=(), statistics={"received_frame_count": 0})
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_COMPLETED, "1", session.session_id, capture_result
    ))
    await wait_for_failure_audit(machine)
    await machine.handle_machine_start()

    # 核对失败状态、异常记录和当前周期身份。
    assert session.state == SessionState.FAILED
    assert machine.active_session is session
    assert [update[2:] for update in progress_updates] == [
        (ProgressStage.IMAGE_CAPTURE, ProgressStatus.FAILED),
    ]
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "本轮未采集到图像"
    assert json.loads(events[0][3])["session_errors"] == session.errors
    camera_state_notification.assert_not_called()

    # 收到真实关闭后释放失败周期。
    await machine.handle_machine_close()
    assert machine.active_session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", (False, True))
async def test_cycle_close_notifies_once_for_current_session(
    tmp_path: Path, interrupted: bool
) -> None:
    """确认正常或中断关闭只通知当前周期一次。

    Args:
        tmp_path: pytest 提供的临时目录。
        interrupted: 本次关闭是否为中断关闭。

    Returns:
        返回示例：
            None  # 当前周期通知一次，旧周期及重复关闭均无通知
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None
    notification = Mock()
    machine.notify_cycle_closed = notification

    # 旧周期关闭不改变当前周期。
    await machine.handle_machine_close(close_event=RuntimeEvent(
        EventType.MACHINE_CLOSED, "1", "old-session"
    ))
    notification.assert_not_called()
    assert session.capture_stop_time is None

    # 首次关闭当前周期时发送一次通知。
    await machine.handle_machine_close(interrupted=interrupted)
    notification.assert_called_once_with("1", "session-1")

    # 重复关闭和空闲关闭不再发送通知。
    await machine.handle_machine_close(interrupted=interrupted)
    machine.cycles.clear()
    machine.active_session_id = None
    await machine.handle_machine_close()
    notification.assert_called_once_with("1", "session-1")


@pytest.mark.asyncio
async def test_capture_failure_is_audited_and_blocks_new_session(tmp_path: Path) -> None:
    """确认相机设备故障只使当前周期失败并阻止本机再启动。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 故障周期已审计，本机没有建立新周期
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, progress_updates, _ = create_machine(tmp_path, (evidence_frame,))
    camera_state_notification = Mock()
    machine.notify_camera_state = camera_state_notification
    machine.cycles.clear()
    machine.active_session_id = None
    machine_status_notification = Mock()
    machine.notify_machine_status = machine_status_notification
    sdk_camera = SimpleNamespace(
        serial="camera-1",
        closed=False,
        faulted=False,
        received_frame_count=0,
        capture_lock=threading.Lock(),
        start_grabbing=Mock(),
        read_frame=Mock(side_effect=MvsError("GetImageBuffer 失败")),
        stop_grabbing=Mock(),
    )

    async def deliver_capture_event(event: RuntimeEvent) -> None:
        """将相机采集事件放入当前机器队列。

        Args:
            event: 相机交付的采集事件。

        Returns:
            返回示例：
                None  # 采集事件已进入机器队列
        """
        await machine.queue.put(event)

    # 将真实相机适配器和测试设备接入机器。
    camera = Camera(
        "1", "1号皮带机", 1000, 50, deliver_capture_event,
        machine.on_system_failure,
    )
    camera.sdk_camera = sdk_camera
    machine.camera = camera
    machine.initialized = True

    # 启动采集并处理相机交付的故障事件。
    await machine.handle_machine_start()
    session = machine.active_session
    assert session is not None
    delivery_task = camera.delivery_task
    await delivery_task
    await asyncio.sleep(0)
    capture_event = await machine.queue.get()
    await machine.handle_event(capture_event)
    await wait_for_failure_audit(machine)
    machine.queue.task_done()

    # 核对本轮失败记录及相机可用状态。
    assert session.state == SessionState.FAILED
    assert session.errors == ["GetImageBuffer 失败", "相机采集失败"]
    assert machine.active_session is session
    assert not camera.available
    assert not camera.is_capturing
    assert machine.machine_failure_reason == "相机采集失败"
    machine.on_system_failure.assert_not_called()
    machine_status_notification.assert_called_once_with("1", "fault")
    camera_state_notification.assert_called_once_with("1", "相机故障", "GetImageBuffer 失败")
    assert progress_updates[-1][2:] == (
        ProgressStage.IMAGE_CAPTURE,
        ProgressStatus.FAILED,
    )
    events = read_abnormal_events(database)
    assert events[0][2] == "相机采集失败"
    assert json.loads(events[0][3])["session_errors"] == session.errors

    # 现场关闭后即使相机恢复可用，本机仍不再受理 START。
    await machine.handle_machine_close()
    assert machine.active_session is None
    sdk_camera.faulted = False
    await machine.handle_machine_start()
    assert machine.active_session is None
    assert machine.machine_failure_reason == "相机采集失败"

    # 另一台机器仍能正常创建自己的测量周期。
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    other_machine, other_database, _, _, _ = create_machine(
        other_directory, (evidence_frame,)
    )
    other_machine.machine_config = MachineConfig(
        "2", "2号皮带机", "camera-2", "meter-2"
    )
    other_machine.cycles.clear()
    other_machine.active_session_id = None
    other_machine.initialized = True
    other_machine.camera.available = True
    other_machine.camera.is_capturing = False
    other_machine.camera.start_capture = Mock(
        side_effect=lambda session_id, started: start_test_capture(
            machine.camera, started
        )
    )
    other_machine.camera.stop = AsyncMock()
    other_machine.camera.delivery_task = asyncio.get_running_loop().create_future()
    try:
        await other_machine.handle_machine_start()
        assert other_machine.active_session is not None
        assert other_machine.active_session.machine_id == "2"
        assert other_machine.machine_failure_reason is None
    finally:
        # 清理另一台机器的测试资源。
        other_machine.camera.delivery_task.cancel()
        other_machine.camera.delivery_task = None
        await other_machine.release_resources("测试结束")
        other_database.close()


@pytest.mark.asyncio
async def test_late_capture_failure_notifies_without_repeating_session_failure(
    tmp_path: Path,
) -> None:
    """确认 OCR 超时后的迟到采集故障只登记机器故障。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 机器故障已登记，失败周期和异常记录未重复处理
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, _ = create_machine(tmp_path, (evidence_frame,))
    camera_state_notification = Mock()
    machine.notify_camera_state = camera_state_notification
    machine_status_notification = Mock()
    machine.notify_machine_status = machine_status_notification
    machine.initialized = True
    machine.camera.available = True
    machine.camera.is_capturing = False
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.RUNNING
    session.ocr_result = None

    # OCR 超时先将当前周期结算为失败。
    await machine.handle_event(RuntimeEvent(
        EventType.OCR_TIMEOUT, "1", session.session_id
    ))
    await wait_for_failure_audit(machine)
    assert session.state == SessionState.FAILED
    assert session.errors == ["OCR 识别超时"]
    assert machine.machine_failure_reason is None
    machine_status_notification.assert_not_called()
    abnormal_events = read_abnormal_events(database)
    assert len(abnormal_events) == 1
    progress_before_capture_failure = list(progress_updates)

    # 将真实采集故障交给已经失败的周期。
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_FAILED, "1", session.session_id, "StopGrabbing 失败"
    ))

    # 核对机器故障和通知已登记，周期失败没有重复结算。
    machine_status_notification.assert_called_once_with("1", "fault")
    camera_state_notification.assert_called_once_with("1", "相机故障", "StopGrabbing 失败")
    assert session.errors == ["OCR 识别超时"]
    assert session.state == SessionState.FAILED
    assert machine.active_session is session
    assert machine.machine_failure_reason == "相机采集失败"
    assert read_abnormal_events(database) == abnormal_events
    assert progress_updates == progress_before_capture_failure

    # 真实 CLOSE 释放周期，本机故障继续阻止下一次 START。
    await machine.handle_machine_close()
    assert machine.active_session is None
    await machine.handle_machine_start()
    assert machine.active_session is None


@pytest.mark.asyncio
async def test_busy_camera_does_not_mark_device_faulted(tmp_path: Path) -> None:
    """确认采集锁占用只拒绝新周期，不标记相机故障。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 本轮未受理且相机保持可用
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, _, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    machine.cycles.clear()
    machine.active_session_id = None
    capture_lock = threading.Lock()
    capture_lock.acquire()
    sdk_camera = SimpleNamespace(closed=False, faulted=False, capture_lock=capture_lock)
    camera = Camera("1", "1号皮带机", 1000, 50, AsyncMock(), Mock())
    camera.sdk_camera = sdk_camera
    machine.camera = camera

    # 准备已初始化机器的整体状态通知。
    machine.initialized = True
    machine_status_notification = Mock()
    machine.notify_machine_status = machine_status_notification

    # 采集锁占用时尝试启动新周期。
    try:
        await machine.handle_machine_start()
        assert machine.active_session is None
        assert machine.waiting_cycle_reset
        assert not sdk_camera.faulted
        assert machine.overall_status == MachineOverallStatus.ONLINE
        machine_status_notification.assert_not_called()
    finally:
        capture_lock.release()


@pytest.mark.asyncio
async def test_capture_failure_after_close_releases_session(tmp_path: Path) -> None:
    """确认 CLOSE 已到达时相机故障仍能直接失败收尾。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 已关闭周期失败并释放，不产生测量记录
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None

    # 交付迟于 CLOSE 的采集故障并核对结果。
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_FAILED,
        "1",
        session.session_id,
        "StopGrabbing 失败",
    ))
    await wait_for_failure_audit(machine)
    assert session.state == SessionState.FAILED
    assert machine.active_session is None
    assert machine.machine_failure_reason == "相机采集失败"
    assert read_abnormal_events(database)[0][2] == "相机采集失败"


@pytest.mark.asyncio
async def test_ocr_execution_error_fails_and_is_audited(tmp_path: Path) -> None:
    """确认 OCR 执行异常进入失败收尾并写入异常事件。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # OCR 异常和失败原因已记录
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.RUNNING
    session.ocr_result = None
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock(
        side_effect=OCRProcessingError("模型执行失败")
    )
    machine.text_recognizer = recognizer
    machine.publish_event = machine.handle_event

    # 执行 OCR 并让失败事件进入机器处理流程。
    await machine.run_ocr_pipeline(session, (evidence_frame.camera_frame,))
    await wait_for_failure_audit(machine)

    # 核对执行异常、失败状态和异常事件。
    assert session.state == SessionState.FAILED
    assert machine.active_session is session
    assert "模型执行失败" in session.errors
    assert "OCR 识别执行失败" in session.errors
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "OCR 识别执行失败"
    assert json.loads(events[0][3])["session_errors"] == session.errors


@pytest.mark.asyncio
async def test_preparation_error_fails_only_current_session(tmp_path: Path) -> None:
    """确认初筛中的已知异常只结束当前测量周期。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 初筛错误按 OCR_FAILED 处理且未触发系统故障
    """
    # 建立运行中的周期和会报告已知错误的预处理入口。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.RUNNING
    session.ocr_result = None
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(side_effect=OCRProcessingError("图片初筛失败"))
    recognizer.recognize_qualified_frames = Mock()
    recognizer.acquire_ocr_access = Mock(wraps=recognizer.acquire_ocr_access)
    machine.text_recognizer = recognizer
    machine.publish_event = AsyncMock(side_effect=machine.handle_event)

    # 执行预处理并核对失败事件和共享资源调用。
    await machine.run_ocr_pipeline(session, (evidence_frame.camera_frame,))
    await wait_for_failure_audit(machine)
    published_event = machine.publish_event.await_args.args[0]
    assert published_event.event_type == EventType.OCR_FAILED
    assert published_event.payload == "图片初筛失败"
    assert session.state == SessionState.FAILED
    assert session.errors == ["图片初筛失败", "OCR 识别执行失败"]
    recognizer.acquire_ocr_access.assert_not_called()
    recognizer.recognize_qualified_frames.assert_not_called()
    machine.on_system_failure.assert_not_called()
    database.close()


@pytest.mark.asyncio
async def test_no_qualified_frames_complete_without_ocr_lock(tmp_path: Path) -> None:
    """确认初筛为空时直接交付复核结果且不等待共享资源。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 复核结果已交付，原资源占用者仍持有共享锁
    """
    # 建立运行中的周期并让其他任务占用共享资源。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.RUNNING
    session.ocr_result = None
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(return_value=((evidence_frame,), ()))
    recognizer.recognize_qualified_frames = Mock()
    recognizer.recognize_images = Mock()
    machine.text_recognizer = recognizer
    machine.publish_event = AsyncMock(side_effect=machine.handle_event)
    resource_acquired = asyncio.Event()
    resource_release = asyncio.Event()
    resource_holder_task = asyncio.create_task(hold_ocr_processing_resource(
        recognizer, resource_acquired, resource_release
    ))
    await resource_acquired.wait()
    recognizer.acquire_ocr_access = Mock(wraps=recognizer.acquire_ocr_access)

    try:
        # 在原占用者不释放资源时完成本轮初筛结果交付。
        await asyncio.wait_for(
            machine.run_ocr_pipeline(session, (evidence_frame.camera_frame,)),
            timeout=1,
        )
        published_event = machine.publish_event.await_args.args[0]
        assert published_event.event_type == EventType.OCR_COMPLETED
        assert session.ocr_state == OCRState.COMPLETED
        assert session.ocr_result.review_reason == "初筛后没有合格图片"
        assert session.ocr_result.review_frames == (evidence_frame,)

        # 核对本轮没有占用共享资源、启动超时或调用 OCR Engine。
        recognizer.acquire_ocr_access.assert_not_called()
        recognizer.recognize_qualified_frames.assert_not_called()
        recognizer.recognize_images.assert_not_called()
        assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks
        assert recognizer._processing_lock.locked()
        assert not resource_holder_task.done()
    finally:
        # 放行原占用者并关闭测试库。
        resource_release.set()
        await resource_holder_task
        database.close()


@pytest.mark.asyncio
async def test_invalid_session_after_preparation_skips_ocr(tmp_path: Path) -> None:
    """确认预处理结束后失效的周期不进入共享资源等待。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 没有资源等待、处理期限、推理和结果事件
    """
    # 建立会在预处理期间失效的测量周期。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    recognizer = TextRecognizer()

    def prepare_frames(
        session_id: str,
        capture_id: str,
        camera_serial: str,
        frames: tuple[object, ...],
    ) -> tuple[tuple[MeasurementFrame, ...], tuple[MeasurementFrame, ...]]:
        """结束当前周期并返回已经整理的测试帧。

        Args:
            session_id: 本轮测量周期编号。
            capture_id: 本轮采集编号。
            camera_serial: 本轮相机序列号。
            frames: 本轮相机原始帧。

        Returns:
            返回示例：
                (
                    (evidence_frame,),  # 全部原始帧
                    (evidence_frame,),  # 合格帧
                )
        """
        session.state = SessionState.FAILED
        return (evidence_frame,), (evidence_frame,)

    recognizer.prepare_frames_for_ocr = Mock(side_effect=prepare_frames)
    recognizer.recognize_qualified_frames = Mock()
    recognizer.acquire_ocr_access = Mock(wraps=recognizer.acquire_ocr_access)
    machine.text_recognizer = recognizer
    machine.publish_event = AsyncMock()

    # 运行识别任务并核对失效周期被提前丢弃。
    await machine.run_ocr_pipeline(session, (evidence_frame.camera_frame,))
    recognizer.prepare_frames_for_ocr.assert_called_once()
    recognizer.acquire_ocr_access.assert_not_called()
    recognizer.recognize_qualified_frames.assert_not_called()
    assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks
    machine.publish_event.assert_not_awaited()
    database.close()


@pytest.mark.asyncio
async def test_ocr_timeout_starts_after_shared_resource_and_cancels_on_completion(
    tmp_path: Path,
) -> None:
    """确认排队不计入 OCR 期限且正常完成后取消期限。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 等资源时无 OCR 期限，完成后没有迟到超时事件
    """
    # 建立可启动周期的机器和阻塞的 OCR 执行入口。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    machine.cycles.clear()
    machine.active_session_id = None
    machine.config = replace(
        machine.config,
        ocr_lock_wait_timeout_ms=1000,
        ocr_result_timeout_ms=250,
    )
    machine.camera.available = True
    machine.camera.is_capturing = False
    machine.camera.start_capture = Mock(
        side_effect=lambda session_id, started: start_test_capture(
            machine.camera, started
        )
    )
    machine.camera.stop = AsyncMock()
    machine.camera.delivery_task = asyncio.get_running_loop().create_future()
    resource_acquired = asyncio.Event()
    resource_release = asyncio.Event()
    processing_started = threading.Event()
    processing_release = threading.Event()
    expected_result = OCRResult(
        recognized_lines=(),
        selected_frames=(),
        line_frame_ids=(),
        review_frames=(),
        review_reason=None,
    )

    def process_frames(
        session_id: str,
        measurement_frames: tuple[object, ...],
        qualified_frames: tuple[object, ...],
    ) -> OCRResult:
        """等待测试放行后返回正常 OCR 结果。

        Args:
            session_id: 本轮测量周期编号。
            measurement_frames: 本轮全部原始帧。
            qualified_frames: 本轮合格帧。

        Returns:
            返回示例：
                expected_result  # 正常 OCR 结果
        """
        processing_started.set()
        if not processing_release.wait(timeout=5):
            raise TimeoutError("OCR 执行未被放行")
        return expected_result

    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock(side_effect=process_frames)
    machine.text_recognizer = recognizer
    published_events: list[EventType] = []

    async def deliver_event(event: RuntimeEvent) -> None:
        """记录 OCR 事件并交给当前机器处理。

        Args:
            event: OCR 任务发布的业务事件。

        Returns:
            返回示例：
                None  # OCR 事件已记录并处理
        """
        published_events.append(event.event_type)
        await machine.handle_event(event)

    machine.publish_event = deliver_event
    resource_holder_task = asyncio.create_task(hold_ocr_processing_resource(
        recognizer, resource_acquired, resource_release
    ))
    await resource_acquired.wait()
    try:
        # START 只创建整轮周期期限。
        await machine.handle_machine_start()
        session = machine.active_session
        assert session is not None
        cycle = machine.cycles[session.session_id]
        assert set(cycle.deadline_tasks) == {EventType.CYCLE_TIMEOUT}

        # 采集完成后让 OCR 排队超过处理期限。
        capture_result = CaptureResult(
            frames=(evidence_frame.camera_frame,), statistics={}
        )
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", session.session_id, capture_result
        ))
        recognition_task = cycle.recognition_task
        assert recognition_task is not None
        await asyncio.sleep(0.3)
        assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks
        assert session.state == SessionState.RUNNING
        assert not published_events
        machine.text_recognizer.prepare_frames_for_ocr.assert_called_once_with(
            session.session_id,
            session.capture_id,
            session.camera_serial,
            (evidence_frame.camera_frame,),
        )
        machine.text_recognizer.recognize_qualified_frames.assert_not_called()

        # 释放占用者后，实际 OCR 开始时才登记处理期限。
        resource_release.set()
        await resource_holder_task
        assert await asyncio.to_thread(processing_started.wait, 5)
        assert EventType.OCR_TIMEOUT in cycle.deadline_tasks
        assert session.state == SessionState.RUNNING

        # OCR 在期限内完成后不再发布超时事件。
        processing_release.set()
        await recognition_task
        await asyncio.sleep(0.3)
        assert published_events == [EventType.OCR_COMPLETED]
        assert session.ocr_state == OCRState.COMPLETED
        assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks
        assert session.state == SessionState.RUNNING
    finally:
        # 放行资源和识别任务并清理测试库。
        resource_release.set()
        processing_release.set()
        await resource_holder_task
        machine.camera.delivery_task.cancel()
        machine.camera.delivery_task = None
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_ocr_processing_timeout_fails_current_session(tmp_path: Path) -> None:
    """确认真正 OCR 执行超过期限后沿用失败清理。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # OCR 已超时，当前周期已失败并等待 CLOSE
    """
    # 建立等待 OCR 的周期和阻塞的识别入口。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None
    machine.config = replace(machine.config, ocr_result_timeout_ms=40)
    machine.camera.stop = AsyncMock()

    # 准备在线机器的整体状态通知。
    machine.initialized = True
    machine.camera.available = True
    machine_status_notification = Mock()
    machine.notify_machine_status = machine_status_notification

    # 创建识别执行和超时处理的同步通知。
    processing_started = threading.Event()
    processing_release = threading.Event()
    timeout_handled = asyncio.Event()

    def process_frames(
        session_id: str,
        measurement_frames: tuple[object, ...],
        qualified_frames: tuple[object, ...],
    ) -> object:
        """等待测试放行后结束 OCR 执行。

        Args:
            session_id: 本轮测量周期编号。
            measurement_frames: 本轮全部原始帧。
            qualified_frames: 本轮合格帧。

        Returns:
            返回示例：
                object()  # 超时后应被丢弃的 OCR 结果
        """
        processing_started.set()
        if not processing_release.wait(timeout=5):
            raise TimeoutError("OCR 执行未被放行")
        return object()

    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock(side_effect=process_frames)
    machine.text_recognizer = recognizer
    published_events: list[EventType] = []

    async def deliver_event(event: RuntimeEvent) -> None:
        """处理 OCR 事件并通知测试超时已结算。

        Args:
            event: OCR 任务或期限任务发布的事件。

        Returns:
            返回示例：
                None  # 事件已处理，超时事件已通知测试
        """
        published_events.append(event.event_type)
        await machine.handle_event(event)
        if event.event_type == EventType.OCR_TIMEOUT:
            timeout_handled.set()

    machine.publish_event = deliver_event
    try:
        # 采集完成后启动 OCR 并等待实际执行开始。
        capture_result = CaptureResult(
            frames=(evidence_frame.camera_frame,), statistics={}
        )
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", session.session_id, capture_result
        ))
        recognition_task = cycle.recognition_task
        assert recognition_task is not None
        assert await asyncio.to_thread(processing_started.wait, 5)
        assert EventType.OCR_TIMEOUT in cycle.deadline_tasks

        # 保持 OCR 阻塞直到处理期限到期。
        await asyncio.wait_for(timeout_handled.wait(), timeout=5)
        assert published_events == [EventType.OCR_TIMEOUT]
        assert session.ocr_state == OCRState.TIMED_OUT
        assert session.state == SessionState.FAILED
        assert machine.machine_failure_reason is None
        assert machine.overall_status == MachineOverallStatus.ONLINE
        machine_status_notification.assert_not_called()
        assert machine.active_session is session
        assert cycle.recognition_task is recognition_task
        assert not recognition_task.done()

        # 旧识别完成后不发布迟到结果。
        processing_release.set()
        await asyncio.gather(recognition_task, return_exceptions=True)
        assert published_events == [EventType.OCR_TIMEOUT]
    finally:
        # 放行识别并清理当前周期和测试库。
        processing_release.set()
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_invalid_session_waiting_for_ocr_resource_skips_timeout_and_inference(
    tmp_path: Path,
) -> None:
    """确认排队期间失效的周期不启动 OCR 期限或推理。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 失效周期没有 OCR 期限、推理和结果事件
    """
    # 建立等待共享资源的 OCR 任务。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock()
    machine.text_recognizer = recognizer
    resource_acquired = asyncio.Event()
    resource_release = asyncio.Event()
    resource_holder_task = asyncio.create_task(hold_ocr_processing_resource(
        recognizer, resource_acquired, resource_release
    ))
    await resource_acquired.wait()
    recognizer.acquire_ocr_access = Mock(wraps=recognizer.acquire_ocr_access)
    machine.publish_event = AsyncMock()
    recognition_task = asyncio.create_task(machine.run_ocr_pipeline(
        session, (evidence_frame.camera_frame,)
    ))
    try:
        # 周期在等待共享资源期间失效。
        for wait_attempt in range(100):
            if recognizer.acquire_ocr_access.called:
                break
            await asyncio.sleep(0.01)
        assert recognizer.acquire_ocr_access.called
        session.state = SessionState.FAILED
        resource_release.set()
        await resource_holder_task
        await recognition_task

        # 核对失效周期没有启动 OCR 期限和识别。
        assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks
        machine.text_recognizer.recognize_qualified_frames.assert_not_called()
        machine.publish_event.assert_not_awaited()
    finally:
        # 放行资源拥有者并关闭测试库。
        resource_release.set()
        await resource_holder_task
        await asyncio.gather(recognition_task, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
async def test_consecutive_ocr_lock_wait_timeouts_are_recorded_per_session(
    tmp_path: Path,
) -> None:
    """确认连续等待资源超时分别入库且关闭后仍可启动下一轮。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 两轮失败分别入库，第三轮仍可创建
    """
    # 建立持续占用的共享资源和待处理的首轮周期。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, first_session, progress_updates, _ = create_machine(
        tmp_path, (evidence_frame,)
    )
    cycle = machine.cycles[first_session.session_id]
    first_session.capture_stop_time = None
    machine.active_session_id = first_session.session_id
    first_session.ocr_state = OCRState.WAITING
    first_session.ocr_result = None
    machine.config = replace(machine.config, ocr_lock_wait_timeout_ms=50)
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock()
    machine.text_recognizer = recognizer
    resource_acquired = asyncio.Event()
    resource_release = asyncio.Event()
    resource_holder_task = asyncio.create_task(hold_ocr_processing_resource(
        recognizer, resource_acquired, resource_release
    ))
    await resource_acquired.wait()
    machine.camera.available = True
    machine.camera.is_capturing = False
    machine.camera.start_capture = Mock(
        side_effect=lambda session_id, started: start_test_capture(
            machine.camera, started
        )
    )
    machine.camera.stop = AsyncMock()
    pending_events: asyncio.Queue[RuntimeEvent] = asyncio.Queue()

    async def queue_event(event: RuntimeEvent) -> None:
        """收集识别任务发布的事件供机器主流程处理。

        Args:
            event: 识别任务发布的事件。

        Returns:
            返回示例：
                None  # 事件已加入测试队列
        """
        await pending_events.put(event)

    machine.publish_event = queue_event
    capture_result = CaptureResult(
        frames=(evidence_frame.camera_frame,), statistics={}
    )
    try:
        for cycle_number in (1, 2):
            # 采集完成后等待共享资源超时。
            session = machine.active_session
            assert session is not None
            cycle = machine.cycles[session.session_id]
            await machine.handle_event(RuntimeEvent(
                EventType.CAPTURE_COMPLETED,
                session.machine_id,
                session.session_id,
                capture_result,
            ))
            recognition_task = cycle.recognition_task
            assert recognition_task is not None
            timeout_event = await asyncio.wait_for(pending_events.get(), timeout=5)
            assert timeout_event.event_type == EventType.OCR_LOCK_WAIT_TIMEOUT
            assert timeout_event.session_id == session.session_id
            await recognition_task
            await machine.handle_event(timeout_event)
            await wait_for_failure_audit(machine)

            # 核对本轮失败进度和独立异常记录。
            assert session.ocr_state == OCRState.FAILED
            assert session.state == SessionState.FAILED
            assert session.errors == ["OCR 识别资源等待超时"]
            assert machine.active_session is session
            assert machine.machine_failure_reason is None
            assert (
                session.machine_id,
                session.session_id,
                ProgressStage.CHARACTER_RECOGNITION,
                ProgressStatus.FAILED,
            ) in progress_updates
            abnormal_events = read_abnormal_events(database)
            assert len(abnormal_events) == cycle_number
            assert abnormal_events[-1][:3] == (
                session.machine_id,
                session.session_id,
                "OCR 识别资源等待超时",
            )
            with sqlite3.connect(database.config.database_path) as connection:
                measurement_count = connection.execute(
                    "SELECT COUNT(*) FROM measurement_records"
                ).fetchone()[0]
            assert measurement_count == 0
            assert not resource_holder_task.done()
            machine.text_recognizer.recognize_qualified_frames.assert_not_called()
            assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks

            # 真实 CLOSE 释放本轮，再创建下一轮周期。
            await machine.handle_machine_close()
            assert machine.active_session is None
            machine.camera.delivery_task = asyncio.get_running_loop().create_future()
            machine.camera.delivery_task.set_result(None)
            await machine.handle_machine_start()
            assert machine.active_session is not None
            assert machine.active_session.session_id != session.session_id
            machine.camera.delivery_task = None

        # 核对两条异常分别属于前两轮，第三轮已正常受理。
        abnormal_events = read_abnormal_events(database)
        assert [event[1] for event in abnormal_events] == [
            first_session.session_id,
            session.session_id,
        ]
        assert machine.active_session is not None
    finally:
        # 放行资源拥有者并清理机器资源。
        resource_release.set()
        await resource_holder_task
        machine.camera.delivery_task = None
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_cancelled_ocr_resource_wait_does_not_release_current_owner(
    tmp_path: Path,
) -> None:
    """确认等待资源时取消识别任务不会释放其他任务占用的资源。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 原任务继续占用资源，没有超时或识别结果
    """
    # 建立被其他任务占用的共享资源。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock()
    machine.text_recognizer = recognizer
    resource_acquired = asyncio.Event()
    resource_release = asyncio.Event()
    resource_holder_task = asyncio.create_task(hold_ocr_processing_resource(
        recognizer, resource_acquired, resource_release
    ))
    await resource_acquired.wait()
    recognizer.acquire_ocr_access = Mock(wraps=recognizer.acquire_ocr_access)
    machine.publish_event = AsyncMock()
    recognition_task = asyncio.create_task(machine.run_ocr_pipeline(
        session, (evidence_frame.camera_frame,)
    ))
    try:
        # 取消仍在等待共享资源的识别任务。
        for wait_attempt in range(100):
            if recognizer.acquire_ocr_access.called:
                break
            await asyncio.sleep(0.01)
        assert recognizer.acquire_ocr_access.called
        session.state = SessionState.FAILED
        recognition_task.cancel()
        await asyncio.gather(recognition_task, return_exceptions=True)

        # 核对取消没有释放资源或执行识别。
        assert not resource_holder_task.done()
        with pytest.raises(OCRResourceWaitTimeoutError):
            async with recognizer.acquire_ocr_access(0.01):
                pytest.fail("原任务仍占用资源时不应进入")
        assert EventType.OCR_TIMEOUT not in cycle.deadline_tasks
        machine.text_recognizer.recognize_qualified_frames.assert_not_called()
        machine.publish_event.assert_not_awaited()
    finally:
        # 放行资源拥有者并关闭测试库。
        resource_release.set()
        await resource_holder_task
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_event_type",
    (EventType.OCR_TIMEOUT, EventType.OCR_FAILED),
)
async def test_ocr_failure_close_releases_session_while_old_task_finishes(
    tmp_path: Path,
    failure_event_type: EventType,
) -> None:
    """确认旧 OCR 收尾时保留失败周期资源且不清理新周期任务。

    Args:
        tmp_path: pytest 提供的临时目录。
        failure_event_type: 本次触发的 OCR 失败事件类型。

    Returns:
        返回示例：
            None  # 现场占用已解除，旧线程结束后才回收后台上下文
    """
    # 建立等待 OCR 的旧周期和两轮可控的阻塞识别。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, old_session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[old_session.session_id]
    old_session.capture_stop_time = None
    machine.active_session_id = old_session.session_id
    old_session.ocr_state = OCRState.WAITING
    old_session.ocr_result = None
    old_started = threading.Event()
    old_release = threading.Event()
    new_started = threading.Event()
    new_release = threading.Event()
    old_result = object()
    new_result = object()

    def process_frames(
        session_id: str,
        measurement_frames: tuple[object, ...],
        qualified_frames: tuple[object, ...],
    ) -> object:
        """按周期等待测试放行后返回对应 OCR 结果。

        Args:
            session_id: 本次识别所属的周期编号。
            measurement_frames: 本次识别的全部原始帧。
            qualified_frames: 本次识别的合格帧。

        Returns:
            返回示例：
                old_result  # 旧周期放行后的识别结果
        """
        if session_id == old_session.session_id:
            old_started.set()
            if not old_release.wait(timeout=5):
                raise TimeoutError("旧 OCR 未被放行")
            return old_result

        new_started.set()
        if not new_release.wait(timeout=5):
            raise TimeoutError("新 OCR 未被放行")
        return new_result

    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock(side_effect=process_frames)
    machine.text_recognizer = recognizer
    machine.publish_event = AsyncMock()
    machine.camera.available = True
    machine.camera.is_capturing = False
    machine.camera.start_capture = Mock(
        side_effect=lambda session_id, started: start_test_capture(
            machine.camera, started
        )
    )
    machine.camera.stop = AsyncMock()
    capture_result = CaptureResult(frames=(evidence_frame.camera_frame,), statistics={})

    try:
        # 启动旧 OCR 并在底层识别仍运行时触发超时。
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", old_session.session_id, capture_result
        ))
        old_task = cycle.recognition_task
        assert old_task is not None
        assert await asyncio.to_thread(old_started.wait, 5)
        await machine.handle_event(RuntimeEvent(
            failure_event_type, "1", old_session.session_id, "模型执行失败"
        ))
        await wait_for_failure_audit(machine)
        assert old_session.state == SessionState.FAILED
        assert machine.active_session is old_session
        assert cycle.recognition_task is old_task
        assert old_session.session_id in machine.cycles
        assert not old_task.done()

        # 真实 CLOSE 解除现场占用，旧线程收尾期间建立新周期。
        await machine.handle_machine_close()
        assert machine.active_session is None
        machine.camera.delivery_task = asyncio.get_running_loop().create_future()
        await machine.handle_machine_start()
        new_session = machine.active_session
        assert new_session is not None
        assert new_session is not old_session
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", new_session.session_id, capture_result
        ))
        new_cycle = machine.cycles[new_session.session_id]
        new_task = new_cycle.recognition_task
        assert new_task is not None
        assert machine.cycles[new_session.session_id].recognition_task is new_task
        await asyncio.sleep(0.05)
        assert not new_started.is_set()

        # 放行旧 OCR 并核对旧回调未清理新周期的任务。
        old_release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        assert await asyncio.to_thread(new_started.wait, 5)
        await asyncio.sleep(0)
        assert old_session.session_id not in machine.cycles
        assert machine.active_session is new_session
        assert machine.cycles[new_session.session_id].recognition_task is new_task
        machine.publish_event.assert_not_awaited()

        # 旧周期的迟到事件不进入新周期。
        await machine.handle_event(RuntimeEvent(
            EventType.OCR_COMPLETED, "1", old_session.session_id, old_result
        ))
        assert machine.active_session is new_session
        assert machine.cycles[new_session.session_id].recognition_task is new_task
        assert new_session.ocr_result is None
    finally:
        # 放行阻塞识别并清理本机任务和测试库。
        old_release.set()
        new_release.set()
        delivery_task = machine.camera.delivery_task
        if delivery_task is not None:
            delivery_task.cancel()
            machine.camera.delivery_task = None
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_old_ocr_unknown_error_reports_system_failure_without_clearing_new_task(
    tmp_path: Path,
) -> None:
    """确认旧 OCR 的未知异常上报故障且不清理新任务引用。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 未知异常已上报，新周期和任务引用保持有效
    """
    # 建立已关闭的新周期和仍在执行的新 OCR 任务。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, new_session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[new_session.session_id]
    new_session.state = SessionState.FAILED
    new_task = asyncio.create_task(asyncio.Event().wait())
    cycle.recognition_task = new_task
    machine.active_session_id = new_session.session_id
    new_task.add_done_callback(
        lambda task: machine.handle_recognition_task_finished(
            task, new_session.session_id
        )
    )
    unknown_error = RuntimeError("旧 OCR 未知异常")

    async def raise_old_error() -> None:
        """让已解绑的旧 OCR 任务以未知异常结束。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 任务实际抛出旧 OCR 的未知异常
        """
        raise unknown_error

    try:
        # 令旧任务完成并执行其回调。
        old_task = asyncio.create_task(raise_old_error())
        old_session = replace(new_session, session_id="old-session")
        machine.cycles[old_session.session_id] = CycleContext(
            old_session, recognition_task=old_task
        )
        old_task.add_done_callback(
            lambda task: machine.handle_recognition_task_finished(
                task, old_session.session_id
            )
        )
        await asyncio.gather(old_task, return_exceptions=True)
        await asyncio.sleep(0)

        # 核对未知异常与新周期隔离。
        machine.on_system_failure.assert_called_once_with(unknown_error)
        assert "old-session" not in machine.cycles
        assert machine.cycles[new_session.session_id].recognition_task is new_task
        assert machine.active_session is new_session
    finally:
        # 取消新任务并关闭测试库。
        new_task.cancel()
        await asyncio.gather(new_task, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_detached_ocr_task(tmp_path: Path) -> None:
    """确认退出流程等待已解绑但仍在运行的 OCR 底层线程。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 旧 OCR 收尾完成后退出流程才结束
    """
    # 建立没有活动周期但仍有旧 OCR 任务的机器。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    machine.cycles.clear()
    machine.active_session_id = None
    machine.camera.stop = AsyncMock()
    recognizer = TextRecognizer()
    machine.text_recognizer = recognizer
    operation_started = threading.Event()
    operation_finished = threading.Event()

    def finish_old_ocr() -> None:
        """等待测试放行后结束旧 OCR 底层线程。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 旧 OCR 底层线程已结束
        """
        operation_started.set()
        if not operation_finished.wait(timeout=5):
            raise TimeoutError("旧 OCR 底层线程未被放行")

    async def run_old_ocr() -> None:
        """持有共享 OCR 处理资源直到旧底层线程结束。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 旧识别结束且共享资源已释放
        """
        # 在共享资源上下文内等待阻塞识别结束。
        async with recognizer.acquire_ocr_access(1):
            await run_blocking_operation(finish_old_ocr)

    # 启动已解绑的旧任务并发起退出。
    old_task = asyncio.create_task(run_old_ocr())
    old_session = MeasurementSession(
        session_id="old-session", machine_id="1", camera_serial="camera-1",
        frequency_meter_serial="meter-1", capture_id="capture-old",
        start_time=TEST_SESSION_START_TIME, capture_start_time=0.0,
        capture_stop_time=1.0, state=SessionState.FAILED,
    )
    machine.cycles[old_session.session_id] = CycleContext(
        old_session, recognition_task=old_task
    )
    old_task.add_done_callback(
        lambda task: machine.handle_recognition_task_finished(
            task, old_session.session_id
        )
    )
    shutdown_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(operation_started.wait, 5)
        shutdown_task = asyncio.create_task(machine.release_resources("测试结束"))
        await asyncio.sleep(0)
        machine.camera.stop.assert_awaited_once()
        assert not shutdown_task.done()

        # 放行旧 OCR 后核对退出完成与任务回收。
        operation_finished.set()
        await shutdown_task
        await asyncio.sleep(0)
        assert old_task.done()
        assert not machine.cycles
        async with recognizer.acquire_ocr_access(0.1):
            pass
    finally:
        # 放行可能尚未结束的线程并关闭测试库。
        operation_finished.set()
        if shutdown_task is not None:
            await asyncio.gather(shutdown_task, return_exceptions=True)
        await asyncio.gather(old_task, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
async def test_unknown_ocr_error_reaches_system_failure_callback(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    """确认未知识别异常通过任务完成回调交给全局故障入口。

    Args:
        tmp_path: pytest 提供的临时目录。
        error_type: 注入识别任务的未知异常类型。

    Returns:
        返回示例：
            None  # 未知异常已交给系统故障回调，未生成 OCR 失败事件
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    cycle = machine.cycles[session.session_id]
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None
    error = error_type("未知识别错误")
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(
        return_value=((evidence_frame,), (evidence_frame,))
    )
    recognizer.recognize_qualified_frames = Mock(side_effect=error)
    machine.text_recognizer = recognizer
    machine.publish_event = AsyncMock()

    # 交付采集结果并由机器启动带完成回调的识别任务。
    capture_result = CaptureResult(frames=(evidence_frame.camera_frame,), statistics={})
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_COMPLETED, "1", session.session_id, capture_result
    ))
    current_recognition_task = cycle.recognition_task
    assert current_recognition_task is not None

    # 等待原始异常离开任务并核对完成回调的交付结果。
    with pytest.raises(error_type) as captured_error:
        await current_recognition_task
    assert captured_error.value is error
    machine.on_system_failure.assert_called_once_with(error)
    machine.publish_event.assert_not_awaited()
    assert cycle.recognition_task is None
    async with recognizer.acquire_ocr_access(0.1):
        pass
    assert session.ocr_state == OCRState.RUNNING
    assert read_abnormal_events(database) == []


@pytest.mark.asyncio
async def test_cycle_timeout_fails_and_is_audited(tmp_path: Path) -> None:
    """确认周期超时进入失败收尾并记录超时原因。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 超时失败和异常事件已核对
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None

    # 交付周期超时事件。
    await machine.handle_event(RuntimeEvent(
        EventType.CYCLE_TIMEOUT, "1", session.session_id
    ))
    await wait_for_failure_audit(machine)

    # 核对周期失败、等待现场复位和超时审计。
    assert session.state == SessionState.FAILED
    assert machine.waiting_cycle_reset
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "测量周期超时"
    assert json.loads(events[0][3])["session_errors"] == session.errors

    # 核对审计写入成功时不会升级为系统故障。
    machine.on_system_failure.assert_not_called()


@pytest.mark.asyncio
async def test_audit_failure_does_not_block_session_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """确认异常事件写入失败时仍完成 Session 失败收尾并升级为系统故障。

    Args:
        tmp_path: pytest 提供的临时目录。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 审计失败已记录日志，收尾完整且故障上报发生在清理之后
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    audit_error = sqlite3.OperationalError("运行库写入失败")
    database.save_abnormal_event = Mock(side_effect=audit_error)

    # 频率交付身份只在收尾中清空，用于核对故障上报发生在清理之后。
    machine.frequency_adapter.active_session_id = session.session_id
    state_at_escalation = {}

    def record_state_at_escalation(error: Exception) -> None:
        """记录上报系统故障时的本轮状态。

        Args:
            error: 本次交给系统故障入口的异常。

        Returns:
            返回示例：
                None  # 上报时刻的周期状态已记录
        """
        state_at_escalation["frequency_session_id"] = machine.frequency_adapter.active_session_id
        state_at_escalation["session_state"] = session.state

    machine.on_system_failure = Mock(side_effect=record_state_at_escalation)

    # 执行失败收尾并等待现场关闭。
    await machine.handle_session_failure(session, "此次相机没有采集到任何帧")
    await wait_for_failure_audit(machine)
    assert machine.active_session is session
    await machine.handle_machine_close()

    # 核对审计写入只尝试一次且周期已释放。
    database.save_abnormal_event.assert_called_once()
    assert "1号皮带机 保存测量失败记录时发生异常" in caplog.text
    assert machine.active_session is None

    # 核对系统故障在收尾完成后收到审计异常。
    machine.on_system_failure.assert_called_once_with(audit_error)
    assert state_at_escalation == {
        "frequency_session_id": None,
        "session_state": SessionState.FAILED,
    }


@pytest.mark.asyncio
async def test_storage_failure_keeps_root_cause_when_audit_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """确认已有系统级根因时，异常事件写入失败不会覆盖原始根因。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 收尾完整、审计失败有日志且系统故障保留原始根因
    """
    # 同时设置证据图片写入故障和异常事件写入故障。
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    write_error = OSError("图片写入失败")
    monkeypatch.setattr(
        "runtime.machine_runtime.save_evidence_image",
        Mock(side_effect=write_error),
    )
    database.save_abnormal_event = Mock(
        side_effect=sqlite3.OperationalError("运行库写入失败")
    )

    # 执行本轮结算。
    await machine.try_finalize(session)
    await cycle.result_storage_task
    await wait_for_failure_audit(machine)

    # 核对本轮已完整失败收尾并释放周期。
    assert session.state == SessionState.FAILED
    assert session.errors.count("证据图片保存失败") == 1
    assert machine.active_session is None
    assert machine.frequency_adapter.active_session_id is None

    # 核对审计写入只尝试一次且失败已记录日志。
    database.save_abnormal_event.assert_called_once()
    assert read_abnormal_events(database) == []
    assert "1号皮带机 保存测量失败记录时发生异常" in caplog.text

    # 核对系统故障只上报一次，且仍是原始证据写入根因。
    machine.on_system_failure.assert_called_once()
    escalated_error = machine.on_system_failure.call_args.args[0]
    assert isinstance(escalated_error, EvidenceWriteError)
    assert escalated_error.__cause__ is write_error


@pytest.mark.asyncio
async def test_io_interruption_fails_open_session(tmp_path: Path) -> None:
    """确认 IO 中断结束仍未收到 CLOSE 的周期并记录失败。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 活动周期已失败收尾并留下 IO_INTERRUPTED 记录
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.ocr_state = OCRState.WAITING

    # 交付 IO 中断事件并检查本轮失败收尾。
    await machine.handle_event(RuntimeEvent(EventType.IO_INTERRUPTED, "1"))
    await wait_for_failure_audit(machine)
    assert session.state == SessionState.FAILED
    assert "IO 通信中断" in session.errors
    assert session.capture_stop_time is not None
    assert machine.active_session is None
    assert machine.waiting_cycle_reset

    # 核对异常事件中的周期身份和失败原因。
    events = read_abnormal_events(database)
    assert [(row[0], row[1], row[2]) for row in events] == [
        ("1", "session-1", "IO 通信中断"),
    ]


@pytest.mark.asyncio
async def test_io_interruption_preserves_closed_session(tmp_path: Path) -> None:
    """确认已收到 CLOSE 的周期继续等待正常结果。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 已关闭周期未被 IO 中断改为失败
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))

    # 交付 IO 中断事件并检查已关闭周期状态。
    await machine.handle_event(RuntimeEvent(EventType.IO_INTERRUPTED, "1"))
    assert session.state == SessionState.RUNNING
    assert session.session_id in machine.cycles
    assert machine.waiting_cycle_reset
    assert read_abnormal_events(database) == []


@pytest.mark.asyncio
async def test_failed_session_releases_when_delivery_task_finishes(tmp_path: Path) -> None:
    """确认 IO 中断失败的周期在采集交付任务结束后被释放。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 交付期间周期保留，同轮采集故障只登记机器故障，交付结束后周期已释放
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, _, _, _ = create_machine(tmp_path, (evidence_frame,))
    camera = machine.camera
    camera.available = True
    camera.is_capturing = False
    camera.stop = AsyncMock()
    machine.cycles.clear()
    machine.active_session_id = None
    machine.initialized = True

    # 采集交付任务由测试放行结束。
    delivery_released = asyncio.Event()

    def clear_delivery_task(task: asyncio.Task) -> None:
        """按真实相机顺序在交付结束时先清空交付任务引用。

        Args:
            task: 已结束的采集交付任务。

        Returns:
            返回示例：
                None  # 相机交付任务引用已清空
        """
        camera.delivery_task = None

    def start_capture(session_id: str, capture_start_time: float) -> tuple:
        """建立等待放行的采集交付任务。

        Args:
            session_id: 本轮采集归属的周期编号。
            capture_start_time: 本轮采集开始的单调时间。

        Returns:
            返回示例：
                None  # 交付任务已登记，相机清理回调已先于机器的释放回调
        """
        camera.delivery_task = asyncio.create_task(delivery_released.wait())
        camera.delivery_task.add_done_callback(clear_delivery_task)
        return start_test_capture(camera, capture_start_time)

    camera.start_capture = start_capture

    # 受理 START 后交付任务仍在运行，IO 中断先把本轮结算为失败。
    await machine.handle_machine_start()
    session = machine.active_session
    assert session is not None
    delivery_task = camera.delivery_task
    await machine.handle_event(RuntimeEvent(EventType.IO_INTERRUPTED, "1"))
    await wait_for_failure_audit(machine)
    assert session.state == SessionState.FAILED
    assert session.errors == ["IO 通信中断"]
    assert len(read_abnormal_events(database)) == 1
    assert session.session_id in machine.cycles

    # 同轮采集故障在交付结束前到达，只登记机器故障，本轮不重复结算。
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_FAILED, "1", session.session_id, "GetImageBuffer 失败"
    ))
    assert machine.machine_failure_reason == "相机采集失败"
    assert session.errors == ["IO 通信中断"]
    assert [(row[0], row[1], row[2]) for row in read_abnormal_events(database)] == [
        ("1", session.session_id, "IO 通信中断"),
    ]
    assert session.session_id in machine.cycles

    # 放行交付任务，相机清空引用后机器的交付回调完成释放。
    delivery_released.set()
    await asyncio.gather(delivery_task, return_exceptions=True)
    await asyncio.sleep(0)
    assert camera.delivery_task is None
    assert machine.active_session is None
    assert machine.machine_failure_reason == "相机采集失败"

    # 机器故障登记继续阻止下一轮 START。
    await machine.handle_machine_start()
    assert machine.active_session is None
    database.close()


@pytest.mark.asyncio
async def test_machine_failure_does_not_repeat_failed_session_settlement(
    tmp_path: Path,
) -> None:
    """确认机器故障收尾只登记故障，不负责释放已满足条件的失败周期。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 周期由显式释放清空，机器故障处理本身不触发释放
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))

    # 建立全部释放条件已满足、且交付任务已结束的失败周期。
    session.state = SessionState.FAILED
    session.errors = ["IO 通信中断"]
    machine.camera.delivery_task = None

    # 同轮采集故障交给已失败的周期，只登记机器故障。
    await machine.handle_machine_failure(session, "相机采集失败")
    assert machine.machine_failure_reason == "相机采集失败"
    assert session.errors == ["IO 通信中断"]
    assert read_abnormal_events(database) == []

    # 真实受理 START 时相机的交付结束回调会再次尝试释放，本测试绕过该回调。
    # 机器故障处理没有释放周期，显式调用释放才清空当前周期。
    assert session.session_id in machine.cycles
    machine.release_finished_session(session.session_id)
    assert machine.active_session is None
    database.close()


@pytest.mark.asyncio
async def test_ocr_notification_precedes_close_and_survives_release(tmp_path: Path) -> None:
    """验证有效 OCR 立即通知文字且后续结算不清空通知结果。

    Args:
        tmp_path: 临时存储目录。

    Returns:
        返回示例：
            None  # 文字先于 CLOSE 通知，旧事件被隔离且结果正常释放
    """
    frame = create_frame("session-1", "frame-1", b"image")
    machine, database, session, progress_updates, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    result = session.ocr_result
    session.ocr_result = None
    session.ocr_state = OCRState.RUNNING
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    notification = Mock()
    machine.notify_ocr_result = notification

    # 旧周期和未知周期不发送文字通知。
    await machine.handle_event(RuntimeEvent(EventType.OCR_COMPLETED, "1", "old", result))
    notification.assert_not_called()

    # 当前结果在现场关闭前立即发送，参数仅包含身份和文字。
    await machine.handle_event(RuntimeEvent(EventType.OCR_COMPLETED, "1", session.session_id, result))
    notification.assert_called_once_with(
        "1",
        session.session_id,
        result.recognized_lines,
    )
    assert session.capture_stop_time is None
    assert session.ocr_state == OCRState.COMPLETED

    # 重复结果被忽略，CLOSE 后正常提交并释放周期。
    await machine.handle_event(RuntimeEvent(EventType.OCR_COMPLETED, "1", session.session_id, result))
    await machine.handle_machine_close()
    await cycle.result_storage_task
    assert session.state == SessionState.COMMITTED
    assert session.ocr_result is None
    assert machine.active_session is None
    assert notification.call_count == 1

    # 正式入库文字与当前周期通知文字保持一致。
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT recognized_lines FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert json.loads(record[0]) == list(result.recognized_lines)
    database.close()


def create_storage_runtime(temporary_directory: Path, shutdown_timeout_ms: int = 1000):
    """建立使用临时数据库和模拟相机的结果保存运行时。

    Args:
        temporary_directory: 数据库和证据图片的测试目录。
        shutdown_timeout_ms: 退出时等待活动测量完成的毫秒数。

    Returns:
        返回示例：
            (
                SystemRuntime(...),  # 已启动机器事件监听的测试系统
                MachineRuntime(...),  # 当前待关闭的机器
                MeasurementSession(...),  # OCR 已完成且尚未关闭的测量
            )
    """
    # 建立已完成识别、尚未收到关闭信号的测量。
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(temporary_directory, (frame,))
    session.capture_stop_time = None
    machine.active_session_id = session.session_id
    session.measurement_frequencies = [FrequencyMeasurement(session.session_id, "meter-1", 50.0)]
    machine.camera.stop = AsyncMock()

    # 将测试机器接入真实系统事件入口和故障处理。
    config = replace(machine.config, io_machine_channels={"1": 0}, shutdown_timeout_ms=shutdown_timeout_ms)
    runtime = SystemRuntime(config)
    runtime.database = database
    runtime.machines = {"1": machine}
    runtime.accepting_signals = True
    runtime.camera_sdk = SimpleNamespace(close=Mock())
    machine.publish_event = runtime.publish_event
    machine.on_system_failure = runtime.handle_system_failure
    machine.state_changed = runtime.state_changed

    # 启动本机事件监听，保持现场信号的回执和处理顺序。
    runtime.worker_tasks.append(asyncio.create_task(runtime.run_worker("1号皮带机", machine.listen_events)))
    return runtime, machine, session


@pytest.mark.asyncio
async def test_slow_storage_does_not_block_other_machine_io(tmp_path: Path) -> None:
    """确认一台机器保存变慢时仍读取并处理另一台机器的启停脉冲。

    Args:
        tmp_path: 测试数据库和图片目录。

    Returns:
        返回示例：
            None  # 保存阻塞期间其他机器正常启停，本机可受理下一轮
    """
    # 让首台机器在保存证据前等待测试放行。
    runtime, machine, session = create_storage_runtime(tmp_path, shutdown_timeout_ms=20)
    cycle = machine.cycles[session.session_id]
    runtime.config = replace(runtime.config, io_machine_channels={"1": 0, "2": 1}, modbus_poll_interval_ms=1)
    storage_started = asyncio.Event()
    release_storage = threading.Event()
    loop = asyncio.get_running_loop()
    original_save = machine.save_evidence_images_and_measurement_record

    def save_after_release(record: MeasurementRecord, frames: tuple[MeasurementFrame, ...]) -> None:
        """等待放行后执行真实的测试文件和数据库保存。

        Args:
            record: 本轮测量记录。
            frames: 本轮证据帧。

        Returns:
            返回示例：
                None  # 测试结果已完整写入临时目录
        """
        loop.call_soon_threadsafe(storage_started.set)
        if not release_storage.wait(timeout=5):
            raise TimeoutError("测试未放行结果保存")
        original_save(record, frames)

    machine.save_evidence_images_and_measurement_record = Mock(side_effect=save_after_release)
    other_closed = asyncio.Event()
    other_camera = SimpleNamespace(
        available=True,
        is_capturing=False,
        delivery_task=loop.create_future(),
        start_capture=Mock(),
        inform_capture_workflow_stop=AsyncMock(),
        stop=AsyncMock(),
    )
    other_camera.unfinished_delivery_tasks = set()
    other_camera.start_capture.side_effect = (
        lambda session_id, started: start_test_capture(other_camera, started)
    )
    other_camera.delivery_task.set_result(None)

    # 建立另一台独立处理事件的机器。
    other_machine = MachineRuntime(
        replace(machine.machine_config, machine_id="2", machine_name="2号皮带机"),
        runtime.config,
        other_camera,
        SimpleNamespace(active_session_id=None),
        machine.text_recognizer,
        runtime.database,
        runtime.publish_event,
        None,
        runtime.handle_system_failure,
        runtime.state_changed,
        notify_cycle_closed=lambda machine_id, session_id: other_closed.set(),
    )
    runtime.machines["2"] = other_machine
    runtime.worker_tasks.append(asyncio.create_task(runtime.run_worker("2号皮带机", other_machine.listen_events)))
    runtime.io_previous_states = {0: True, 1: False}
    readings = iter(([False, False], [False, True], [False, False]))
    finish_polling = asyncio.Event()

    async def read_discrete_inputs(address: int, count: int) -> list[bool] | None:
        """先关闭首台机器，再交付另一台机器的启动和关闭脉冲。

        Args:
            address: DI 起始地址。
            count: 读取的 DI 通道数。

        Returns:
            返回示例：
                [False, True]  # 首台机器已关闭，另一台机器已启动
                None  # 测试结束，不再交付状态
        """
        assert count == 2
        reading = next(readings, None)
        if reading is None:
            await finish_polling.wait()
        elif reading[1]:
            await storage_started.wait()
        return reading

    runtime.modbus_client = SimpleNamespace(read_discrete_inputs=read_discrete_inputs, disconnect=AsyncMock())
    runtime.worker_tasks.append(asyncio.create_task(runtime.run_worker("Modbus IO", runtime.listen_io)))
    try:
        # 保存尚未放行时，另一台机器已经收到完整启停脉冲。
        await asyncio.wait_for(other_closed.wait(), timeout=1)
        assert storage_started.is_set()
        assert not release_storage.is_set()
        other_camera.start_capture.assert_called_once()
        assert other_machine.active_session is None
        other_session = next(iter(other_machine.cycles.values())).session
        assert other_session.capture_stop_time is not None
        assert session.state == SessionState.SAVING_RESULT
        assert session.session_id in machine.cycles
        assert machine.active_session is None
        assert session.final_frequency.value_hz == 50.0

        # 本机保存期间的新启动立即采集，旧轮仍只保存一次。
        await asyncio.wait_for(runtime.handle_start("1"), timeout=1)
        assert machine.active_session is not None
        assert machine.active_session.session_id != session.session_id
        await runtime.handle_close("1")
        await machine.try_finalize(session)
        assert session.session_id in machine.cycles
        assert machine.active_session is None
        machine.save_evidence_images_and_measurement_record.assert_called_once()

        # 放行保存后，首台机器完成入库并释放原周期。
        storage_task = cycle.result_storage_task
        release_storage.set()
        await asyncio.wait_for(storage_task, timeout=1)
        assert session.state == SessionState.COMMITTED
        assert machine.active_session is None
        assert runtime.failure is None
        with sqlite3.connect(runtime.config.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM measurement_records").fetchone()[0] == 1
    finally:
        # 放行所有模拟等待，按系统退出流程回收测试任务。
        release_storage.set()
        finish_polling.set()
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_timeout_ms", [1, 1000])
async def test_shutdown_waits_for_result_storage(tmp_path: Path, shutdown_timeout_ms: int) -> None:
    """确认正常退出和退出超时都等待已经开始的结果保存完成。

    Args:
        tmp_path: 测试数据库和图片目录。
        shutdown_timeout_ms: 活动测量的退出等待期限。

    Returns:
        返回示例：
            None  # 结果完整入库后才关闭驱动和数据库，成功周期未改为失败
    """
    runtime, machine, session = create_storage_runtime(tmp_path, shutdown_timeout_ms)
    cycle = machine.cycles[session.session_id]
    storage_started = asyncio.Event()
    release_storage = threading.Event()
    loop = asyncio.get_running_loop()
    original_save = machine.save_evidence_images_and_measurement_record

    def save_after_release(record: MeasurementRecord, frames: tuple[MeasurementFrame, ...]) -> None:
        """等待测试放行，然后写入本轮证据和结果。

        Args:
            record: 本轮测量记录。
            frames: 本轮证据帧。

        Returns:
            返回示例：
                None  # 本轮测试记录已保存
        """
        loop.call_soon_threadsafe(storage_started.set)
        if not release_storage.wait(timeout=5):
            raise TimeoutError("测试未放行结果保存")
        original_save(record, frames)

    machine.save_evidence_images_and_measurement_record = save_after_release
    shutdown_task = None
    try:
        # CLOSE 回执返回后，保存线程仍等待放行。
        await asyncio.wait_for(runtime.handle_close("1"), timeout=1)
        await asyncio.wait_for(storage_started.wait(), timeout=1)
        shutdown_task = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0.03)
        assert not shutdown_task.done()
        assert session.state == SessionState.SAVING_RESULT
        runtime.camera_sdk.close.assert_not_called()

        # 放行后核对退出完成、成功状态和真实入库记录。
        release_storage.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
        assert session.state == SessionState.COMMITTED
        assert session.errors == []
        assert cycle.result_storage_task is None
        assert machine.active_session is None
        assert runtime.failure is None
        runtime.camera_sdk.close.assert_called_once()
        with sqlite3.connect(runtime.config.database_path) as connection:
            assert connection.execute("SELECT session_id FROM measurement_records").fetchone() == (session.session_id,)
        assert read_abnormal_events(runtime.database) == []
    finally:
        # 防止断言失败留下等待中的线程和系统任务。
        release_storage.set()
        await runtime.stop()
        if shutdown_task is not None:
            await asyncio.gather(shutdown_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, ValueError])
async def test_background_storage_error_reaches_system_shutdown(tmp_path: Path, error_type: type[Exception]) -> None:
    """确认后台保存的数据库异常和未知异常都触发系统退出。

    Args:
        tmp_path: 测试数据库和图片目录。
        error_type: 注入保存线程的异常类型。

    Returns:
        返回示例：
            None  # 原始故障已上报，当前测量和系统资源已清理
    """
    runtime, machine, session = create_storage_runtime(tmp_path)
    cycle = machine.cycles[session.session_id]
    storage_error = error_type("测试结果保存失败")
    runtime.database.write_measurement_record = Mock(side_effect=storage_error)
    try:
        # CLOSE 回执只确认现场关闭处理，故障通过后台任务交给系统。
        await runtime.handle_close("1")
        with pytest.raises(error_type, match="测试结果保存失败") as captured_error:
            await asyncio.wait_for(runtime.wait_for_failure(), timeout=1)
        await runtime.stop()

        # 核对根因和退出结果，没有未处理的保存任务。
        assert captured_error.value is storage_error
        assert runtime.failure is storage_error
        assert runtime.failure_event.is_set()
        assert not runtime.accepting_signals
        assert session.state == SessionState.FAILED
        assert machine.active_session is None
        assert cycle.result_storage_task is None
        assert not runtime.worker_tasks
        runtime.camera_sdk.close.assert_called_once()
    finally:
        await runtime.stop()


def record_audit_started(database: Database) -> asyncio.Event:
    """记录测试审计线程开始写入的时刻，保留真实数据库操作。

    Args:
        database: 使用临时运行库的测试数据库。

    Returns:
        返回示例：
            asyncio.Event()  # 审计线程进入写入方法时被设置
    """
    # 记录进入写入方法的通知和原始方法。
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_save = database.save_abnormal_event

    def save_audit(*arguments, **keyword_arguments) -> None:
        """通知测试线程已开始，然后执行真实审计写入。

        Args:
            arguments: 审计写入的位置参数。
            keyword_arguments: 审计写入的关键字参数。

        Returns:
            返回示例：
                None  # 审计记录已写入临时运行库
        """
        loop.call_soon_threadsafe(started.set)
        original_save(*arguments, **keyword_arguments)

    database.save_abnormal_event = Mock(side_effect=save_audit)
    return started


@pytest.mark.asyncio
async def test_locked_failure_audit_does_not_block_other_machine_io(tmp_path: Path) -> None:
    """确认失败审计等待真实 SQLite 写锁时，另一台机器仍能收到完整启停信号。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 写锁未释放时仍可受理下一轮，旧轮只保留一份审计
    """
    # 建立等待采集结果的首台机器，并占住运行库写锁。
    runtime, machine, session = create_storage_runtime(tmp_path)
    cycle = machine.cycles[session.session_id]
    runtime.config = replace(runtime.config, io_machine_channels={"1": 0, "2": 1})
    runtime.io_previous_states = {0: True, 1: False}
    session.ocr_state = OCRState.WAITING
    audit_started = record_audit_started(runtime.database)
    lock = sqlite3.connect(runtime.config.recovery_path)
    lock.execute("BEGIN IMMEDIATE")

    # 建立可独立接收真实队列事件的第二台机器。
    loop = asyncio.get_running_loop()
    other_camera = SimpleNamespace(
        available=True,
        is_capturing=False,
        delivery_task=loop.create_future(),
        start_capture=Mock(),
        inform_capture_workflow_stop=AsyncMock(),
        stop=AsyncMock(),
    )
    other_camera.unfinished_delivery_tasks = set()
    other_camera.start_capture.side_effect = (
        lambda session_id, started: start_test_capture(other_camera, started)
    )
    other_camera.delivery_task.set_result(None)
    other_closed = Mock()
    other_machine = MachineRuntime(
        replace(machine.machine_config, machine_id="2", machine_name="2号皮带机"),
        runtime.config,
        other_camera,
        SimpleNamespace(active_session_id=None),
        machine.text_recognizer,
        runtime.database,
        runtime.publish_event,
        None,
        runtime.handle_system_failure,
        runtime.state_changed,
        notify_cycle_closed=other_closed,
    )
    runtime.machines["2"] = other_machine
    runtime.worker_tasks.append(asyncio.create_task(runtime.run_worker("2号皮带机", other_machine.listen_events)))
    try:
        # 空帧使首台机器失败，审计线程开始后仍因写锁无法提交。
        await runtime.publish_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", session.session_id, CaptureResult(frames=(), statistics={})
        ))
        await asyncio.wait_for(audit_started.wait(), timeout=1)
        audit_task = cycle.failure_audit_task
        assert audit_task is not None
        assert not audit_task.done()
        assert read_abnormal_events(runtime.database) == []

        # 同份 DI 先关闭首台机器再启动第二台，随后关闭第二台。
        await asyncio.wait_for(runtime.handle_io_states([False, True]), timeout=0.25)
        await asyncio.wait_for(runtime.handle_io_states([False, False]), timeout=0.25)
        other_camera.start_capture.assert_called_once()
        other_closed.assert_called_once()
        assert other_machine.active_session is None
        other_session = next(iter(other_machine.cycles.values())).session
        assert other_session.capture_stop_time is not None
        assert session.session_id in machine.cycles
        assert machine.active_session is None
        assert session.state == SessionState.FAILED
        assert session.capture_stop_time is not None
        assert not audit_task.done()

        # 首台机器受理下一周期，旧轮重复结算不再创建审计任务。
        await runtime.handle_start("1")
        assert machine.active_session is not None
        assert machine.active_session.session_id != session.session_id
        await runtime.handle_close("1")
        await machine.try_finalize(session)
        assert session.session_id in machine.cycles
        assert machine.active_session is None
        assert cycle.failure_audit_task is audit_task
        runtime.database.save_abnormal_event.assert_called_once()

        # 放开真实写锁后提交审计并释放首台机器的周期。
        lock.rollback()
        await asyncio.wait_for(audit_task, timeout=1)
        assert cycle.failure_audit_task is None
        assert machine.active_session is None
        assert runtime.failure is None
        assert read_abnormal_events(runtime.database)[0][:3] == ("1", session.session_id, "本轮未采集到图像")
    finally:
        # 所有失败路径都释放写锁并等待正常系统清理。
        lock.rollback()
        lock.close()
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_timeout_ms", [1, 1000])
async def test_shutdown_waits_for_failure_audit(tmp_path: Path, shutdown_timeout_ms: int) -> None:
    """确认退出触发的失败审计结束前，不关闭数据库或相机驱动。

    Args:
        tmp_path: pytest 提供的临时目录。
        shutdown_timeout_ms: 等待周期结算的期限，覆盖正常等待和超时退出。

    Returns:
        返回示例：
            None  # 写锁释放后审计完整保存，退出回收所有任务并关闭数据库
    """
    # 阻塞退出时产生的失败记录写入。
    runtime, machine, session = create_storage_runtime(tmp_path, shutdown_timeout_ms)
    cycle = machine.cycles[session.session_id]
    audit_started = record_audit_started(runtime.database)
    runtime.database.close = Mock(wraps=runtime.database.close)
    lock = sqlite3.connect(runtime.config.recovery_path)
    lock.execute("BEGIN IMMEDIATE")
    shutdown_task = asyncio.create_task(runtime.stop())
    try:
        # 退出流程等待审计，本轮保持失败状态和任务引用。
        await asyncio.wait_for(audit_started.wait(), timeout=1)
        await asyncio.sleep(0.03)
        assert not shutdown_task.done()
        assert session.state == SessionState.FAILED
        assert session.session_id in machine.cycles
        assert machine.active_session is None
        assert cycle.failure_audit_task is not None
        runtime.camera_sdk.close.assert_not_called()
        runtime.database.close.assert_not_called()
        assert read_abnormal_events(runtime.database) == []

        # 写锁释放后才完成退出和数据库关闭。
        lock.rollback()
        await asyncio.wait_for(shutdown_task, timeout=1)
        assert cycle.failure_audit_task is None
        assert machine.active_session is None
        assert runtime.failure is None
        assert not runtime.worker_tasks
        runtime.camera_sdk.close.assert_called_once()
        runtime.database.close.assert_called_once()
        assert read_abnormal_events(runtime.database)[0][:3] == ("1", session.session_id, "测量周期中断")
    finally:
        # 放行并回收退出任务，不留下后台线程。
        lock.rollback()
        lock.close()
        await runtime.stop()
        await asyncio.gather(shutdown_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_root_error", [False, True])
async def test_cancelled_failure_audit_drains_write_and_preserves_root_error(
    tmp_path: Path, has_root_error: bool
) -> None:
    """确认审计任务取消后仍等写入完成，系统故障优先保留已有根因。

    Args:
        tmp_path: pytest 提供的临时目录。
        has_root_error: 是否已有应优先上报的系统级原始异常。

    Returns:
        返回示例：
            None  # 线程已完成后才关闭数据库，根因或意外取消已交给系统故障入口
    """
    # 用真实写锁阻塞已开始的审计事务。
    runtime, machine, session = create_storage_runtime(tmp_path)
    cycle = machine.cycles[session.session_id]
    audit_started = record_audit_started(runtime.database)
    runtime.database.close = Mock(wraps=runtime.database.close)
    root_error = sqlite3.OperationalError("原始测量入库失败") if has_root_error else None
    lock = sqlite3.connect(runtime.config.recovery_path)
    lock.execute("BEGIN IMMEDIATE")
    try:
        await machine.handle_session_failure(session, "测试失败", system_error=root_error)
        await asyncio.wait_for(audit_started.wait(), timeout=1)
        audit_task = cycle.failure_audit_task

        # 连续取消不提前结束正在执行的数据库线程。
        audit_task.cancel()
        await asyncio.sleep(0)
        audit_task.cancel()
        await asyncio.sleep(0.03)
        assert not audit_task.done()
        assert machine.active_session is session
        assert runtime.failure is None
        runtime.database.close.assert_not_called()

        # 放行事务后才补抛取消，并由任务回调触发系统退出。
        lock.rollback()
        await asyncio.gather(audit_task, return_exceptions=True)
        await asyncio.wait_for(runtime.stop(), timeout=1)
        assert audit_task.cancelled()
        assert cycle.failure_audit_task is None
        assert machine.active_session is None
        assert read_abnormal_events(runtime.database)[0][:3] == ("1", session.session_id, "测试失败")
        if has_root_error:
            assert runtime.failure is root_error
        else:
            assert str(runtime.failure) == "测量失败记录保存任务意外取消"
        runtime.database.close.assert_called_once()
    finally:
        # 防止测试失败时遗留锁和系统任务。
        lock.rollback()
        lock.close()
        await runtime.stop()


def create_background_runtime(temporary_directory: Path):
    """建立使用真实周期主流程和可立即停流相机的测试系统。

    Args:
        temporary_directory: 临时数据库和证据根目录。

    Returns:
        返回示例：
            (
                SystemRuntime(...),  # 真实事件队列及退出流程
                MachineRuntime(...),  # 没有活动周期的测试机器
            )
    """
    # 复用临时数据库和机器事件监听。
    runtime, machine, _ = create_storage_runtime(temporary_directory, 20)
    machine.cycles.clear()
    machine.active_session_id = None
    machine.initialized = True
    machine.notify_machine_warning = Mock()

    # 模拟每次采集一帧并停止读帧，保留真实采集锁和交付任务。
    sdk_camera = SimpleNamespace(
        serial="camera-1", closed=False, faulted=False,
        capture_lock=threading.Lock(), received_frame_count=0,
        start_grabbing=Mock(), stop_grabbing=Mock(),
        encode_image=lambda frame: frame.image_bytes,
    )

    def read_frame(stop_requested: threading.Event, timeout_ms: int):
        """返回属于本次取流的原始帧字节。

        Args:
            stop_requested: 本轮现场采集停止通知。
            timeout_ms: 单次读帧期限。

        Returns:
            返回示例：
                SimpleNamespace(image_bytes=b"capture-1")  # 所属帧字节
        """
        sdk_camera.received_frame_count += 1
        stop_requested.set()
        return SimpleNamespace(
            image_bytes=f"capture-{sdk_camera.received_frame_count}".encode(),
        )

    sdk_camera.read_frame = Mock(side_effect=read_frame)
    machine.camera = Camera("1", "1号皮带机", 1000, 50,
                            runtime.publish_event, runtime.handle_system_failure)
    machine.camera.sdk_camera = sdk_camera

    # 根据所属 Session 封装证据，使用真实 OCR 共享锁。
    recognizer = TextRecognizer()

    def prepare_frames(session_id, capture_id, camera_serial, frames):
        """将原始帧封装为所属周期的识别和证据输入。

        Args:
            session_id: 所属周期编号。
            capture_id: 所属采集编号。
            camera_serial: 所属相机编号。
            frames: 所属原始帧。

        Returns:
            返回示例：
                (
                    (MeasurementFrame(...),),  # 全部证据帧
                    (MeasurementFrame(...),),  # 合格识别帧
                )
        """
        frame = create_frame(session_id, f"frame-{session_id}", frames[0].image_bytes)
        return (frame,), (frame,)

    def recognize_frames(session_id, measurement_frames, qualified_frames):
        """返回可区分不同周期的文字和证据。

        Args:
            session_id: 所属周期编号。
            measurement_frames: 本轮全部帧。
            qualified_frames: 本轮合格帧。

        Returns:
            返回示例：
                OCRResult(...)  # 同一周期的文字及证据帧对应关系
        """
        # 根据本轮帧标记生成对应的测试编号。
        frame_bytes = qualified_frames[0].camera_frame.image_bytes
        capture_number = frame_bytes.decode().split("-")[-1]
        return OCRResult(
            recognized_lines=(f"292621{capture_number}C",),
            selected_frames=qualified_frames,
            line_frame_ids=((qualified_frames[0].frame_id,),),
            review_frames=(), review_reason=None,
        )

    recognizer.prepare_frames_for_ocr = Mock(side_effect=prepare_frames)
    recognizer.recognize_qualified_frames = Mock(side_effect=recognize_frames)
    machine.text_recognizer = recognizer
    return runtime, machine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "background_stage",
    ["resource", "recognition", "storage", "audit"],
)
async def test_close_accepts_next_capture_during_background_stage(
    tmp_path: Path, background_stage: str,
) -> None:
    """确认旧轮处于各后台阶段时，CLOSE 后下一轮当场完成采集。

    Args:
        tmp_path: 临时数据库和证据目录。
        background_stage: 受控阻塞的后台阶段。

    Returns:
        返回示例：
            None  # 旧轮保留后台任务，第二轮完成取流且归属正确
    """
    # 用事件控制旧轮资源等待、识别、保存或失败审计。
    runtime, machine = create_background_runtime(tmp_path)
    loop = asyncio.get_running_loop()
    background_entered = asyncio.Event()
    release_thread = threading.Event()
    resource_release = asyncio.Event()
    resource_acquired = asyncio.Event()
    waiter_entered = asyncio.Event()
    resource_holder = None
    first_session_id = None
    original_recognize = machine.text_recognizer.recognize_qualified_frames.side_effect
    original_save = machine.save_evidence_images_and_measurement_record
    original_audit = runtime.database.save_abnormal_event

    def recognize_after_release(session_id, measurement_frames, qualified_frames):
        """等待旧轮识别放行并返回所属结果。

        Args:
            session_id: 所属周期编号。
            measurement_frames: 本轮全部帧。
            qualified_frames: 本轮合格帧。

        Returns:
            返回示例：
                OCRResult(...)  # 本轮识别结果
        """
        if background_stage == "recognition" and session_id == first_session_id:
            loop.call_soon_threadsafe(background_entered.set)
            assert release_thread.wait(5)
        return original_recognize(session_id, measurement_frames, qualified_frames)

    def save_after_release(record, frames):
        """阻塞旧轮保存后执行真实临时文件和数据库写入。

        Args:
            record: 所属测量记录。
            frames: 所属证据帧。

        Returns:
            返回示例：
                None  # 本轮结果已保存
        """
        if record.session_id == first_session_id:
            loop.call_soon_threadsafe(background_entered.set)
            assert release_thread.wait(5)
        original_save(record, frames)

    def audit_after_release(reason, *arguments, **keyword_arguments):
        """阻塞旧轮失败审计后写入临时运行库。

        Args:
            reason: 失败原因。
            arguments: 审计位置参数。
            keyword_arguments: 含所属周期编号的审计参数。

        Returns:
            返回示例：
                None  # 本轮失败审计已保存
        """
        if keyword_arguments.get("session_id") == first_session_id:
            loop.call_soon_threadsafe(background_entered.set)
            assert release_thread.wait(5)
        original_audit(reason, *arguments, **keyword_arguments)

    if background_stage in {"resource", "audit"}:
        resource_holder = asyncio.create_task(hold_ocr_processing_resource(
            machine.text_recognizer, resource_acquired, resource_release,
        ))
        await resource_acquired.wait()
    original_acquire = machine.text_recognizer.acquire_ocr_access

    @asynccontextmanager
    async def acquire_and_notify(timeout_seconds):
        """记录本轮实际申请共享识别资源的时刻。

        Args:
            timeout_seconds: 共享锁等待期限。

        Returns:
            返回示例：
                yield None  # 已取得共享 OCR 资源
        """
        waiter_entered.set()
        async with original_acquire(timeout_seconds):
            yield

    machine.text_recognizer.acquire_ocr_access = acquire_and_notify
    machine.text_recognizer.recognize_qualified_frames.side_effect = (
        recognize_after_release
    )
    if background_stage == "storage":
        machine.save_evidence_images_and_measurement_record = save_after_release
    if background_stage == "audit":
        runtime.database.save_abnormal_event = audit_after_release

    try:
        # 受理第一轮，等待相机实际取流和帧交付结束。
        await runtime.handle_start("1")
        first = machine.active_session
        first_session_id = first.session_id
        first_cycle = machine.cycles[first_session_id]
        await first_cycle.delivery_task
        await machine.wait_until_event_queue_drained()
        assert first_cycle.capture_task.duration_seconds == 1.0
        if background_stage == "resource":
            await asyncio.wait_for(waiter_entered.wait(), 1)
        if background_stage == "recognition":
            await asyncio.wait_for(background_entered.wait(), 1)
        if background_stage == "audit":
            await machine.handle_event(RuntimeEvent(
                EventType.OCR_FAILED, "1", first_session_id, "本轮 OCR 执行失败",
            ))
            await asyncio.wait_for(background_entered.wait(), 1)
        if background_stage == "storage":
            recognized = asyncio.Event()
            machine.notify_ocr_result = lambda *arguments: recognized.set()
            if first.ocr_state != OCRState.COMPLETED:
                await asyncio.wait_for(recognized.wait(), 1)

        # CLOSE 解除现场占用，后台上下文继续保留。
        await runtime.handle_close("1")
        if background_stage == "storage":
            await asyncio.wait_for(background_entered.wait(), 1)
        assert machine.active_session_id is None
        assert first_session_id in machine.cycles
        if background_stage == "resource":
            assert first_cycle.recognition_task is not None
            assert not first_cycle.recognition_task.done()

        # 第二轮 START 立即受理，并在旧轮放行前完成实际采集。
        await runtime.handle_start("1")
        second = machine.active_session
        assert second is not None and second.session_id != first_session_id
        second_cycle = machine.cycles[second.session_id]
        await second_cycle.delivery_task
        await machine.wait_until_event_queue_drained()
        assert second_cycle.capture_task.capture_finished.is_set()
        assert second.capture_summary["retained_frame_count"] == 1
        assert machine.camera.sdk_camera.start_grabbing.call_count == 2
        assert len(machine.cycles) == 2
        assert first_session_id in machine.cycles
        assert not release_thread.is_set()
        assert machine.frequency_adapter.active_session_id == second.session_id
        assert runtime.failure is None
    finally:
        # 放行所有受控线程和共享资源，等待真实退出收尾。
        release_thread.set()
        resource_release.set()
        if resource_holder is not None:
            await resource_holder
        await runtime.stop()
        assert not machine.cycles


@pytest.mark.asyncio
async def test_interleaved_cycles_save_distinct_results_once(tmp_path: Path) -> None:
    """确认后轮先入库时，两轮文字、频率、证据仍各自保存一次。

    Args:
        tmp_path: 临时数据库和证据目录。

    Returns:
        返回示例：
            None  # 两条记录及证据分别归属各自 Session，没有重复提交
    """
    # 阻塞第一轮保存，允许第二轮先完成。
    runtime, machine = create_background_runtime(tmp_path)
    loop = asyncio.get_running_loop()
    first_storage_started = asyncio.Event()
    first_storage_release = threading.Event()
    original_save = machine.save_evidence_images_and_measurement_record
    saved_session_ids = []
    first_session_id = None
    recognition_notifications = asyncio.Queue()
    machine.notify_ocr_result = (
        lambda *arguments: recognition_notifications.put_nowait(arguments)
    )

    def save_in_controlled_order(record, frames):
        """按测试放行顺序写入每轮完整结果。

        Args:
            record: 所属周期的正式记录。
            frames: 所属证据帧。

        Returns:
            返回示例：
                None  # 所属周期的结果已保存
        """
        if record.session_id == first_session_id:
            loop.call_soon_threadsafe(first_storage_started.set)
            assert first_storage_release.wait(5)
        original_save(record, frames)
        saved_session_ids.append(record.session_id)

    machine.save_evidence_images_and_measurement_record = save_in_controlled_order
    try:
        # 第一轮形成结果并开始后台保存。
        await runtime.handle_start("1")
        first = machine.active_session
        first_session_id = first.session_id
        first_cycle = machine.cycles[first_session_id]
        first_notification = await asyncio.wait_for(recognition_notifications.get(), 1)
        assert first_notification[1] == first_session_id
        await machine.handle_event(RuntimeEvent(
            EventType.FREQUENCY_MEASURED, "1", first_session_id,
            FrequencyMeasurement(first_session_id, "meter-1", 41.0),
        ))
        await runtime.handle_close("1")
        await asyncio.wait_for(first_storage_started.wait(), 1)
        first_storage_task = first_cycle.result_storage_task

        # 第二轮完成采集、文字识别和频率接收。
        await runtime.handle_start("1")
        second = machine.active_session
        second_cycle = machine.cycles[second.session_id]
        second_notification = await asyncio.wait_for(recognition_notifications.get(), 1)
        assert second_notification[1] == second.session_id
        await machine.handle_event(RuntimeEvent(
            EventType.FREQUENCY_MEASURED, "1", second.session_id,
            FrequencyMeasurement(second.session_id, "meter-1", 42.0),
        ))

        # 旧 CLOSE、旧频率、旧结果和重复结算均不影响新轮或另建保存任务。
        await machine.handle_event(RuntimeEvent(
            EventType.MACHINE_CLOSED, "1", first_session_id,
        ))
        await machine.handle_event(RuntimeEvent(
            EventType.FREQUENCY_MEASURED, "1", first_session_id,
            FrequencyMeasurement(first_session_id, "meter-1", 99.0),
        ))
        await machine.handle_event(RuntimeEvent(
            EventType.OCR_COMPLETED, "1", first_session_id, None,
        ))
        await machine.try_finalize(first)
        assert first_cycle.result_storage_task is first_storage_task
        assert machine.active_session is second
        assert machine.frequency_adapter.active_session_id == second.session_id
        assert [
            reading.value_hz for reading in second.measurement_frequencies
        ] == [42.0]

        # 后轮先提交，旧轮再提交，完成回调各自回收所属上下文。
        await runtime.handle_close("1")
        await second_cycle.result_storage_task
        first_storage_release.set()
        await first_storage_task
        machine.handle_result_storage_finished(first_storage_task, first_session_id)
        await runtime.wait_until_idle(1)
        assert saved_session_ids == [second.session_id, first_session_id]
        assert not machine.cycles
        with sqlite3.connect(runtime.config.database_path) as connection:
            rows = connection.execute(
                "SELECT session_id, recognized_lines, final_frequency_hz, "
                "measurement_frequencies, evidence_directory FROM measurement_records"
            ).fetchall()
        assert len(rows) == 2
        for session, frequency, image_bytes, recognized_line in (
            (first, 41.0, b"capture-1", "2926211C"),
            (second, 42.0, b"capture-2", "2926212C"),
        ):
            row = next(row for row in rows if row[0] == session.session_id)
            assert json.loads(row[1]) == [recognized_line]
            assert row[2] == frequency
            assert json.loads(row[3]) == [{
                "session_id": session.session_id,
                "frequency_meter_serial": "meter-1",
                "value_hz": frequency,
            }]
            directory = Path(row[4])
            assert directory.name == session.session_id
            image_path = directory / f"frame-{session.session_id}.jpg"
            assert image_path.read_bytes() == image_bytes
        assert runtime.failure is None
    finally:
        first_storage_release.set()
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_event_type",
    [EventType.CAPTURE_COMPLETED, EventType.OCR_COMPLETED],
)
async def test_capacity_waits_for_queued_frames_and_bounds_refusal_audit(
    tmp_path: Path, frame_event_type: EventType,
) -> None:
    """确认失败轮的排队帧占用容量，拒收不补拍且审计任务有界。

    Args:
        tmp_path: 临时数据库和证据目录。
        frame_event_type: 带未消费帧的采集或识别事件。

    Returns:
        返回示例：
            None  # 帧丢弃后恢复名额，真实 CLOSE 后才能再次启动
    """
    # 保留已失败但帧尚未消费的周期和另一后台周期。
    frame = create_frame("session-1", "frame-1", b"old-frame")
    machine, database, first, _, _ = create_machine(tmp_path, (frame,))
    first.state = SessionState.FAILED
    first_cycle = machine.cycles[first.session_id]
    first_cycle.capture_result_pending = frame_event_type == EventType.CAPTURE_COMPLETED
    first_cycle.ocr_result_pending = frame_event_type == EventType.OCR_COMPLETED
    delivery_task = asyncio.get_running_loop().create_future()
    delivery_task.set_result(None)
    first_cycle.delivery_task = delivery_task
    second = replace(first, session_id="session-2", state=SessionState.RUNNING)
    machine.cycles[second.session_id] = CycleContext(second)
    payload = (
        CaptureResult((frame,), {})
        if frame_event_type == EventType.CAPTURE_COMPLETED else first.ocr_result
    )
    machine.queue.put_nowait(RuntimeEvent(
        frame_event_type, "1", first.session_id, payload,
    ))
    machine.notify_machine_warning = Mock()
    audit_started = asyncio.Event()
    audit_release = threading.Event()
    loop = asyncio.get_running_loop()
    original_audit = database.save_abnormal_event

    def save_refusal_after_release(reason, *arguments, **keyword_arguments):
        """阻塞拒收审计后写入临时运行库。

        Args:
            reason: 拒收或周期失败原因。
            arguments: 审计位置参数。
            keyword_arguments: 机器、容量和占用数量等审计参数。

        Returns:
            返回示例：
                None  # 对应异常事件已保存
        """
        if "后台处理积压" in reason:
            loop.call_soon_threadsafe(audit_started.set)
            assert audit_release.wait(5)
        original_audit(reason, *arguments, **keyword_arguments)

    database.save_abnormal_event = Mock(side_effect=save_refusal_after_release)
    try:
        # 两次物理启动都拒收，慢审计期间只有一个拒收任务。
        await machine.handle_machine_start()
        await asyncio.wait_for(audit_started.wait(), 1)
        refusal_task = machine.rejected_start_audit_task
        await machine.handle_machine_close()
        await machine.handle_machine_start()
        assert machine.rejected_start_audit_task is refusal_task
        database.save_abnormal_event.assert_called_once()
        machine.camera.start_capture.assert_not_called()
        assert len(machine.cycles) == 2
        assert machine.machine_failure_reason is None
        assert machine.notify_machine_warning.call_args.args == (
            "1", "后台处理积压，本次启动未采集，请暂停换带",
        )

        # 交付任务即使已结束，尚未消费的帧仍阻止容量回收。
        machine.release_finished_session(first.session_id)
        assert first.session_id in machine.cycles
        machine.discard_pending_events()
        assert first.session_id in machine.cycles
        machine.handle_delivery_finished(delivery_task, first.session_id)
        assert first_cycle.delivery_task is None
        assert first.session_id not in machine.cycles
        await machine.handle_machine_start()
        machine.camera.start_capture.assert_not_called()

        # 真实 CLOSE 复位后只采集新的 START，不补拍被拒收皮带。
        await machine.handle_machine_close()
        await machine.handle_machine_start()
        assert machine.active_session is not None
        assert machine.active_session.session_id not in {
            first.session_id, second.session_id,
        }
        machine.camera.start_capture.assert_called_once()
        audit_release.set()
        await refusal_task
        rows = read_abnormal_events(database)
        assert rows[0][0] == "1"
        assert json.loads(rows[0][3]) == {"capacity": 2, "occupied": 2}
        machine.on_system_failure.assert_not_called()
    finally:
        audit_release.set()
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_cancelled_ocr_thread_keeps_capacity_until_actual_finish(tmp_path: Path):
    """确认失败 OCR 的底层线程仍占用名额，线程真正结束后才恢复启动。

    Args:
        tmp_path: 临时数据库和证据目录。

    Returns:
        返回示例：
            None  # cancel 不提前释放线程、共享锁或周期容量
    """
    # 启动已 CLOSE 旧轮的阻塞识别线程。
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, first, _, _ = create_machine(tmp_path, (frame,))
    first.ocr_state = OCRState.WAITING
    first.ocr_result = None
    thread_started = asyncio.Event()
    thread_release = threading.Event()
    loop = asyncio.get_running_loop()
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(return_value=((frame,), (frame,)))

    def recognize_after_release(*arguments):
        """等待放行旧识别线程。

        Args:
            arguments: 旧轮识别输入。

        Returns:
            返回示例：
                None  # 旧线程已结束，结果随取消丢弃
        """
        loop.call_soon_threadsafe(thread_started.set)
        assert thread_release.wait(5)

    recognizer.recognize_qualified_frames = recognize_after_release
    machine.text_recognizer = recognizer
    try:
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", first.session_id,
            CaptureResult((frame.camera_frame,), {}),
        ))
        await asyncio.wait_for(thread_started.wait(), 1)
        old_task = machine.cycles[first.session_id].recognition_task
        await machine.handle_event(RuntimeEvent(
            EventType.OCR_TIMEOUT, "1", first.session_id,
        ))
        await wait_for_failure_audit(machine)
        assert not old_task.done()
        assert first.session_id in machine.cycles

        # 第二轮后台尚未结算，第三轮必须明确拒收。
        second = replace(first, session_id="second", state=SessionState.RUNNING,
                         ocr_state=OCRState.WAITING, errors=[])
        machine.cycles[second.session_id] = CycleContext(second)
        machine.notify_machine_warning = Mock()
        await machine.handle_machine_start()
        machine.camera.start_capture.assert_not_called()
        assert machine.waiting_cycle_reset
        machine.notify_machine_warning.assert_called_once()

        # 底层线程完成之前不能进入共享引擎，完成之后才归还容量。
        with pytest.raises(OCRResourceWaitTimeoutError):
            async with recognizer.acquire_ocr_access(0.01):
                pytest.fail("旧线程仍在识别")
        thread_release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        assert first.session_id not in machine.cycles
        await machine.handle_machine_close()
        await machine.handle_machine_start()
        machine.camera.start_capture.assert_called_once()
        assert machine.active_session.session_id != second.session_id
    finally:
        thread_release.set()
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_shutdown_completes_queued_ocr_during_grace_period(tmp_path: Path) -> None:
    """确认已 CLOSE 的周期在退出等待期间仍可完成识别和正式保存。

    Args:
        tmp_path: 临时数据库和证据目录。

    Returns:
        返回示例：
            None  # 退出等待期间完成的识别正常提交，不生成失败审计
    """
    # 占用识别资源，保留尚未完成 OCR 的已关闭周期。
    runtime, machine = create_background_runtime(tmp_path)
    runtime.config = replace(runtime.config, shutdown_timeout_ms=5000)
    resource_acquired = asyncio.Event()
    resource_release = asyncio.Event()
    resource_task = asyncio.create_task(hold_ocr_processing_resource(
        machine.text_recognizer, resource_acquired, resource_release,
    ))

    # 记录真实退出流程开始等待周期结算的时刻。
    drain_started = asyncio.Event()
    original_wait_until_idle = runtime.wait_until_idle
    shutdown_task = None

    async def record_drain_started(timeout_seconds: float) -> None:
        """通知测试退出等待已开始并继续执行真实结算等待。

        Args:
            timeout_seconds: 本次退出等待的最长秒数。

        Returns:
            返回示例：
                None  # 全部周期已结算
        """
        drain_started.set()
        await original_wait_until_idle(timeout_seconds)

    runtime.wait_until_idle = record_drain_started
    try:
        await asyncio.wait_for(resource_acquired.wait(), 1)
        await runtime.handle_start("1")
        session = machine.active_session

        # 明确等待模拟帧采集完成，再测试已关闭周期的退出宽限。
        capture = machine.cycles[session.session_id].capture_task
        await asyncio.wait_for(capture.capture_finished.wait(), 1)
        session.measurement_frequencies = [FrequencyMeasurement(session.session_id, "meter-1", 50.0)]
        await runtime.handle_close("1")

        # 退出已进入正常等待阶段时才归还识别资源。
        shutdown_task = asyncio.create_task(runtime.stop())
        await asyncio.wait_for(drain_started.wait(), 1)
        assert session.state == SessionState.RUNNING
        assert not runtime.releasing_resources
        assert not shutdown_task.done()
        runtime.camera_sdk.close.assert_not_called()
        resource_release.set()
        await asyncio.wait_for(shutdown_task, 1)

        # 核对正常提交的结果和完整资源回收。
        assert session.state == SessionState.COMMITTED
        assert session.errors == []
        assert not machine.cycles
        assert runtime.failure is None
        runtime.camera_sdk.close.assert_called_once()
        assert read_abnormal_events(runtime.database) == []
        with sqlite3.connect(runtime.config.database_path) as connection:
            saved_rows = connection.execute("SELECT session_id FROM measurement_records").fetchall()
            assert saved_rows == [(session.session_id,)]
    finally:
        # 断言失败时也归还识别资源并等待系统退出。
        resource_release.set()
        await resource_task
        await runtime.stop()
        if shutdown_task is not None:
            await shutdown_task


@pytest.mark.asyncio
async def test_shutdown_drains_multiple_background_storage_recognition_and_audit(
    tmp_path: Path,
):
    """确认停止监测等待多轮保存、识别线程和审计全部结束才关闭资源。

    Args:
        tmp_path: 临时数据库和证据目录。

    Returns:
        返回示例：
            None  # 正式结果完整保存，各失败轮只审计一次，驱动最后关闭
    """
    # 准备三轮可分别放行的后台任务。
    runtime, machine = create_background_runtime(tmp_path)
    machine.config = replace(machine.config, max_inflight_cycles=3)
    loop = asyncio.get_running_loop()
    storage_started = asyncio.Event()
    recognition_started = asyncio.Event()
    audit_started = asyncio.Event()
    resource_release_started = asyncio.Event()
    storage_release = threading.Event()
    recognition_release = threading.Event()
    audit_release = threading.Event()
    recognized = asyncio.Queue()
    machine.notify_ocr_result = lambda *arguments: recognized.put_nowait(arguments)
    original_save = machine.save_evidence_images_and_measurement_record
    original_recognize = machine.text_recognizer.recognize_qualified_frames.side_effect
    original_audit = runtime.database.save_abnormal_event
    original_camera_stop = machine.camera.stop
    storage_session_id = None
    recognition_session_id = None
    audit_session_id = None
    shutdown_task = None
    runtime.database.close = Mock(wraps=runtime.database.close)

    def save_after_release(record, frames):
        """等待正式保存放行再写入临时数据。

        Args:
            record: 所属记录。
            frames: 所属证据帧。

        Returns:
            返回示例：
                None  # 正式结果已写入
        """
        if record.session_id == storage_session_id:
            loop.call_soon_threadsafe(storage_started.set)
            assert storage_release.wait(5)
        original_save(record, frames)

    def recognize_after_release(session_id, measurement_frames, qualified_frames):
        """等待第二轮实际识别线程放行。

        Args:
            session_id: 所属周期。
            measurement_frames: 全部本轮帧。
            qualified_frames: 本轮合格帧。

        Returns:
            返回示例：
                OCRResult(...)  # 本轮识别结果
        """
        if session_id == recognition_session_id:
            loop.call_soon_threadsafe(recognition_started.set)
            assert recognition_release.wait(5)
        return original_recognize(session_id, measurement_frames, qualified_frames)

    def audit_after_release(reason, *arguments, **keyword_arguments):
        """等待第三轮审计放行再写入临时运行库。

        Args:
            reason: 本轮失败原因。
            arguments: 审计位置参数。
            keyword_arguments: 所属周期等审计信息。

        Returns:
            返回示例：
                None  # 本轮审计已保存
        """
        if keyword_arguments.get("session_id") == audit_session_id:
            loop.call_soon_threadsafe(audit_started.set)
            assert audit_release.wait(5)
        original_audit(reason, *arguments, **keyword_arguments)

    async def stop_camera_and_record_release() -> None:
        """完成退出停流并通知测试已进入资源释放阶段。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 相机交付已结束，退出流程继续取消识别任务
        """
        await original_camera_stop()
        resource_release_started.set()

    machine.camera.stop = stop_camera_and_record_release
    machine.save_evidence_images_and_measurement_record = save_after_release
    machine.text_recognizer.recognize_qualified_frames.side_effect = (
        recognize_after_release
    )
    runtime.database.save_abnormal_event = Mock(side_effect=audit_after_release)
    try:
        # 第一轮已 CLOSE，后台保存等待放行。
        await runtime.handle_start("1")
        storage_session = machine.active_session
        storage_session_id = storage_session.session_id
        await asyncio.wait_for(recognized.get(), 1)
        await runtime.handle_close("1")
        await asyncio.wait_for(storage_started.wait(), 1)
        storage_task = machine.cycles[storage_session_id].result_storage_task

        # 第二轮已 CLOSE，底层识别线程继续持有引擎。
        await runtime.handle_start("1")
        recognition_session = machine.active_session
        recognition_session_id = recognition_session.session_id
        await asyncio.wait_for(recognition_started.wait(), 1)
        await runtime.handle_close("1")
        recognition_task = machine.cycles[recognition_session_id].recognition_task

        # 第三轮已 CLOSE，失败审计继续等待。
        await runtime.handle_start("1")
        audit_session = machine.active_session
        audit_session_id = audit_session.session_id
        await runtime.handle_close("1")
        await machine.handle_session_failure(audit_session, "第三轮测试失败")
        await asyncio.wait_for(audit_started.wait(), 1)
        audit_task = machine.cycles[audit_session_id].failure_audit_task
        assert machine.active_session_id is None
        assert len(machine.cycles) == 3

        # 退出不取消已开始保存或审计，仍等待阻塞的实际识别线程。
        shutdown_task = asyncio.create_task(runtime.stop())
        storage_release.set()
        await storage_task
        audit_release.set()
        await audit_task
        await asyncio.wait_for(resource_release_started.wait(), 1)
        assert runtime.releasing_resources
        assert not shutdown_task.done()
        assert not recognition_task.done()
        runtime.camera_sdk.close.assert_not_called()
        runtime.database.close.assert_not_called()
        recognition_release.set()
        await asyncio.wait_for(shutdown_task, 1)

        # 正式成功轮不重复失败，失败审计与 Session 分别对应。
        assert storage_session.state == SessionState.COMMITTED
        assert recognition_task.cancelled()
        assert recognition_session.state == SessionState.FAILED
        assert audit_session.state == SessionState.FAILED
        assert not machine.cycles
        assert runtime.failure is None
        runtime.camera_sdk.close.assert_called_once()
        runtime.database.close.assert_called_once()
        audit_rows = read_abnormal_events(runtime.database)
        assert sorted(row[1] for row in audit_rows) == sorted([
            recognition_session_id, audit_session_id,
        ])
        with sqlite3.connect(runtime.config.database_path) as connection:
            saved_rows = connection.execute(
                "SELECT session_id FROM measurement_records"
            ).fetchall()
            assert saved_rows == [
                (storage_session_id,),
            ]
    finally:
        storage_release.set()
        recognition_release.set()
        audit_release.set()
        await runtime.stop()
        if shutdown_task is not None:
            await shutdown_task


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, ValueError])
async def test_old_storage_failure_stops_new_cycle_and_preserves_root_cause(
    tmp_path: Path, error_type: type[Exception],
):
    """确认旧轮系统级保存故障仍停止新轮且保留原始根因。

    Args:
        tmp_path: 临时数据库和证据目录。
        error_type: 数据库或未分类保存异常类型。

    Returns:
        返回示例：
            None  # 新轮已停止，所有任务已回收，后续清理故障不覆盖根因
    """
    # 阻塞旧轮保存到新轮完成采集之后再抛出根因。
    runtime, machine = create_background_runtime(tmp_path)
    loop = asyncio.get_running_loop()
    storage_started = asyncio.Event()
    storage_release = threading.Event()
    recognized = asyncio.Queue()
    machine.notify_ocr_result = lambda *arguments: recognized.put_nowait(arguments)
    root_error = error_type("旧轮保存原始故障")

    def fail_after_release(record, frames):
        """等待放行后抛出旧轮保存根因。

        Args:
            record: 旧轮测量记录。
            frames: 旧轮证据帧。

        Returns:
            返回示例：
                None  # 实际抛出原始异常
        """
        loop.call_soon_threadsafe(storage_started.set)
        assert storage_release.wait(5)
        raise root_error

    machine.save_evidence_images_and_measurement_record = fail_after_release
    try:
        await runtime.handle_start("1")
        old = machine.active_session
        await asyncio.wait_for(recognized.get(), 1)
        await runtime.handle_close("1")
        await asyncio.wait_for(storage_started.wait(), 1)
        await runtime.handle_start("1")
        new = machine.active_session
        assert (await asyncio.wait_for(recognized.get(), 1))[1] == new.session_id
        assert new.capture_summary["retained_frame_count"] == 1
        runtime.database.save_abnormal_event = Mock(side_effect=RuntimeError("随后审计故障"))
        storage_release.set()
        with pytest.raises(error_type) as captured:
            await asyncio.wait_for(runtime.wait_for_failure(), 1)
        await runtime.stop()
        assert captured.value is root_error
        assert runtime.failure is root_error
        assert old.state == SessionState.FAILED
        assert new.state == SessionState.FAILED
        assert not machine.cycles
        assert not machine.camera.unfinished_delivery_tasks
        assert not runtime.worker_tasks
        runtime.camera_sdk.close.assert_called_once()
    finally:
        storage_release.set()
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_event_type",
    [EventType.OCR_LOCK_WAIT_TIMEOUT, EventType.OCR_TIMEOUT, EventType.OCR_FAILED],
)
async def test_old_ordinary_failure_and_duplicate_events_leave_new_cycle_untouched(
    tmp_path: Path,
    failure_event_type: EventType,
):
    """确认旧轮普通超时与重复事件只失败旧轮，不中断新轮现场采集。

    Args:
        tmp_path: 临时数据库和证据目录。
        failure_event_type: 旧轮普通识别失败或超时事件。

    Returns:
        返回示例：
            None  # 新轮现场、频率和识别状态保持有效，旧轮只审计一次
    """
    frame = create_frame("session-1", "frame-1", b"old-image")
    machine, database, old, _, _ = create_machine(tmp_path, (frame,))
    old.ocr_state = OCRState.RUNNING
    old.ocr_result = None
    try:
        # 在旧轮尚未识别完成时，受理下一轮现场 START。
        await machine.handle_machine_start()
        new = machine.active_session
        new_cycle = machine.cycles[new.session_id]
        machine.notify_cycle_closed = Mock()
        machine.notify_camera_state = Mock()
        await machine.handle_event(RuntimeEvent(
            failure_event_type, "1", old.session_id, "旧轮错误",
        ))
        await wait_for_failure_audit(machine)
        original_errors = tuple(old.errors)

        # 重复超时、旧 CLOSE 和旧 IO 中断均不触碰新轮。
        for event_type in (
            EventType.OCR_LOCK_WAIT_TIMEOUT, EventType.OCR_FAILED,
            EventType.OCR_TIMEOUT, EventType.CYCLE_TIMEOUT,
            EventType.MACHINE_CLOSED, EventType.IO_INTERRUPTED,
            EventType.CAPTURE_FAILED,
        ):
            await machine.handle_event(RuntimeEvent(
                event_type, "1", old.session_id, "旧轮错误",
            ))
        assert old.state == SessionState.FAILED
        assert tuple(old.errors) == original_errors
        assert len(read_abnormal_events(database)) == 1
        assert machine.active_session is new
        assert new.state == SessionState.RUNNING
        assert new.ocr_state == OCRState.WAITING
        assert new.frequency_state == FrequencyState.RUNNING
        assert new.errors == []
        assert machine.frequency_adapter.active_session_id == new.session_id
        assert not machine.waiting_cycle_reset
        assert not new_cycle.capture_task.stop_requested.is_set()
        machine.camera.inform_capture_workflow_stop.assert_not_awaited()
        machine.notify_cycle_closed.assert_not_called()
        machine.notify_camera_state.assert_not_called()
        machine.on_system_failure.assert_not_called()
    finally:
        await machine.release_resources("测试结束")
        database.close()


@pytest.mark.asyncio
async def test_cancelled_ocr_enqueue_discards_undelivered_frame_payload(tmp_path: Path):
    """确认识别帧尚未入队时取消交付，实际任务结束后可回收容量。

    Args:
        tmp_path: 临时数据库和证据目录。

    Returns:
        返回示例：
            None  # 取消交付没有留下待消费帧标志或周期名额
    """
    # 用真实有界分发入口阻塞 OCR 结果入队。
    frame = create_frame("session-1", "frame-1", b"old-image")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    cycle = machine.cycles[session.session_id]
    runtime = SystemRuntime(machine.config)
    runtime.machines = {"1": machine}
    machine.queue = asyncio.Queue(1)
    machine.queue.put_nowait(RuntimeEvent(EventType.MACHINE_STARTED, "1"))
    enqueue_started = asyncio.Event()
    recognizer = TextRecognizer()
    recognizer.prepare_frames_for_ocr = Mock(return_value=((frame,), ()))
    machine.text_recognizer = recognizer

    async def publish_result(event):
        """通知识别结果已开始排队并沿真实入口交付。

        Args:
            event: 含本轮证据帧的 OCR 结果事件。

        Returns:
            返回示例：
                None  # 结果已入队或入队等待被取消
        """
        enqueue_started.set()
        await runtime.publish_event(event)

    machine.publish_event = publish_result
    cycle.recognition_task = asyncio.create_task(
        machine.run_ocr_pipeline(session, (frame,))
    )
    cycle.recognition_task.add_done_callback(
        lambda task: machine.handle_recognition_task_finished(task, session.session_id)
    )
    recognition_task = cycle.recognition_task
    try:
        await asyncio.wait_for(enqueue_started.wait(), 1)
        assert cycle.ocr_result_pending
        assert not recognition_task.done()
        await machine.handle_session_failure(session, "排队期间周期失败")
        await asyncio.gather(recognition_task, return_exceptions=True)
        await wait_for_failure_audit(machine)
        assert recognition_task.cancelled()
        assert not cycle.ocr_result_pending
        assert session.session_id not in machine.cycles
        assert machine.queue.qsize() == 1
        machine.on_system_failure.assert_not_called()
    finally:
        await machine.release_resources("测试结束")
        database.close()
