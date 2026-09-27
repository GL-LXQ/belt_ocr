"""验证机器测量结果保存与 OCR 任务生命周期。"""

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from async_utils import run_blocking_operation
from camera.camera import Camera
from config_util import AppConfig, MachineConfig
from camera.hikrobot_sdk import MvsError
from database import CommitIntegrityConflictError, Database, MeasurementRecord
from enums import EventType, OCRState, ProgressStage, ProgressStatus, SessionState
from machine import EvidenceWriteError, ImageEncodingError, Machine
from models import (
    BeltSession,
    CaptureResult,
    CapturedFrame,
    FrequencyMeasurement,
    OCRResult,
    RuntimeEvent,
)
from text_recognition import OCRProcessingError


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


def create_frame(session_id: str, frame_id: str, image_data: bytes) -> CapturedFrame:
    """建立带图片内容的测试帧。

    Args:
        session_id: 测量周期编号。
        frame_id: 图片编号。
        image_data: 图片测试内容。

    Returns:
        返回示例：
            CapturedFrame(...)  # 带编号和图片内容的测试帧
    """
    return CapturedFrame(
        session_id=session_id,
        capture_id="capture-1",
        camera_serial="camera-1",
        frame_id=frame_id,
        captured_at="2026-09-23T00:00:00+00:00",
        captured_monotonic=0.0,
        camera_frame=SimpleNamespace(data=image_data),
    )


def create_machine(
    temporary_directory: Path,
    selected_frames: tuple[CapturedFrame, ...],
    review_frames: tuple[CapturedFrame, ...] = (),
    review_reason: str | None = None,
) -> tuple[Machine, Database, BeltSession, list, list]:
    """建立可直接结算的机器、数据库和周期。

    Args:
        temporary_directory: 测试数据库和图片的根目录。
        selected_frames: 正常结算时保存的证据帧。
        review_frames: 待复核时保存的原始帧。
        review_reason: 待复核原因。

    Returns:
        返回示例：
            (
                Machine(...),  # 测试机器
                Database(...),  # 测试数据库
                BeltSession(...),  # 当前测量周期
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
        return camera_frame.data

    # 组装机器依赖并记录进度通知。
    camera = SimpleNamespace(
        sdk_camera=SimpleNamespace(encode_image=encode_image),
        delivery_task=None,
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

    machine = Machine(
        machine_config=MachineConfig("1", "camera-1", "meter-1"),
        config=config,
        camera=camera,
        frequency_adapter=SimpleNamespace(active_session_id=None),
        text_recognizer=SimpleNamespace(),
        database=database,
        publish_event=publish_event,
        notify_measurement_progress=record_progress,
        on_fatal_error=Mock(),
        state_changed=asyncio.Event(),
    )

    # 建立已关闭且 OCR 完成的周期。
    session = BeltSession(
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
            ordered_lines=() if review_reason else ("AB123456",),
            normalized_lines=() if review_reason else ("AB123456",),
            selected_frames=selected_frames,
            line_frame_ids=() if review_reason else ((selected_frames[0].frame_id,),),
            review_frames=review_frames,
            review_reason=review_reason,
        ),
    )
    machine.current_session = session
    return machine, database, session, progress_updates, encoding_threads


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


@pytest.mark.asyncio
async def test_finalize_saves_images_before_record(tmp_path: Path) -> None:
    """确认正常证据先落盘，随后直接写入测量记录。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 图片和数据库记录均已核对
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, encoding_threads = create_machine(
        tmp_path, (frame,)
    )

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

    # 核对保存状态、后台线程和数据库内容。
    assert session.state == SessionState.COMMITTED
    assert machine.current_session is None
    assert len(encoding_threads) == 1
    assert encoding_threads[0] != event_loop_thread
    assert progress_updates[-1][2:] == (
        ProgressStage.EVIDENCE_STORAGE,
        ProgressStatus.SUCCESS,
    )
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT ordered_lines, final_frequency_hz, "
            "evidence_directory, needs_review "
            "FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert json.loads(record[0]) == ["AB123456"]
    assert record[1] == 50.0
    expected_directory = tmp_path / "evidence/20240922/1/session-1"
    assert record[2] == str(expected_directory)
    assert record[3] == 0


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
    await machine.try_finalize(session)

    # 核对两张图片和合并后的复核原因。
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert (expected_directory / "frame-1.jpg").read_bytes() == b"image-one"
    assert (expected_directory / "frame-2.jpg").read_bytes() == b"image-two"
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT ordered_lines, final_frequency_hz, evidence_directory, "
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
    session.ocr_result = OCRResult(
        ordered_lines=("123",),
        normalized_lines=("123",),
        selected_frames=(evidence_frame,),
        line_frame_ids=((evidence_frame.frame_id,),),
        review_frames=(evidence_frame,),
        review_reason="没有可靠的 20 位文字",
    )

    # 结算后读取测量记录。
    await machine.try_finalize(session)
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT ordered_lines, needs_review, review_reason "
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
    delivery_task = asyncio.create_task(asyncio.sleep(0))
    machine.camera.delivery_task = delivery_task
    machine.camera.stop = AsyncMock()
    await delivery_task

    # 保存测量记录并确认周期仍由机器持有。
    await machine.try_finalize(session)
    assert session.state == SessionState.COMMITTED
    assert machine.current_session is session

    # 执行退出清理并核对成功状态和异常事件。
    await machine.release_resources("程序退出超时")
    assert session.state == SessionState.COMMITTED
    assert session.errors == []
    assert machine.current_session is None
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
    assert machine.current_session is None
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

    def encode_image(camera_frame: object) -> bytes:
        """首帧正常编码，第二帧返回 MVS 编码错误。

        Args:
            camera_frame: 当前相机帧。

        Returns:
            返回示例：
                b"image-one"  # 首帧编码结果
        """
        if camera_frame.data == b"image-two":
            raise MvsError("SaveImageEx3 编码失败")
        return camera_frame.data

    # 使用真实相机可用状态检查本轮失败后的机器状态。
    sdk_camera = SimpleNamespace(
        encode_image=encode_image,
        closed=False,
        faulted=False,
        capture_lock=threading.Lock(),
    )
    camera = Camera("1", 1000, 50, publish_event, machine.on_fatal_error)
    camera.sdk_camera = sdk_camera
    machine.camera = camera
    machine.initialized = True

    # 执行本轮保存并核对失败审计和证据清理。
    await machine.try_finalize(session)
    assert session.state == SessionState.FAILED
    assert session.errors == ["SaveImageEx3 编码失败", "证据图片编码失败"]
    assert machine.current_session is None
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
    assert machine.acceptance_state == "READY"
    machine.on_fatal_error.assert_not_called()
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

    def encode_first_image(camera_frame: object) -> bytes:
        """编码首帧并让第二帧失败。

        Args:
            camera_frame: 当前相机帧。

        Returns:
            返回示例：
                b"image-one"  # 首帧的图片字节
        """
        if camera_frame.data == b"image-two":
            raise RuntimeError("编码失败")
        return camera_frame.data

    machine.camera.sdk_camera.encode_image = encode_first_image
    with pytest.raises(ImageEncodingError):
        await machine.try_finalize(session)

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
    write_error = OSError("图片写入失败")
    monkeypatch.setattr(
        "machine.save_evidence_image",
        Mock(side_effect=write_error),
    )
    await machine.try_finalize(session)

    # 核对失败状态和数据库内容。
    assert session.state == SessionState.FAILED
    assert "证据图片保存失败" in session.errors
    assert "测量结果入库失败" not in session.errors
    assert "测量记录提交冲突" not in session.errors
    assert read_abnormal_events(database)[0][2] == "证据图片保存失败"
    machine.on_fatal_error.assert_called_once()
    fatal_error = machine.on_fatal_error.call_args.args[0]
    assert isinstance(fatal_error, EvidenceWriteError)
    assert fatal_error.__cause__ is write_error
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
    unknown_error = error_type("未知图片保存错误")
    monkeypatch.setattr(
        "machine.save_evidence_image",
        Mock(side_effect=unknown_error),
    )

    # 执行图片保存并核对未知异常保持原样上抛。
    with pytest.raises(error_type, match="未知图片保存错误") as captured_error:
        await machine.try_finalize(session)
    assert captured_error.value is unknown_error
    assert "测量结果入库失败" not in session.errors
    assert "证据图片保存失败" not in session.errors
    assert "测量记录提交冲突" not in session.errors
    assert read_abnormal_events(database) == []
    machine.on_fatal_error.assert_not_called()


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
    write_error = sqlite3.OperationalError("database is locked")
    database.write_measurement_record = Mock(side_effect=write_error)
    await machine.try_finalize(session)

    # 核对本轮失败状态与已保存的图片。
    assert session.state == SessionState.FAILED
    assert "测量结果入库失败" in session.errors
    assert "证据图片保存失败" not in session.errors
    assert read_abnormal_events(database)[0][2] == "测量结果入库失败"
    machine.on_fatal_error.assert_called_once_with(write_error)
    assert machine.current_session is None
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
    existing_record = MeasurementRecord(
        machine_id=session.machine_id,
        session_id=session.session_id,
        start_time=session.start_time,
        finish_time="2026-09-23T00:00:01+00:00",
        ordered_lines=("EXISTING",),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=tmp_path / "existing",
        needs_review=False,
        review_reason=None,
    )
    database.write_measurement_record(existing_record)

    # 执行数据库提交并核对冲突分类。
    await machine.try_finalize(session)
    assert session.state == SessionState.FAILED
    assert "测量记录提交冲突" in session.errors
    assert "测量结果入库失败" not in session.errors
    assert read_abnormal_events(database)[0][2] == "测量记录提交冲突"
    machine.on_fatal_error.assert_called_once()
    conflict_error = machine.on_fatal_error.call_args.args[0]
    assert isinstance(conflict_error, CommitIntegrityConflictError)

    # 核对数据库仍保留首次提交的内容。
    with sqlite3.connect(database.config.database_path) as connection:
        saved_record = connection.execute(
            "SELECT ordered_lines, evidence_directory "
            "FROM measurement_records WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert saved_record == (
        json.dumps(existing_record.ordered_lines),
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
    unknown_error = ValueError("未知数据库错误")
    database.write_measurement_record = Mock(side_effect=unknown_error)

    # 执行数据库提交并核对未知异常保持原样上抛。
    with pytest.raises(ValueError, match="未知数据库错误") as captured_error:
        await machine.try_finalize(session)
    assert captured_error.value is unknown_error
    assert "测量记录提交冲突" not in session.errors
    assert "DATABASE_WRITE_FAILED" not in session.errors
    assert read_abnormal_events(database) == []
    machine.on_fatal_error.assert_not_called()


def test_database_compares_evidence_directory(tmp_path: Path) -> None:
    """确认同一周期重复写入只接受相同证据目录。

    Args:
        tmp_path: pytest 提供的临时目录。

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
        ordered_lines=("AB123456",),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1",
        needs_review=False,
        review_reason=None,
    )

    # 重复保存相同记录。
    database.write_measurement_record(record)
    database.write_measurement_record(record)

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
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None

    # 交付空采集结果并尝试启动下一轮。
    capture_result = CaptureResult(frames=(), statistics={"received_frame_count": 0})
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_COMPLETED, "1", session.session_id, capture_result
    ))
    await machine.handle_machine_start()

    # 核对失败状态、异常记录和当前周期身份。
    assert session.state == SessionState.FAILED
    assert machine.current_session is session
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
    assert machine.current_session is None


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
    machine.current_session = None
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
    machine.current_session = None
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
    camera = Camera("1", 1000, 50, deliver_capture_event, machine.on_fatal_error)
    camera.sdk_camera = sdk_camera
    machine.camera = camera

    # 启动采集并处理相机交付的故障事件。
    await machine.handle_machine_start()
    session = machine.current_session
    assert session is not None
    delivery_task = camera.delivery_task
    await delivery_task
    await asyncio.sleep(0)
    capture_event = await machine.queue.get()
    await machine.handle_event(capture_event)
    machine.queue.task_done()

    # 核对本轮失败记录及相机可用状态。
    assert session.state == SessionState.FAILED
    assert session.errors == ["GetImageBuffer 失败", "相机采集失败"]
    assert machine.current_session is session
    assert not camera.available
    assert not camera.is_capturing
    machine.on_fatal_error.assert_not_called()
    camera_state_notification.assert_called_once_with("1", "相机故障", "GetImageBuffer 失败")
    assert progress_updates[-1][2:] == (
        ProgressStage.IMAGE_CAPTURE,
        ProgressStatus.FAILED,
    )
    events = read_abnormal_events(database)
    assert events[0][2] == "相机采集失败"
    assert json.loads(events[0][3])["session_errors"] == session.errors

    # 现场关闭后尝试重新启动本机周期。
    await machine.handle_machine_close()
    await machine.handle_machine_start()
    assert machine.current_session is None


@pytest.mark.asyncio
async def test_late_capture_failure_notifies_without_repeating_session_failure(
    tmp_path: Path,
) -> None:
    """确认迟到的采集故障仍通知相机状态，不重复结算已失败周期。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 相机状态已通知，失败周期和异常记录未重复处理
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, _ = create_machine(tmp_path, (evidence_frame,))
    camera_state_notification = Mock()
    machine.notify_camera_state = camera_state_notification
    session.state = SessionState.FAILED
    session.errors.append("OCR 识别超时")

    # 将真实采集故障交给已经失败的周期。
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_FAILED, "1", session.session_id, "StopGrabbing 失败"
    ))

    # 核对机器级通知和周期记录均只执行一次。
    camera_state_notification.assert_called_once_with("1", "相机故障", "StopGrabbing 失败")
    assert session.errors == ["OCR 识别超时"]
    assert session.state == SessionState.FAILED
    assert machine.current_session is session
    assert read_abnormal_events(database) == []
    assert progress_updates == []


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
    machine.current_session = None
    capture_lock = threading.Lock()
    capture_lock.acquire()
    sdk_camera = SimpleNamespace(closed=False, faulted=False, capture_lock=capture_lock)
    camera = Camera("1", 1000, 50, AsyncMock(), Mock())
    camera.sdk_camera = sdk_camera
    machine.camera = camera

    # 采集锁占用时尝试启动新周期。
    try:
        await machine.handle_machine_start()
        assert machine.current_session is None
        assert machine.waiting_cycle_reset
        assert not sdk_camera.faulted
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
    assert session.state == SessionState.FAILED
    assert machine.current_session is None
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
    session.ocr_state = OCRState.RUNNING
    session.ocr_result = None
    machine.text_recognizer = SimpleNamespace(
        processing_lock=asyncio.Lock(),
        process_session_frames=Mock(side_effect=OCRProcessingError("模型执行失败")),
    )
    machine.publish_event = machine.handle_event

    # 执行 OCR 并让失败事件进入机器处理流程。
    await machine.recognize_session(session, (evidence_frame.camera_frame,))

    # 核对执行异常、失败状态和异常事件。
    assert session.state == SessionState.FAILED
    assert machine.current_session is session
    assert "模型执行失败" in session.errors
    assert "OCR 识别执行失败" in session.errors
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "OCR 识别执行失败"
    assert json.loads(events[0][3])["session_errors"] == session.errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_event_type",
    (EventType.OCR_TIMEOUT, EventType.OCR_FAILED),
)
async def test_ocr_failure_close_releases_session_while_old_task_finishes(
    tmp_path: Path,
    failure_event_type: EventType,
) -> None:
    """确认旧 OCR 收尾时释放失败周期并保留新周期的任务引用。

    Args:
        tmp_path: pytest 提供的临时目录。
        failure_event_type: 本次触发的 OCR 失败事件类型。

    Returns:
        返回示例：
            None  # 失败周期已释放，新周期与任务引用保持有效
    """
    # 建立等待 OCR 的旧周期和两轮可控的阻塞识别。
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, old_session, _, _ = create_machine(tmp_path, (evidence_frame,))
    old_session.capture_stop_time = None
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
        capture_id: str,
        camera_serial: str,
        frames: tuple[object, ...],
    ) -> object:
        """按周期等待测试放行后返回对应 OCR 结果。

        Args:
            session_id: 本次识别所属的周期编号。
            capture_id: 本次识别的采集编号。
            camera_serial: 本次识别的相机序列号。
            frames: 本次识别的原始帧。

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

    machine.text_recognizer = SimpleNamespace(
        processing_lock=asyncio.Lock(),
        process_session_frames=Mock(side_effect=process_frames),
    )
    machine.publish_event = AsyncMock()
    machine.camera.available = True
    machine.camera.is_capturing = False
    machine.camera.start_capture = Mock()
    machine.camera.stop = AsyncMock()
    capture_result = CaptureResult(frames=(evidence_frame.camera_frame,), statistics={})

    try:
        # 启动旧 OCR 并在底层识别仍运行时触发超时。
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", old_session.session_id, capture_result
        ))
        old_task = machine.recognition_task
        assert old_task is not None
        assert await asyncio.to_thread(old_started.wait, 5)
        await machine.handle_event(RuntimeEvent(
            failure_event_type, "1", old_session.session_id, "模型执行失败"
        ))
        assert old_session.state == SessionState.FAILED
        assert machine.current_session is old_session
        assert machine.recognition_task is None
        assert old_task in machine.recognition_tasks
        assert not old_task.done()

        # 真实 CLOSE 释放旧周期，下一次 START 建立新周期。
        await machine.handle_machine_close()
        assert machine.current_session is None
        machine.camera.delivery_task = asyncio.get_running_loop().create_future()
        await machine.handle_machine_start()
        new_session = machine.current_session
        assert new_session is not None
        assert new_session is not old_session
        await machine.handle_event(RuntimeEvent(
            EventType.CAPTURE_COMPLETED, "1", new_session.session_id, capture_result
        ))
        new_task = machine.recognition_task
        assert new_task is not None
        assert new_task in machine.recognition_tasks
        assert not new_started.is_set()

        # 放行旧 OCR 并核对旧回调未清理新周期的任务。
        old_release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        assert await asyncio.to_thread(new_started.wait, 5)
        await asyncio.sleep(0)
        assert old_task not in machine.recognition_tasks
        assert machine.current_session is new_session
        assert machine.recognition_task is new_task
        assert new_task in machine.recognition_tasks
        machine.publish_event.assert_not_awaited()

        # 旧周期的迟到事件不进入新周期。
        await machine.handle_event(RuntimeEvent(
            EventType.OCR_COMPLETED, "1", old_session.session_id, old_result
        ))
        assert machine.current_session is new_session
        assert machine.recognition_task is new_task
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
async def test_old_ocr_unknown_error_remains_fatal_without_clearing_new_task(
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
    new_session.state = SessionState.FAILED
    new_task = asyncio.create_task(asyncio.Event().wait())
    machine.recognition_task = new_task
    machine.recognition_tasks.add(new_task)
    new_task.add_done_callback(machine.handle_recognition_task_finished)
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
        machine.recognition_tasks.add(old_task)
        old_task.add_done_callback(machine.handle_recognition_task_finished)
        await asyncio.gather(old_task, return_exceptions=True)
        await asyncio.sleep(0)

        # 核对未知异常与新周期隔离。
        machine.on_fatal_error.assert_called_once_with(unknown_error)
        assert old_task not in machine.recognition_tasks
        assert new_task in machine.recognition_tasks
        assert machine.recognition_task is new_task
        assert machine.current_session is new_session
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
    machine.current_session = None
    machine.camera.stop = AsyncMock()
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

    # 启动已解绑的旧任务并发起退出。
    old_task = asyncio.create_task(run_blocking_operation(finish_old_ocr))
    machine.recognition_tasks.add(old_task)
    old_task.add_done_callback(machine.handle_recognition_task_finished)
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
        assert not machine.recognition_tasks
    finally:
        # 放行可能尚未结束的线程并关闭测试库。
        operation_finished.set()
        if shutdown_task is not None:
            await asyncio.gather(shutdown_task, return_exceptions=True)
        await asyncio.gather(old_task, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
async def test_unknown_ocr_error_reaches_fatal_callback(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    """确认未知识别异常通过任务完成回调交给全局故障入口。

    Args:
        tmp_path: pytest 提供的临时目录。
        error_type: 注入识别任务的未知异常类型。

    Returns:
        返回示例：
            None  # 未知异常已交给致命故障回调，未生成 OCR 失败事件
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None
    error = error_type("未知识别错误")
    machine.text_recognizer = SimpleNamespace(
        processing_lock=asyncio.Lock(),
        process_session_frames=Mock(side_effect=error),
    )
    machine.publish_event = AsyncMock()

    # 交付采集结果并由机器启动带完成回调的识别任务。
    capture_result = CaptureResult(frames=(evidence_frame.camera_frame,), statistics={})
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_COMPLETED, "1", session.session_id, capture_result
    ))
    recognition_task = machine.recognition_task
    assert recognition_task is not None

    # 等待原始异常离开任务并核对完成回调的交付结果。
    with pytest.raises(error_type) as captured_error:
        await recognition_task
    assert captured_error.value is error
    machine.on_fatal_error.assert_called_once_with(error)
    machine.publish_event.assert_not_awaited()
    assert machine.recognition_task is None
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
    session.ocr_state = OCRState.WAITING
    session.ocr_result = None

    # 交付周期超时事件。
    await machine.handle_event(RuntimeEvent(
        EventType.CYCLE_TIMEOUT, "1", session.session_id
    ))

    # 核对周期失败、等待现场复位和超时审计。
    assert session.state == SessionState.FAILED
    assert machine.waiting_cycle_reset
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "测量周期超时"
    assert json.loads(events[0][3])["session_errors"] == session.errors


@pytest.mark.asyncio
async def test_audit_failure_does_not_block_session_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """确认异常事件写入失败时仍完成 Session 失败收尾。

    Args:
        tmp_path: pytest 提供的临时目录。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 审计失败已记录日志且现场关闭后释放周期
    """
    evidence_frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (evidence_frame,))
    session.capture_stop_time = None
    database.save_abnormal_event = Mock(
        side_effect=sqlite3.OperationalError("运行库写入失败")
    )

    # 执行失败收尾并等待现场关闭。
    await machine.handle_measurement_failure(session, "此次相机没有采集到任何帧")
    assert session.state == SessionState.FAILED
    assert machine.current_session is session
    await machine.handle_machine_close()

    # 核对审计写入只尝试一次且周期已释放。
    database.save_abnormal_event.assert_called_once()
    assert "记录测量失败事件失败" in caplog.text
    assert machine.current_session is None


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
    session.ocr_state = OCRState.WAITING

    # 交付 IO 中断事件并检查本轮失败收尾。
    await machine.handle_event(RuntimeEvent(EventType.IO_INTERRUPTED, "1"))
    assert session.state == SessionState.FAILED
    assert "IO 通信中断" in session.errors
    assert session.capture_stop_time is not None
    assert machine.current_session is None
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
    assert machine.current_session is session
    assert machine.waiting_cycle_reset
    assert read_abnormal_events(database) == []


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
    result = session.ocr_result
    session.ocr_result = None
    session.ocr_state = OCRState.RUNNING
    session.capture_stop_time = None
    notification = Mock()
    machine.notify_ocr_result = notification

    # 旧周期和未知周期不发送文字通知。
    await machine.handle_event(RuntimeEvent(EventType.OCR_COMPLETED, "1", "old", result))
    notification.assert_not_called()

    # 当前结果在现场关闭前立即发送，参数仅包含身份和文字。
    await machine.handle_event(RuntimeEvent(EventType.OCR_COMPLETED, "1", session.session_id, result))
    notification.assert_called_once_with("1", session.session_id, result.ordered_lines, result.normalized_lines)
    assert session.capture_stop_time is None
    assert session.ocr_state == OCRState.COMPLETED

    # 重复结果被忽略，CLOSE 后正常提交并释放周期。
    await machine.handle_event(RuntimeEvent(EventType.OCR_COMPLETED, "1", session.session_id, result))
    await machine.handle_machine_close()
    assert session.state == SessionState.COMMITTED
    assert session.ocr_result is None
    assert machine.current_session is None
    assert notification.call_count == 1
    database.close()
