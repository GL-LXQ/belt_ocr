"""验证测量记录写入时的 SQLite 写锁重试边界。"""

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

import database as database_module
from config_util import AppConfig
from database import Database, MeasurementRecord


def create_database_and_record(
    temporary_directory: Path,
) -> tuple[Database, MeasurementRecord]:
    """创建已初始化的结果库和一条测量记录。

    Args:
        temporary_directory: 测试数据库所在目录。

    Returns:
        返回示例：
            (
                Database(...),  # 已初始化的结果库
                MeasurementRecord(
                    machine_id="1",  # 机器编号
                    session_id="session-1",  # 周期编号
                    start_time="2026-09-24T00:00:00+00:00",  # 开始时间
                    finish_time="2026-09-24T00:00:01+00:00",  # 结束时间
                    ordered_lines=("ABC",),  # 最终文字
                    final_frequency_hz=50.0,  # 最终频率
                    measurement_frequencies=(),  # 频率明细
                    evidence_directory=Path("evidence"),  # 证据目录
                    needs_review=False,  # 是否需要复核
                    review_reason=None,  # 复核原因
                ),
            )
    """
    # 初始化测试结果库。
    config = AppConfig(
        database_path=temporary_directory / "measurements.sqlite3",
        evidence_directory=temporary_directory / "evidence",
        mvs_development_directory=temporary_directory,
    )
    database = Database(config)
    database.initialize_result_database()

    # 建立用于测试写入的测量记录。
    record = MeasurementRecord(
        machine_id="1",
        session_id="session-1",
        start_time="2026-09-24T00:00:00+00:00",
        finish_time="2026-09-24T00:00:01+00:00",
        ordered_lines=("ABC",),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=config.evidence_directory,
        needs_review=False,
        review_reason=None,
    )
    return database, record


def test_busy_write_succeeds_on_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认首次写锁竞争后重新连接并成功写入一次。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 第二次写入成功且只保存一条记录
    """
    database, record = create_database_and_record(tmp_path)
    successful_connection = sqlite3.connect(database.config.database_path)
    busy_error = sqlite3.OperationalError("database is locked")
    busy_error.sqlite_errorname = "SQLITE_BUSY"
    connect_mock = Mock(side_effect=[
        busy_error,
        successful_connection,
    ])
    sleep_mock = Mock()

    # 首次连接报告写锁竞争，第二次使用真实连接完成事务。
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database_module.sqlite3, "connect", connect_mock)
            patch.setattr(database_module.time, "sleep", sleep_mock)
            database.write_measurement_record(record)
    finally:
        successful_connection.close()

    # 核对重试次数和最终写入内容。
    assert connect_mock.call_count == 2
    sleep_mock.assert_called_once_with(0.1)
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurements WHERE session_id = ?",
            (record.session_id,),
        ).fetchone()[0]
    assert record_count == 1


def test_persistent_busy_stops_after_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认持续写锁竞争达到重试上限后原样抛出。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 两次写入尝试和最终异常已核对
    """
    database, record = create_database_and_record(tmp_path)
    connect_mock = Mock(side_effect=sqlite3.OperationalError("database is locked"))
    sleep_mock = Mock()

    # 连续两次报告写锁竞争。
    with monkeypatch.context() as patch:
        patch.setattr(database_module.sqlite3, "connect", connect_mock)
        patch.setattr(database_module.time, "sleep", sleep_mock)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            database.write_measurement_record(record)

    # 核对达到上限后没有继续等待或写入。
    assert connect_mock.call_count == 2
    sleep_mock.assert_called_once_with(0.1)


def test_other_operational_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认非写锁错误不进入重试。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 非写锁错误立即上抛
    """
    database, record = create_database_and_record(tmp_path)
    operational_error = sqlite3.OperationalError("unable to open database file")
    connect_mock = Mock(side_effect=operational_error)
    sleep_mock = Mock()

    # 抛出与写锁竞争无关的 SQLite 操作错误。
    with monkeypatch.context() as patch:
        patch.setattr(database_module.sqlite3, "connect", connect_mock)
        patch.setattr(database_module.time, "sleep", sleep_mock)
        with pytest.raises(
            sqlite3.OperationalError, match="unable to open database file"
        ):
            database.write_measurement_record(record)

    # 核对没有进行第二次尝试。
    assert connect_mock.call_count == 1
    sleep_mock.assert_not_called()
