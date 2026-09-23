"""验证机器结算时直接保存证据图片和测量记录。"""

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config_util import AppConfig, MachineConfig
from database import Database, MeasurementRecord
from enums import OCRState, ProgressStage, ProgressStatus, SessionState
from machine import ImageEncodingError, Machine
from models import BeltSession, CapturedFrame, FrequencyMeasurement, OCRResult


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
        start_time="2026-09-23T00:00:00+00:00",
        capture_start_time=0.0,
        capture_stop_time=1.0,
        ocr_state=OCRState.SUCCESS,
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
    assert record[2] == str(tmp_path / "evidence/1/session-1")
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
    assert (tmp_path / "evidence/1/session-1/frame-1.jpg").read_bytes() == b"image-one"
    assert (tmp_path / "evidence/1/session-1/frame-2.jpg").read_bytes() == b"image-two"
    with sqlite3.connect(database.config.database_path) as connection:
        record = connection.execute(
            "SELECT ordered_lines, final_frequency_hz, evidence_directory, "
            "needs_review, review_reason "
            "FROM measurements WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert json.loads(record[0]) == []
    assert record[1] is None
    assert record[2] == str(tmp_path / "evidence/1/session-1")
    assert record[3] == 1
    assert record[4] == "没有最终文字；没有找到最终频率，请人工复核。"


@pytest.mark.asyncio
async def test_encoding_failure_removes_new_images(tmp_path: Path) -> None:
    """确认第二张图片编码失败时清理第一张且不写数据库。

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
    assert not (tmp_path / "evidence/1/session-1/frame-1.jpg").exists()
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurements"
        ).fetchone()[0]
    assert record_count == 0


@pytest.mark.asyncio
async def test_image_write_failure_skips_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认图片写入失败时不写数据库并结束本轮。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 本轮失败且数据库没有测量记录
    """
    frame = create_frame("session-1", "frame-1", b"image-one")
    machine, database, session, _, _ = create_machine(tmp_path, (frame,))
    monkeypatch.setattr(
        "machine.save_evidence_image",
        Mock(side_effect=OSError("图片写入失败")),
    )
    await machine.try_finalize(session)

    # 核对失败状态和数据库内容。
    assert session.state == SessionState.FAILED
    assert "DATABASE_WRITE_FAILED" in session.errors
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurements"
        ).fetchone()[0]
    assert record_count == 0


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
    assert machine.current_session is None
    assert (tmp_path / "evidence/1/session-1/frame-1.jpg").read_bytes() == b"image-one"
    assert progress_updates[-1][2:] == (
        ProgressStage.EVIDENCE_STORAGE,
        ProgressStatus.FAILED,
    )


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
        evidence_directory=tmp_path / "evidence/1/session-1",
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
