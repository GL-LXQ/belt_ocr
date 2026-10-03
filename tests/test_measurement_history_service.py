"""保留原界面的纯业务回归，不加载历史 Qt 控件。"""

import json

import os

import sqlite3

from datetime import date, datetime, time, timedelta, timezone

from pathlib import Path

from unittest.mock import Mock

import pytest

from config_util import AppConfig

from database import Database, MeasurementRecord

from repo.machine_repo import MachineRepo

from repo.abnormal_event_repo import AbnormalEventRepo

from repo.measurement_record_repo import MeasurementRecordRepo

from src.service.abnormal_event_service import AbnormalEventService

from src.service.measurement_record_service import (
    MeasurementRecordService,
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)

from src.service.machine_service import MachineService

@pytest.fixture
def measurement_record_service(tmp_path: Path) -> MeasurementRecordService:
    """建立含正常、待复核和历史机器状态的业务库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            MeasurementRecordService(...)  # 已保存四条测量记录的读取服务
    """
    # 使用现有数据库入口创建业务表和三台机器。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    database = Database(config)
    database.initialize_result_database()
    machine_repo = MachineRepo(config.database_path)
    first_machine_id = machine_repo.insert("一号皮带", "camera-1", "meter-1")
    second_machine_id = machine_repo.insert("二号皮带", "camera-2", "meter-2")
    third_machine_id = machine_repo.insert(
        "三号皮带", "camera-3", "meter-3", enabled=False
    )
    machine_repo.soft_delete(second_machine_id)

    # 写入正常、待复核、停用机器和缺少机器信息的历史记录。
    records = (
        MeasurementRecord(
            machine_id=str(first_machine_id),
            session_id="normal-session",
            start_time="2026-09-27T08:00:00+00:00",
            finish_time="2026-09-27T08:01:00+00:00",
            recognized_lines=("12345678", "003"),
            final_frequency_hz=50.0,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "normal-evidence",
            needs_review=False,
            review_reason=None,
        ),
        MeasurementRecord(
            machine_id=str(second_machine_id),
            session_id="review-session",
            start_time="2026-09-27T09:00:00+00:00",
            finish_time="2026-09-27T09:01:00+00:00",
            recognized_lines=("待确认文字",),
            final_frequency_hz=None,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "review-evidence",
            needs_review=True,
            review_reason="没有可靠的 20 位文字",
        ),
        MeasurementRecord(
            machine_id=str(third_machine_id),
            session_id="disabled-machine-session",
            start_time="2026-09-27T09:30:00+00:00",
            finish_time="2026-09-27T09:31:00+00:00",
            recognized_lines=("停用机器记录",),
            final_frequency_hz=25.0,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "disabled-machine-evidence",
            needs_review=False,
            review_reason=None,
        ),
        MeasurementRecord(
            machine_id="99",
            session_id="missing-machine-session",
            start_time="2026-09-27T10:00:00+00:00",
            finish_time="2026-09-27T10:01:00+00:00",
            recognized_lines=(),
            final_frequency_hz=0.0,
            measurement_frequencies=(),
            evidence_directory=tmp_path / "missing-machine-evidence",
            needs_review=False,
            review_reason=None,
        ),
    )
    for record in records:
        database.write_measurement_record(record)
    return MeasurementRecordService(MeasurementRecordRepo(config.database_path))


@pytest.fixture
def paged_measurement_record_service(
    measurement_record_service: MeasurementRecordService,
) -> MeasurementRecordService:
    """在已有业务库中准备二十五条可分页记录。

    Args:
        measurement_record_service: 已建立业务库和机器信息的测量记录服务。

    Returns:
        返回示例：
            MeasurementRecordService(...)  # 已保存二十五条分页测试记录的服务
    """
    # 准备跨页的正常及待复核记录。
    database_path = measurement_record_service.measurement_record_repo.database_path
    records_to_insert = []
    for record_number in range(25):
        finish_second = 5 if record_number == 4 else record_number
        records_to_insert.append((
            f"page-{record_number:02}",
            "1" if record_number < 21 else "2",
            "2026-09-27T08:00:00+00:00",
            f"2026-09-27T08:00:{finish_second:02}+00:00",
            json.dumps([f"文字{record_number:02}"], ensure_ascii=False),
            str(database_path.parent / f"page-{record_number:02}"),
            int(record_number < 21),
        ))

    # 替换原有测量记录。
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM measurement_records")
        connection.executemany(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, "
            "recognized_lines, evidence_directory, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            records_to_insert,
        )
    return measurement_record_service


@pytest.fixture
def text_search_record_service(
    measurement_record_service: MeasurementRecordService,
) -> MeasurementRecordService:
    """准备包含多种长度、重复文字和空人工结果的临时测量记录。

    Args:
        measurement_record_service: 已初始化临时业务库的服务。

    Returns:
        返回示例：
            MeasurementRecordService(...)  # 含三条文字查询样本的服务
    """
    # 准备不同记录共享文字及同一记录中的多行文字。
    record_lines = (
        (
            "text-primary",
            "1",
            (
                "ABCD1234567890123456", "2926215C", "2926217C", "003", "12",
                "6215", "A%B", "A_B", "LEFT", "RIGHT",
            ),
            1,
            None,
            None,
        ),
        ("text-copy", "2", ("2926215C", "AAB"), 0, None, None),
        (
            "text-empty-review", "1", ("HIDDEN",), 1,
            "2026-09-27T12:00:00+00:00", "[]",
        ),
    )

    # 将样本写入现有临时库，并为每条记录设置独立证据目录。
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, recognized_lines, "
            "evidence_directory, needs_review, reviewed_at, reviewed_lines) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    machine_id,
                    "2026-09-27T11:00:00+00:00",
                    "2026-09-27T11:01:00+00:00",
                    json.dumps(recognized_lines),
                    str(database_path.parent / session_id),
                    needs_review,
                    reviewed_at,
                    reviewed_lines,
                )
                for (
                    session_id, machine_id, recognized_lines, needs_review,
                    reviewed_at, reviewed_lines,
                ) in record_lines
            ],
        )
    return measurement_record_service


@pytest.mark.parametrize(
    "text_query,text_match_mode,text_length,expected_sessions",
    [
        (" abcd 1234567890123456 ", "exact", 20, ("text-primary",)),
        ("29\t26\u3000215c", "exact", 8, ("text-primary", "text-copy")),
        ("0\t03", "exact", 3, ("text-primary", "normal-session")),
        ("1\u30002", "exact", 2, ("text-primary",)),
        ("6215", "contains", 8, ("text-primary", "text-copy")),
        ("6215", "exact", 8, ()),
        ("6215", "exact", None, ("text-primary",)),
        ("6215", "contains", 3, ()),
        ("2926", "contains", 8, ("text-primary", "text-copy")),
        ("2926216C", "exact", 8, ()),
        ("%", "contains", None, ("text-primary",)),
        ("_", "contains", None, ("text-primary",)),
        ("LEFT RIGHT", "contains", None, ()),
        ('["', "contains", None, ()),
        ('","', "contains", None, ()),
        ("HIDDEN", "exact", None, ()),
    ],
)
def test_text_search_matches_effective_json_lines(
    text_search_record_service: MeasurementRecordService,
    text_query: str,
    text_match_mode: str,
    text_length: int | None,
    expected_sessions: tuple[str, ...],
) -> None:
    """验证标准化、逐行字面匹配、完整行长度和记录去重。

    Args:
        text_search_record_service: 含文字查询样本的临时服务。
        text_query: 用户输入的完整文字或片段。
        text_match_mode: 包含或精确匹配方式。
        text_length: 被查询行的完整长度。
        expected_sessions: 按结束时间和周期编号倒序排列的预期记录。

    Returns:
        返回示例：
            None  # 查询列表与总数符合逐行匹配结果
    """
    result = text_search_record_service.list_records(
        text_query=text_query,
        text_match_mode=text_match_mode,
        text_length=text_length,
    )

    # 核对记录级返回与总数，保留不同周期中的相同文字。
    assert tuple(record["session_id"] for record in result["records"]) == (
        expected_sessions
    )
    assert result["total"] == len(expected_sessions)
    assert result["total_pages"] == 1


@pytest.mark.parametrize("text_query", [None, "", " \t\u3000\n"])
def test_empty_text_search_ignores_length(
    text_search_record_service: MeasurementRecordService,
    text_query: str | None,
) -> None:
    """验证空查询词不增加文字或行长度筛选。

    Args:
        text_search_record_service: 含文字查询样本的临时服务。
        text_query: 未输入或纯空白的查询词。

    Returns:
        返回示例：
            None  # 无查询词时保留全部记录
    """
    expected = text_search_record_service.list_records()
    result = text_search_record_service.list_records(
        text_query=text_query, text_match_mode="exact", text_length=2
    )
    assert result == expected
    assert result["total"] == 7


@pytest.mark.parametrize("edited_text", [None, "new belt"])
def test_text_search_uses_reviewed_result(
    text_search_record_service: MeasurementRecordService,
    edited_text: str | None,
) -> None:
    """验证人工修改替换查询文字，直接确认仍搜索识别结果。

    Args:
        text_search_record_service: 含待复核样本的临时服务。
        edited_text: 人工修改文字或直接确认标记。

    Returns:
        返回示例：
            None  # 旧文字与新文字命中当前有效结果
    """
    text_search_record_service.complete_review("text-primary", edited_text)
    original_result = text_search_record_service.list_records(
        text_query="2926215C", text_match_mode="exact"
    )
    new_result = text_search_record_service.list_records(text_query="new belt")

    # 核对人工修改后旧文字退出查询，直接确认保留原文字。
    expected_original = ["text-copy"] if edited_text else ["text-primary", "text-copy"]
    assert [record["session_id"] for record in original_result["records"]] == (
        expected_original
    )
    assert original_result["total"] == len(expected_original)
    assert new_result["total"] == (1 if edited_text else 0)
    if edited_text:
        assert new_result["records"][0]["session_id"] == "text-primary"


def test_text_search_combines_filters_before_pagination(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证文字、机器、日期和状态共同筛选后分页并保持稳定倒序。

    Args:
        paged_measurement_record_service: 已保存二十五条记录的临时服务。

    Returns:
        返回示例：
            None  # 四页记录、总数和组合条件保持一致
    """
    selected_date = datetime.fromisoformat(
        "2026-09-27T08:00:00+00:00"
    ).astimezone().date()

    # 查询十条文字命中的待复核记录，每页三条。
    records = []
    for page_number in range(1, 5):
        result = paged_measurement_record_service.list_records(
            "pending",
            "1",
            page_number,
            3,
            selected_date,
            selected_date,
            text_query="文字0",
        )
        assert result["total"] == 10
        assert result["total_pages"] == 4
        assert len(result["records"]) == (1 if page_number == 4 else 3)
        records.extend(result["records"])

    # 核对跨页排序与同一结束时间的周期编号倒序。
    assert [record["session_id"] for record in records] == [
        f"page-{record_number:02}" for record_number in reversed(range(10))
    ]

    # 任一其他条件不符时返回空列表和一致的总数。
    previous_date = selected_date - timedelta(days=1)
    for review_status, machine_id, query_date in (
        ("normal", "1", selected_date),
        ("pending", "2", selected_date),
        ("pending", "1", previous_date),
    ):
        result = paged_measurement_record_service.list_records(
            review_status,
            machine_id,
            start_date=query_date,
            end_date=query_date,
            text_query="文字0",
        )
        assert result["records"] == []
        assert result["total"] == 0
        assert result["total_pages"] == 1


def test_measurement_record_service_filters_and_reads_details(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证历史筛选、软删除机器和只读详情字段。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 筛选结果和详情字段均来自 measurement_records
    """
    # 查询全部、正常、待复核和指定机器的记录。
    records = measurement_record_service.list_records()["records"]
    assert [record["session_id"] for record in records] == [
        "missing-machine-session", "disabled-machine-session",
        "review-session", "normal-session"
    ]
    normal_records = measurement_record_service.list_records("normal")["records"]
    assert [record["session_id"] for record in normal_records] == [
        "missing-machine-session", "disabled-machine-session", "normal-session"
    ]
    review_records = measurement_record_service.list_records("pending")["records"]
    assert [record["session_id"] for record in review_records] == [
        "review-session"
    ]
    assert measurement_record_service.list_records("reviewed")["records"] == []
    assert measurement_record_service.list_records("pending", "1")["records"] == []
    machine_records = measurement_record_service.list_records("pending", "2")["records"]
    assert machine_records[0]["session_id"] == "review-session"
    disabled_records = measurement_record_service.list_records("normal", "3")["records"]
    assert disabled_records[0]["session_id"] == "disabled-machine-session"

    # 核对软删除机器和缺少机器信息时的展示名称。
    machines = measurement_record_service.list_record_machines()["machines"]
    machine_names = {
        machine["machine_id"]: machine["machine_name"] for machine in machines
    }
    assert machine_names == {
        "1": "一号皮带",
        "2": "二号皮带",
        "3": "三号皮带",
        "99": "99",
    }
    assert records[0]["machine_name"] == "99"

    # 读取详情并确认文字、空频率和复核原因。
    review_record = measurement_record_service.get_record("review-session")["record"]
    normal_record = measurement_record_service.get_record("normal-session")["record"]
    assert review_record["recognized_lines"] == ("待确认文字",)
    assert review_record["final_frequency_hz"] is None
    assert review_record["review_reason"] == "没有可靠的 20 位文字"
    assert normal_record["recognized_lines"] == ("12345678", "003")
    assert normal_record["review_reason"] is None
    assert measurement_record_service.get_record("unknown-session")["record"] is None


def test_measurement_records_use_database_pages_and_stable_time_order(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证数据库分页保留结束时间及周期编号的倒序。

    Args:
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 两页记录连续且分页总数准确
    """
    # 读取两页并核对记录数量与分页业务字段。
    first_page = paged_measurement_record_service.list_records(page=1)
    second_page = paged_measurement_record_service.list_records(page=2)
    assert len(first_page["records"]) == 20
    assert len(second_page["records"]) == 5
    pagination = {
        key: first_page[key]
        for key in ("page", "page_size", "total", "total_pages")
    }
    assert pagination == {
        "page": 1,
        "page_size": 20,
        "total": 25,
        "total_pages": 2,
    }
    assert second_page["page"] == 2

    # 核对页间顺序和结束时间相同的记录顺序。
    session_ids = [record["session_id"] for record in first_page["records"]]
    session_ids += [record["session_id"] for record in second_page["records"]]
    assert session_ids == [
        f"page-{record_number:02}" for record_number in reversed(range(25))
    ]
    assert first_page["records"][-1]["session_id"] == "page-05"
    assert second_page["records"][0]["session_id"] == "page-04"


def test_measurement_record_pages_count_only_filtered_records(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证状态和机器筛选的总数仅统计匹配记录。

    Args:
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 筛选后的页数和记录均按对应条件计算
    """
    # 核对跨页的待复核记录和指定机器的正常记录。
    pending_page = paged_measurement_record_service.list_records("pending", page=2)
    machine_page = paged_measurement_record_service.list_records("normal", "2")
    assert pending_page["total"] == 21
    assert pending_page["total_pages"] == 2
    assert [record["session_id"] for record in pending_page["records"]] == ["page-00"]
    assert machine_page["total"] == 4
    assert machine_page["total_pages"] == 1
    assert len(machine_page["records"]) == 4

    # 无匹配记录时保留第一页的显示页数。
    empty_page = paged_measurement_record_service.list_records("reviewed")
    assert empty_page["records"] == []
    assert empty_page["total"] == 0
    assert empty_page["total_pages"] == 1


def test_measurement_records_filter_by_local_day_boundaries(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证本地日期开始边界包含且次日开始边界排除。

    Args:
        measurement_record_service: 已建立业务库的测量记录服务。

    Returns:
        返回示例：
            None  # 查询仅返回所选本地日期内的记录
    """
    # 按当前系统本地时区计算所选日期的 UTC 边界。
    selected_date = date(2026, 9, 28)
    local_start = datetime.combine(selected_date, time.min).astimezone(timezone.utc)
    local_end = datetime.combine(
        selected_date + timedelta(days=1), time.min
    ).astimezone(timezone.utc)
    local_midday = datetime.combine(
        selected_date, time(12, 0)
    ).astimezone(timezone.utc)
    finish_times = {
        "before": local_start - timedelta(seconds=1),
        "start": local_start,
        "inside": local_midday,
        "after": local_end,
    }

    # 替换原有记录并写入边界两侧的 UTC 时间。
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM measurement_records")
        connection.executemany(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, "
            "recognized_lines, evidence_directory) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    "1",
                    finish_time.isoformat(),
                    finish_time.isoformat(),
                    '["文字"]',
                    str(database_path.parent / session_id),
                )
                for session_id, finish_time in finish_times.items()
            ],
        )

    # 查询单日本地日期并核对排他结束边界。
    page = measurement_record_service.list_records(
        start_date=selected_date, end_date=selected_date
    )
    assert page["total"] == 2
    assert page["total_pages"] == 1
    assert [record["session_id"] for record in page["records"]] == [
        "inside", "start"
    ]


def test_daily_summary_counts_local_day_records_and_pending_reviews(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """确认今日统计包含当天正式记录，并只统计尚未复核的记录。

    Args:
        measurement_record_service: 已建立业务库的测量记录服务。

    Returns:
        返回示例：
            None  # 当天记录和复核状态已统计，日期边界与历史查询一致
    """
    # 按本地日期计算当天的 UTC 边界。
    target_date = date(2026, 9, 30)
    start_finish_time = datetime.combine(
        target_date,
        time.min,
    ).astimezone(timezone.utc)
    end_finish_time = datetime.combine(
        target_date + timedelta(days=1),
        time.min,
    ).astimezone(timezone.utc)

    # 准备当天三类记录和日期边界两侧的记录。
    records = [
        ("normal-session", start_finish_time, 0, None),
        ("pending-session", start_finish_time + timedelta(hours=12), 1, None),
        (
            "reviewed-session",
            end_finish_time - timedelta(microseconds=1),
            1,
            end_finish_time.isoformat(),
        ),
        ("yesterday-session", start_finish_time - timedelta(microseconds=1), 1, None),
        ("tomorrow-session", end_finish_time, 1, None),
    ]

    # 将测试记录写入正式测量结果表。
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM measurement_records")
        connection.executemany(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, recognized_lines, "
            "evidence_directory, needs_review, reviewed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    "1",
                    finish_time.isoformat(),
                    finish_time.isoformat(),
                    '["文字"]',
                    str(database_path.parent / session_id),
                    needs_review,
                    reviewed_at,
                )
                for session_id, finish_time, needs_review, reviewed_at in records
            ],
        )

    # 核对当天统计与历史页面的日期范围一致。
    summary = measurement_record_service.get_daily_summary(target_date)
    assert summary == {
        "recognition_count": 3,
        "pending_review_count": 1,
    }
    history = measurement_record_service.list_records(
        start_date=target_date,
        end_date=target_date,
    )
    assert history["total"] == summary["recognition_count"]

    # 完成复核后重新查询，识别总数保持不变。
    measurement_record_service.complete_review("pending-session", None)
    assert measurement_record_service.get_daily_summary(target_date) == {
        "recognition_count": 3,
        "pending_review_count": 0,
    }


@pytest.mark.parametrize("empty_records", [False, True])
def test_daily_summary_repo_uses_one_connection_and_query(
    measurement_record_service: MeasurementRecordService,
    monkeypatch: pytest.MonkeyPatch,
    empty_records: bool,
) -> None:
    """确认单次查询同时统计总数和待复核数，无记录时两项均为零。

    Args:
        measurement_record_service: 已建立业务库的测量记录服务。
        monkeypatch: pytest 提供的属性替换工具。
        empty_records: 是否清空全部测量记录。

    Returns:
        返回示例：
            None  # 单连接和单查询已核对，非空统计为 3 / 1，空库统计为 0 / 0
    """
    # 准备包含三条记录或没有记录的正式测量表。
    measurement_record_repo = measurement_record_service.measurement_record_repo
    database_path = measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        if empty_records:
            connection.execute("DELETE FROM measurement_records")
        else:
            connection.execute(
                "DELETE FROM measurement_records WHERE session_id = ?",
                ("missing-machine-session",),
            )

    # 记录本次统计使用的连接和实际执行的 SQL。
    connection = sqlite3.connect(database_path, timeout=1)
    queried_statements = []
    connection.set_trace_callback(queried_statements.append)
    connection_factory = Mock(return_value=connection)
    monkeypatch.setattr(sqlite3, "connect", connection_factory)

    # 同时核对两项统计值和单次数据库读取。
    summary = measurement_record_repo.count_daily_summary(
        "2026-09-27T00:00:00+00:00",
        "2026-09-28T00:00:00+00:00",
    )
    assert summary == {
        "recognition_count": 0 if empty_records else 3,
        "pending_review_count": 0 if empty_records else 1,
    }
    connection_factory.assert_called_once_with(database_path, timeout=1)
    assert len(queried_statements) == 1


def test_daily_summary_service_returns_one_repo_query_result() -> None:
    """确认服务只调用一次总览统计方法，并原样返回结果。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 两项统计直接来自同次 Repo 查询，旧的 count_records 未被调用
    """
    # 准备 Repo 一次返回的总览统计。
    measurement_record_repo = Mock(spec=MeasurementRecordRepo)
    expected_summary = {
        "recognition_count": 3,
        "pending_review_count": 1,
    }
    measurement_record_repo.count_daily_summary.return_value = expected_summary
    service = MeasurementRecordService(measurement_record_repo)

    # 计算当前本地日期对应的 UTC 查询边界。
    target_date = date(2026, 9, 30)
    start_finish_time = datetime.combine(target_date, time.min)
    end_finish_time = datetime.combine(target_date + timedelta(days=1), time.min)

    # 核对服务仅进行一次统计调用并保留原返回对象。
    assert service.get_daily_summary(target_date) is expected_summary
    measurement_record_repo.count_daily_summary.assert_called_once_with(
        start_finish_time.astimezone(timezone.utc).isoformat(),
        end_finish_time.astimezone(timezone.utc).isoformat(),
    )
    measurement_record_repo.count_records.assert_not_called()


@pytest.mark.parametrize(
    "database_error",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.DatabaseError("database read failed"),
    ],
)
def test_daily_summary_converts_database_failure(database_error: sqlite3.Error) -> None:
    """确认总览统计查询失败时转换为现有服务错误。

    Args:
        database_error: 单次总览统计查询抛出的数据库错误。

    Returns:
        返回示例：
            None  # 数据库原始错误已转换为今日统计读取提示
    """
    # 模拟单次总览统计发生数据库错误。
    measurement_record_repo = Mock(spec=MeasurementRecordRepo)
    measurement_record_repo.count_daily_summary.side_effect = database_error
    service = MeasurementRecordService(measurement_record_repo)

    # 核对统计错误使用既有服务异常类型。
    with pytest.raises(
        MeasurementRecordServiceError,
        match="今日检测统计读取失败。",
    ) as error_info:
        service.get_daily_summary(date(2026, 9, 30))
    assert error_info.value.__cause__ is database_error


def test_measurement_records_combine_date_status_machine_and_pagination(
    paged_measurement_record_service: MeasurementRecordService,
) -> None:
    """验证日期、状态、机器和分页使用相同筛选条件。

    Args:
        paged_measurement_record_service: 已保存二十五条分页记录的服务。

    Returns:
        返回示例：
            None  # 总数和当前页仅包含同时匹配全部筛选条件的记录
    """
    # 在相同状态和机器下增加下一本地日期的记录。
    selected_date = datetime.fromisoformat(
        "2026-09-27T08:00:00+00:00"
    ).astimezone().date()
    next_day_start = datetime.combine(
        selected_date + timedelta(days=1), time.min
    ).astimezone(timezone.utc).isoformat()
    database_path = (
        paged_measurement_record_service.measurement_record_repo.database_path
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, "
            "recognized_lines, evidence_directory, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "outside-date",
                "1",
                next_day_start,
                next_day_start,
                '["其他日期"]',
                str(database_path.parent / "outside-date"),
                1,
            ),
        )

    # 同时应用全部筛选条件并核对两页结果与稳定排序。
    second_page = paged_measurement_record_service.list_records(
        "pending", "1", 2, 5, selected_date, selected_date
    )
    fourth_page = paged_measurement_record_service.list_records(
        "pending", "1", 4, 5, selected_date, selected_date
    )
    assert second_page["total"] == 21
    assert second_page["total_pages"] == 5
    assert second_page["page_size"] == 5
    assert [record["session_id"] for record in second_page["records"]] == [
        "page-15", "page-14", "page-13", "page-12", "page-11"
    ]
    assert [record["session_id"] for record in fourth_page["records"][:2]] == [
        "page-05", "page-04"
    ]


def test_existing_measurement_table_adds_review_columns_without_losing_records(
    tmp_path: Path,
) -> None:
    """验证已有测量表补齐复核字段后保留测量记录。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            None  # 已有记录仍在且两个复核字段可重复初始化
    """
    database_path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE measurement_records (session_id TEXT PRIMARY KEY, "
            "recognized_lines TEXT NOT NULL, needs_review INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO measurement_records VALUES (?, ?, ?)",
            ("existing-session", '["原始文字"]', 1),
        )

        # 重复初始化时仅补齐缺失字段。
        MeasurementRecordRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)
        table_columns = connection.execute("PRAGMA table_info(measurement_records)")
        columns = {column[1] for column in table_columns}
        saved_record = connection.execute(
            "SELECT session_id, recognized_lines, needs_review, "
            "reviewed_at, reviewed_lines "
            "FROM measurement_records"
        ).fetchone()

    assert {"reviewed_at", "reviewed_lines"} <= columns
    assert saved_record == ("existing-session", '["原始文字"]', 1, None, None)


def test_confirm_original_ocr_preserves_original_record_and_prevents_repeat(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证确认原文字只写复核时间且同一记录不能再复核。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 原文字和复核原因保留，重复复核已拒绝
    """
    measurement_record_service.complete_review("review-session")
    record = measurement_record_service.get_record("review-session")["record"]
    reviewed_time = datetime.fromisoformat(record["reviewed_at"])
    assert reviewed_time.tzinfo == timezone.utc
    assert record["recognized_lines"] == ("待确认文字",)
    assert record["reviewed_lines"] is None
    assert record["needs_review"] is True
    assert record["review_reason"] == "没有可靠的 20 位文字"
    assert measurement_record_service.list_records("pending")["records"] == []
    reviewed_records = measurement_record_service.list_records(
        "reviewed", "2"
    )["records"]
    assert reviewed_records[0]["session_id"] == "review-session"
    assert measurement_record_service.list_records("normal", "2")["records"] == []

    # 从数据库再次确认原始字段没有被复核写入覆盖。
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        saved_record = connection.execute(
            "SELECT recognized_lines, needs_review, review_reason, reviewed_lines "
            "FROM measurement_records WHERE session_id = ?",
            ("review-session",),
        ).fetchone()
    assert saved_record == ('["待确认文字"]', 1, "没有可靠的 20 位文字", None)
    with pytest.raises(MeasurementReviewAlreadyCompletedError, match="该记录已完成复核"):
        measurement_record_service.complete_review("review-session", "再次修改")
    with pytest.raises(MeasurementReviewAlreadyCompletedError):
        measurement_record_service.complete_review("normal-session")
    normal_record = measurement_record_service.get_record("normal-session")["record"]
    assert normal_record["reviewed_at"] is None


def test_edited_review_saves_lines_and_rejects_empty_input(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证人工文字先分行再标准化且空输入不完成复核。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 有效文字保存为 JSON，原始 OCR 保留
    """
    # 拒绝没有有效文字的空输入和各种空白字符。
    for edited_text in ("", " \n\t \u3000", "\u2002\u00a0\n\u3000"):
        with pytest.raises(MeasurementRecordServiceError, match="至少需要一条有效文字"):
            measurement_record_service.complete_review("review-session", edited_text)
    pending_record = measurement_record_service.get_record("review-session")["record"]
    assert pending_record["reviewed_at"] is None

    # 分行保存大小写、制表符和全角空格混合的人工文字。
    measurement_record_service.complete_review(
        "review-session",
        " 2926\t215c \n\t\u3000\n ab\u3000 c \n 修 正二  ",
    )
    record = measurement_record_service.get_record("review-session")["record"]
    assert record["recognized_lines"] == ("待确认文字",)
    assert record["reviewed_lines"] == ("2926215C", "ABC", "修正二")
    assert record["needs_review"] is True
    assert record["review_reason"] == "没有可靠的 20 位文字"
    assert datetime.fromisoformat(record["reviewed_at"]).tzinfo == timezone.utc
    reviewed_records = measurement_record_service.list_records("reviewed")["records"]
    assert reviewed_records[0]["reviewed_lines"] == (
        "2926215C", "ABC", "修正二"
    )

    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        saved_record = connection.execute(
            "SELECT recognized_lines, reviewed_lines FROM measurement_records "
            "WHERE session_id = ?",
            ("review-session",),
        ).fetchone()
    assert saved_record[0] == '["待确认文字"]'
    assert json.loads(saved_record[1]) == ["2926215C", "ABC", "修正二"]


def test_invalid_ocr_json_is_a_history_read_error(
    measurement_record_service: MeasurementRecordService,
) -> None:
    """验证损坏的历史 OCR JSON 按普通读取错误报告。

    Args:
        measurement_record_service: 已保存测试记录的测量记录服务。

    Returns:
        返回示例：
            None  # 损坏的文字 JSON 产生历史读取错误
    """
    database_path = measurement_record_service.measurement_record_repo.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE measurement_records SET recognized_lines = ? WHERE session_id = ?",
            ("{broken", "normal-session"),
        )

    # 核对列表读取的对外提示。
    with pytest.raises(MeasurementRecordServiceError) as list_error:
        measurement_record_service.list_records()
    assert str(list_error.value) == "历史记录读取失败。"

    # 核对详情读取的对外提示。
    with pytest.raises(MeasurementRecordServiceError) as detail_error:
        measurement_record_service.get_record("normal-session")
    assert str(detail_error.value) == "历史详情读取失败。"
