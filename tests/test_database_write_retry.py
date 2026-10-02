"""验证测量记录写入时的 SQLite 写锁重试边界。"""

import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

import database as database_module
from config_util import AppConfig
from database import CommitIntegrityConflictError, Database, MeasurementRecord


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
                    recognized_lines=("2926215C", "003"),  # 正式识别文字
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
        recognized_lines=("2926215C", "003"),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=config.evidence_directory,
        needs_review=False,
        review_reason=None,
    )
    return database, record


def test_busy_write_succeeds_on_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """确认首次写锁竞争后重新连接并成功写入一次。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        caplog: pytest 捕获的日志。

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

    # 核对重试次数。
    assert connect_mock.call_count == 2
    sleep_mock.assert_called_once_with(0.1)

    # 核对重试前只记录一条写锁竞争告警。
    retry_warnings = [
        log_record
        for log_record in caplog.records
        if "测量记录写锁竞争" in log_record.getMessage()
    ]
    assert len(retry_warnings) == 1
    assert retry_warnings[0].levelname == "WARNING"
    assert retry_warnings[0].getMessage() == "测量记录写锁竞争，准备重试 session_id=session-1"

    # 核对最终写入内容。
    with sqlite3.connect(database.config.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM measurement_records WHERE session_id = ?",
            (record.session_id,),
        ).fetchone()[0]
        recognized_lines = connection.execute(
            "SELECT recognized_lines FROM measurement_records WHERE session_id = ?",
            (record.session_id,),
        ).fetchone()[0]
    assert record_count == 1
    assert recognized_lines == '["2926215C", "003"]'


def test_recognized_lines_participate_in_idempotent_write(tmp_path: Path) -> None:
    """验证新库正式文字字段参与重复写入和内容冲突比较。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 正式文字已保存，相同内容只写一次，不同文字产生冲突
    """
    # 重复写入同一周期的相同正式文字。
    database, record = create_database_and_record(tmp_path)
    database.write_measurement_record(record)
    database.write_measurement_record(record)

    # 读取新库的字段。
    with sqlite3.connect(database.config.database_path) as connection:
        columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(measurement_records)")
        }

        # 读取已保存的正式文字 JSON。
        saved_records = connection.execute(
            "SELECT recognized_lines FROM measurement_records"
        ).fetchall()
    # 核对新库的完整字段。
    assert columns == {
        "session_id",
        "machine_id",
        "start_time",
        "finish_time",
        "recognized_lines",
        "final_frequency_hz",
        "evidence_directory",
        "measurement_frequencies",
        "needs_review",
        "review_reason",
        "reviewed_at",
        "reviewed_lines",
    }
    # 核对同一周期只保留一条标准化文字。
    assert saved_records == [('["2926215C", "003"]',)]

    # 同一周期改用不同正式文字时报告提交冲突。
    conflicting_record = replace(record, recognized_lines=("2926215D", "003"))
    with pytest.raises(CommitIntegrityConflictError, match="提交内容不一致"):
        database.write_measurement_record(conflicting_record)


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """确认非写锁错误不进入重试。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        caplog: pytest 捕获的日志。

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

    # 核对非写锁错误没有记录重试告警。
    assert "测量记录写锁竞争" not in caplog.text
