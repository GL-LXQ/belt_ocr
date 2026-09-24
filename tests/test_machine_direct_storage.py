"""验证机器结算时直接保存证据图片和测量记录。"""

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

from camera.camera import Camera
from config_util import AppConfig, MachineConfig
from camera.hikrobot_sdk import MvsError
from database import Database, MeasurementRecord
from enums import EventType, OCRState, ProgressStage, ProgressStatus, SessionState
from machine import ImageEncodingError, Machine
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
            "FROM measurements WHERE session_id = ?",
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
            "FROM measurements WHERE session_id = ?",
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
            "FROM measurements WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()

    # 核对可靠文字和待复核原因。
    assert json.loads(record[0]) == ["123"]
    assert record[1] == 1
    assert "没有可靠的 20 位文字" in record[2]
    assert session.state == SessionState.COMMITTED
    assert read_abnormal_events(database) == []


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
    assert session.errors == ["SaveImageEx3 编码失败", "EVIDENCE_ENCODING_FAILED"]
    assert machine.current_session is None
    abnormal_events = read_abnormal_events(database)
    assert len(abnormal_events) == 1
    assert abnormal_events[0][2] == "EVIDENCE_ENCODING_FAILED"
    assert json.loads(abnormal_events[0][3])["session_errors"] == session.errors
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert not (expected_directory / "frame-1.jpg").exists()
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
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
            "SELECT COUNT(*) FROM measurements"
        ).fetchone()[0]
    assert record_count == 0
    assert session.state != SessionState.FAILED
    assert read_abnormal_events(database) == []
    assert "DATABASE_WRITE_FAILED" not in session.errors


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, ValueError])
async def test_image_write_failure_skips_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """确认图片写入失败时不写数据库并结束本轮。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        error_type: 图片文件操作抛出的异常类型。

    Returns:
        返回示例：
            None  # 本轮失败且数据库没有测量记录
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    monkeypatch.setattr(
        "machine.save_evidence_image",
        Mock(side_effect=error_type("图片写入失败")),
    )
    await machine.try_finalize(session)

    # 核对失败状态和数据库内容。
    assert session.state == SessionState.FAILED
    assert "EVIDENCE_WRITE_FAILED" in session.errors
    assert "DATABASE_WRITE_FAILED" not in session.errors
    assert "COMMIT_INTEGRITY_CONFLICT" not in session.errors
    assert read_abnormal_events(database)[0][2] == "EVIDENCE_WRITE_FAILED"
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurements"
        ).fetchone()[0]
    assert record_count == 0


@pytest.mark.asyncio
async def test_unknown_image_write_error_is_not_database_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认未知图片保存异常继续上抛，不记为数据库写入失败。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 未知异常已上抛，未生成数据库失败记录
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    monkeypatch.setattr(
        "machine.save_evidence_image",
        Mock(side_effect=RuntimeError("未知图片保存错误")),
    )

    # 执行图片保存并核对未知异常保持原样上抛。
    with pytest.raises(RuntimeError, match="未知图片保存错误"):
        await machine.try_finalize(session)
    assert "DATABASE_WRITE_FAILED" not in session.errors
    assert read_abnormal_events(database) == []


@pytest.mark.asyncio
async def test_database_failure_keeps_saved_images(tmp_path: Path) -> None:
    """确认数据库写入失败时保留图片并结束本轮。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 周期失败且已保存的图片保留
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, progress_updates, _ = create_machine(tmp_path, (frame,))
    database.write_measurement_record = Mock(
        side_effect=sqlite3.OperationalError("数据库写入失败")
    )
    await machine.try_finalize(session)

    # 核对本轮失败状态与已保存的图片。
    assert session.state == SessionState.FAILED
    assert "DATABASE_WRITE_FAILED" in session.errors
    assert "EVIDENCE_WRITE_FAILED" not in session.errors
    assert read_abnormal_events(database)[0][2] == "DATABASE_WRITE_FAILED"
    assert machine.current_session is None
    expected_directory = tmp_path / f"evidence/{TEST_LOCAL_START_DATE}/1/session-1"
    assert (expected_directory / "frame-1.jpg").read_bytes() == b"image-one"
    assert progress_updates[-1][2:] == (
        ProgressStage.EVIDENCE_STORAGE,
        ProgressStatus.FAILED,
    )


@pytest.mark.asyncio
async def test_database_conflict_keeps_conflict_reason(tmp_path: Path) -> None:
    """确认数据库提交内容冲突仍使用原有错误代码。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 提交冲突已按原有代码记录
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    database.write_measurement_record = Mock(
        side_effect=ValueError("同一 Session 的提交内容不一致。")
    )

    # 执行数据库提交并核对原有冲突分类。
    await machine.try_finalize(session)
    assert session.state == SessionState.FAILED
    assert "COMMIT_INTEGRITY_CONFLICT" in session.errors
    assert "DATABASE_WRITE_FAILED" not in session.errors
    assert read_abnormal_events(database)[0][2] == "COMMIT_INTEGRITY_CONFLICT"


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
    with pytest.raises(ValueError):
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
    assert events[0][2] == "CAPTURE_EMPTY"
    assert json.loads(events[0][3])["session_errors"] == session.errors
    camera_state_notification.assert_not_called()

    # 收到真实关闭后释放失败周期。
    await machine.handle_machine_close()
    assert machine.current_session is None


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
    assert session.errors == ["GetImageBuffer 失败", "CAPTURE_FAILED"]
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
    assert events[0][2] == "CAPTURE_FAILED"
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
    session.errors.append("OCR_TIMEOUT")

    # 将真实采集故障交给已经失败的周期。
    await machine.handle_event(RuntimeEvent(
        EventType.CAPTURE_FAILED, "1", session.session_id, "StopGrabbing 失败"
    ))

    # 核对机器级通知和周期记录均只执行一次。
    camera_state_notification.assert_called_once_with("1", "相机故障", "StopGrabbing 失败")
    assert session.errors == ["OCR_TIMEOUT"]
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
    assert read_abnormal_events(database)[0][2] == "CAPTURE_FAILED"


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
    assert "此次文字识别执行失败" in session.errors
    events = read_abnormal_events(database)
    assert len(events) == 1
    assert events[0][2] == "此次文字识别执行失败"
    assert json.loads(events[0][3])["session_errors"] == session.errors


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
    assert events[0][2] == "此次测量周期超时"
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
    assert "IO_INTERRUPTED" in session.errors
    assert session.capture_stop_time is not None
    assert machine.current_session is None
    assert machine.waiting_cycle_reset

    # 核对异常事件中的周期身份和失败原因。
    events = read_abnormal_events(database)
    assert [(row[0], row[1], row[2]) for row in events] == [
        ("1", "session-1", "IO_INTERRUPTED"),
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
