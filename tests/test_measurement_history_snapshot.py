"""验证历史分页与并发入库、人工复核使用一致的数据快照。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest

from config_util import AppConfig
from database import Database, MeasurementRecord
from repo.measurement_record_repo import MeasurementRecordRepo
from src.service.measurement_record_service import (
    MeasurementRecordService,
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)


@pytest.fixture
def history_database(tmp_path: Path) -> tuple[Database, MeasurementRecord]:
    """通过正式写入入口建立含二十一条待复核记录的临时 WAL 业务库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            (
                Database(...),  # 已写入二十一条测量记录的临时数据库
                MeasurementRecord(...),  # 保留原始文字和证据目录的首条记录
            )
    """
    # 初始化临时业务表并开启并发读写。
    config = AppConfig(
        database_path=tmp_path / "history.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    database = Database(config)
    database.initialize_result_database()
    with closing(sqlite3.connect(config.database_path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")

    # 为每条测量记录保留相同识别文字和独立周期编号。
    sample_record = MeasurementRecord(
        machine_id="1",
        session_id="session-00",
        start_time="2026-10-02T08:00:00+00:00",
        finish_time="2026-10-02T08:01:00+00:00",
        recognized_lines=("ABCD1234567890123456", "2926215C", "003", "12"),
        final_frequency_hz=50.0,
        measurement_frequencies=(),
        evidence_directory=config.evidence_directory / "session-00",
        needs_review=True,
        review_reason="需要人工复核",
    )
    for record_number in range(21):
        session_id = f"session-{record_number:02}"
        database.write_measurement_record(replace(
            sample_record,
            session_id=session_id,
            evidence_directory=config.evidence_directory / session_id,
        ))
    return (
        database,
        sample_record,
    )


@pytest.mark.parametrize("write_action", ["insert", "review", "review_into_empty_result"])
def test_history_page_keeps_count_and_rows_in_one_snapshot(
    history_database: tuple[Database, MeasurementRecord],
    monkeypatch: pytest.MonkeyPatch,
    write_action: str,
) -> None:
    """验证统计后发生的正式入库或复核不会混入当前页的数据快照。

    Args:
        history_database: 含二十一条待复核记录的临时数据库及首条记录。
        monkeypatch: pytest 提供的对象替换工具。
        write_action: 在统计与读取记录之间执行的正式写入操作。

    Returns:
        返回示例：
            None  # 当前页保持同一快照，下一次查询读取最新写入结果
    """
    # 读取并发写入前的完整分页结果。
    database, sample_record = history_database
    service = MeasurementRecordService(MeasurementRecordRepo(database.config.database_path))
    empty_result = write_action == "review_into_empty_result"
    query = {
        "review_status": "reviewed" if empty_result else "pending",
        "machine_id": "1",
        "page": 1 if empty_result else 2,
        "page_size": 20,
        "text_query": "2926217C" if empty_result else "2926215C",
        "text_match_mode": "exact",
        "text_length": 8,
    }
    original_page = service.list_records(**query)
    original_connect = sqlite3.connect
    write_completed = False

    class ConcurrentWriteConnection(sqlite3.Connection):
        """在分页记录查询前提交另一个连接的测量或复核。"""

        def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
            """在列表查询开始前完成一次并发写入。

            Args:
                sql: 当前连接要执行的 SQL。
                parameters: SQL 对应的绑定参数。

            Returns:
                返回示例：
                    sqlite3.Cursor(...)  # 当前语句的执行结果
            """
            nonlocal write_completed

            # 在统计结束后、读取本页记录前触发一次正式写入。
            if sql.startswith("SELECT record.session_id") and not write_completed:
                write_completed = True
                if write_action == "insert":
                    database.write_measurement_record(replace(
                        sample_record,
                        session_id="session-new",
                        finish_time="2026-10-02T08:02:00+00:00",
                        evidence_directory=database.config.evidence_directory / "session-new",
                    ))
                else:
                    service.complete_review("session-00", "2926217C")

            # 继续执行当前连接的原始 SQL。
            return super().execute(sql, parameters)

    # 仅替换连接类型，查询和写入仍由真实 SQLite 执行。
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: original_connect(*args, **kwargs, factory=ConcurrentWriteConnection),
    )
    concurrent_page = service.list_records(**query)
    assert write_completed
    assert concurrent_page == original_page

    # 下一次查询读取已提交写入，并保留空页和末页回退所需的总页数。
    latest_page = service.list_records(**query)
    if write_action == "insert":
        assert latest_page["total"] == 22
        assert latest_page["total_pages"] == 2
        assert [record["session_id"] for record in latest_page["records"]] == ["session-01", "session-00"]
    elif empty_result:
        assert latest_page["total"] == 1
        assert latest_page["total_pages"] == 1
        assert latest_page["records"][0]["session_id"] == "session-00"
        assert latest_page["records"][0]["reviewed_lines"] == ("2926217C",)
    else:
        assert latest_page["total"] == 20
        assert latest_page["total_pages"] == 1
        assert latest_page["records"] == []
        query["page"] = latest_page["total_pages"]
        assert len(service.list_records(**query)["records"]) == 20

    # 原始文字、时间和证据目录不被分页或复核修改。
    detail = service.get_record("session-00")["record"]
    assert detail["recognized_lines"] == sample_record.recognized_lines
    assert detail["finish_time"] == sample_record.finish_time
    assert detail["evidence_directory"] == str(sample_record.evidence_directory)


def test_history_empty_and_out_of_range_pages_preserve_pagination(
    history_database: tuple[Database, MeasurementRecord],
) -> None:
    """验证无记录和超过末页的请求保留现有分页返回约定。

    Args:
        history_database: 含二十一条待复核记录的临时数据库及首条记录。

    Returns:
        返回示例：
            None  # 空结果至少一页，超出末页不在 Service 内修改请求页码
    """
    # 无匹配文字时返回空列表和一页。
    database, _ = history_database
    service = MeasurementRecordService(MeasurementRecordRepo(database.config.database_path))
    empty_page = service.list_records(text_query="2926216C", text_match_mode="exact", text_length=8)
    assert empty_page["records"] == []
    assert empty_page["total"] == 0
    assert empty_page["page"] == 1
    assert empty_page["total_pages"] == 1

    # 超出末页时继续返回实际总数，交给页面现有流程回退。
    missing_page = service.list_records(page=3)
    assert missing_page["records"] == []
    assert missing_page["total"] == 21
    assert missing_page["page"] == 3
    assert missing_page["total_pages"] == 2


@pytest.mark.parametrize("read_failure", [False, True])
def test_history_page_releases_its_only_connection(
    history_database: tuple[Database, MeasurementRecord],
    monkeypatch: pytest.MonkeyPatch,
    read_failure: bool,
) -> None:
    """验证分页完成或读取失败后都关闭唯一的快照连接。

    Args:
        history_database: 含二十一条待复核记录的临时数据库及首条记录。
        monkeypatch: pytest 提供的对象替换工具。
        read_failure: 是否在统计完成后触发本页读取故障。

    Returns:
        返回示例：
            None  # 分页只打开一个连接，成功和失败后均已关闭
    """
    # 保存本次分页创建的真实连接。
    database, _ = history_database
    repo = MeasurementRecordRepo(database.config.database_path)
    service = MeasurementRecordService(repo)
    original_connect = sqlite3.connect
    opened_connections = []

    def open_connection(*args, **kwargs) -> sqlite3.Connection:
        """记录本次打开的 SQLite 连接。

        Args:
            args: SQLite 连接的位置参数。
            kwargs: SQLite 连接的关键字参数。

        Returns:
            返回示例：
                sqlite3.Connection(...)  # 新建的真实数据库连接
        """
        connection = original_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    # 在正常查询或读取异常中检查服务结果。
    monkeypatch.setattr(sqlite3, "connect", open_connection)
    if read_failure:
        monkeypatch.setattr(repo, "_read_record_rows", Mock(side_effect=sqlite3.OperationalError("read failed")))
        with pytest.raises(MeasurementRecordServiceError, match="历史记录读取失败。"):
            service.list_records()
    else:
        assert service.list_records()["total"] == 21

    # 事务结束后不保留任何可用的读取连接。
    assert len(opened_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened_connections[0].execute("SELECT 1")


def test_simultaneous_reviews_preserve_first_committed_result(
    history_database: tuple[Database, MeasurementRecord],
) -> None:
    """验证两个连接同时复核时只有一个结果能成功保存。

    Args:
        history_database: 含二十一条待复核记录的临时数据库及首条记录。

    Returns:
        返回示例：
            None  # 一次成功、一次拒绝，原始文字和首次人工结果保留
    """
    database, sample_record = history_database
    start_review = Barrier(2)

    def submit_review(edited_text: str) -> str | None:
        """同步启动一次复核并返回成功保存的文字。

        Args:
            edited_text: 本次人工复核提交的文字。

        Returns:
            返回示例：
                "2926217C"  # 本次成功保存的人工文字
                None  # 其他连接已先完成复核
        """
        # 为本次复核建立独立服务并等待另一提交者。
        service = MeasurementRecordService(MeasurementRecordRepo(database.config.database_path))
        start_review.wait(timeout=5)

        # 使用正式条件更新提交结果并识别重复复核。
        try:
            service.complete_review("session-00", edited_text)
            return edited_text
        except MeasurementReviewAlreadyCompletedError:
            return None

    # 两个线程通过不同连接同时提交复核。
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(submit_review, ("2926217C", "2926219C")))
    saved_results = [result for result in results if result is not None]
    assert len(saved_results) == 1

    # 核对最终文字和原始测量内容。
    service = MeasurementRecordService(MeasurementRecordRepo(database.config.database_path))
    detail = service.get_record("session-00")["record"]
    assert detail["reviewed_lines"] == tuple(saved_results)
    assert detail["recognized_lines"] == sample_record.recognized_lines
    assert detail["evidence_directory"] == str(sample_record.evidence_directory)
