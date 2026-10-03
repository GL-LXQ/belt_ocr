"""保留原界面的纯业务回归，不加载历史 Qt 控件。"""

import json

import os

import sqlite3

import time as system_time

from contextlib import closing

from datetime import date, datetime, time, timedelta, timezone

from pathlib import Path

from unittest.mock import Mock

import pytest
from src.controller.controller import AppController

from repo.abnormal_event_repo import AbnormalEventRepo

from src.service.abnormal_event_service import AbnormalEventService, format_event_summary

SESSION_ID = "5ad75ed720044b2a9da465055e8ea424"

@pytest.fixture
def abnormal_event_service(tmp_path: Path) -> AbnormalEventService:
    """建立包含多台机器和多种异常原因的事件表。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        返回示例：
            AbnormalEventService(...)  # 已保存三条异常事件的服务
    """
    # 创建现有表结构并写入目标记录与其他筛选记录。
    database_path = tmp_path / "measurements.recovery.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        AbnormalEventRepo.create_table(connection)
    abnormal_event_repo = AbnormalEventRepo(database_path)
    abnormal_event_repo.insert(
        100.0, "1", SESSION_ID, "OCR_TIMEOUT", '{"session_errors": ["OCR_TIMEOUT"]}'
    )
    abnormal_event_repo.insert(
        150.0, "1", "session-other", "CAPTURE_FAILED", '{"message": "相机异常"}'
    )
    abnormal_event_repo.insert(
        200.0, "2", "session-two", "CUSTOM_REASON", '{"message": "未知原因"}'
    )
    return AbnormalEventService(abnormal_event_repo)


@pytest.mark.parametrize(
    "reason",
    ("测量周期超时", "OCR 识别执行失败"),
)
def test_chinese_reason_passes_through_service(
    abnormal_event_service: AbnormalEventService,
    reason: str,
) -> None:
    """确认新写入的中文原因在查询结果中保持原值。

    Args:
        abnormal_event_service: 已保存异常事件的服务。
        reason: 待核对的中文异常原因。

    Returns:
        返回示例：
            None  # 查询结果直接返回已写入的中文原因
    """
    # 写入中文原因并通过正式查询服务读取。
    session_id = f"session-{reason}"
    abnormal_event_service.abnormal_event_repo.insert(
        300.0, "1", session_id, reason, "{}"
    )
    events = abnormal_event_service.list_events(session_id=session_id)["events"]

    # 核对列表和详情均保留原始中文原因。
    assert len(events) == 1
    assert events[0]["reason"] == reason
    detail = abnormal_event_service.get_event(events[0]["abnormal_event_id"])["event"]
    assert detail["reason"] == reason


def test_service_filters_events_and_preserves_reason(
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证时间排序、精确搜索和历史英文原因原样返回。

    Args:
        abnormal_event_service: 已保存三条异常事件的服务。

    Returns:
        返回示例：
            None  # 查询结果符合筛选条件且历史原因原样保留
    """
    # 核对机器选项和默认时间倒序。
    assert abnormal_event_service.list_machine_ids()["machine_ids"] == ["1", "2"]
    events = abnormal_event_service.list_events()["events"]
    assert [event["created_at"] for event in events] == [200.0, 150.0, 100.0]
    assert events[0]["reason"] == "CUSTOM_REASON"
    assert events[1]["reason"] == "CAPTURE_FAILED"

    # 核对机器过滤与完整 Session ID 精确匹配。
    machine_events = abnormal_event_service.list_events(machine_id="1")["events"]
    assert [event["session_id"] for event in machine_events] == [
        "session-other", SESSION_ID
    ]
    target_events = abnormal_event_service.list_events(
        machine_id="1", session_id=SESSION_ID
    )["events"]
    assert len(target_events) == 1
    assert target_events[0]["reason"] == "OCR_TIMEOUT"
    assert abnormal_event_service.list_events(session_id=SESSION_ID[:8])["events"] == []

    # 按主键读取详情，确认历史英文原因和 payload 原样保留。
    target_event = abnormal_event_service.get_event(
        target_events[0]["abnormal_event_id"]
    )["event"]
    assert target_event["reason"] == "OCR_TIMEOUT"
    assert target_event["payload_json"] == '{"session_errors": ["OCR_TIMEOUT"]}'


@pytest.mark.parametrize(
    ("timezone_name", "selected_date", "utc_start", "utc_end"),
    (
        ("UTC", date(2026, 9, 27), "2026-09-27T00:00:00+00:00", "2026-09-28T00:00:00+00:00"),
        ("Asia/Shanghai", date(2026, 9, 27), "2026-09-26T16:00:00+00:00", "2026-09-27T16:00:00+00:00"),
        ("America/New_York", date(2026, 3, 8), "2026-03-08T05:00:00+00:00", "2026-03-09T04:00:00+00:00"),
        ("America/New_York", date(2026, 11, 1), "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
    ),
)
@pytest.mark.skipif(not hasattr(system_time, "tzset"), reason="当前系统不支持测试进程切换本地时区")
def test_service_combines_local_record_dates_machine_and_session(
    abnormal_event_service: AbnormalEventService,
    event_controller: AppController,
    monkeypatch: pytest.MonkeyPatch,
    timezone_name: str,
    selected_date: date,
    utc_start: str,
    utc_end: str,
) -> None:
    """验证本地日期边界、夏令时、组合筛选和同时间稳定排序。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。
        event_controller: 使用该服务的真实控制器。
        monkeypatch: pytest 提供的环境变量替换工具。
        timezone_name: 测试进程使用的本地时区。
        selected_date: 需要查询的本地自然日。
        utc_start: 该自然日起点对应的 UTC 时间。
        utc_end: 下一自然日起点对应的 UTC 时间。

    Returns:
        返回示例：
            None  # 起点包含、终点排除，日期与机器和精确周期条件同时生效
    """
    # 临时切换运行时区并按已知 UTC 边界准备记录。
    with monkeypatch.context() as timezone_patch:
        timezone_patch.setenv("TZ", timezone_name)
        system_time.tzset()
        try:
            start_timestamp = datetime.fromisoformat(utc_start).timestamp()
            end_timestamp = datetime.fromisoformat(utc_end).timestamp()
            middle_timestamp = (start_timestamp + end_timestamp) / 2
            repo = abnormal_event_service.abnormal_event_repo
            for timestamp, machine_id, session_id in (
                (start_timestamp - 0.001, "1", "before"),
                (start_timestamp, "1", "lower"),
                (middle_timestamp, "1", "middle"),
                (end_timestamp - 0.001, "1", "upper-inside"),
                (end_timestamp, "1", "after"),
                (middle_timestamp, "2", "other-machine"),
                (middle_timestamp, "1", "tie"),
            ):
                repo.insert(timestamp, machine_id, session_id, "测量周期超时", "{}")

            # 控制器保留原有空白清理，日期条件在数据库中与机器组合。
            result = event_controller.list_abnormal_events(
                " 1 ", " ", start_date=selected_date, end_date=selected_date
            )
            assert result.success
            assert [event["session_id"] for event in result.data["events"]] == [
                "upper-inside", "tie", "middle", "lower"
            ]
            all_machines = abnormal_event_service.list_events(start_date=selected_date, end_date=selected_date)
            assert len(all_machines["events"]) == 5

            # 完整周期编号继续精确匹配，SQL 参数不会被当作语句。
            matched = abnormal_event_service.list_events("1", "middle", selected_date, selected_date)
            assert [event["session_id"] for event in matched["events"]] == ["middle"]
            assert abnormal_event_service.list_events("1", "midd", selected_date, selected_date)["events"] == []
            injected_query = abnormal_event_service.list_events("1' OR 1=1 --", None, selected_date, selected_date)
            assert injected_query["events"] == []

            # 后端允许只设一侧边界，不传日期时仍返回原有全部记录。
            lower_only = abnormal_event_service.list_events("1", start_date=selected_date)["events"]
            assert all(event["created_at"] >= start_timestamp for event in lower_only)
            assert lower_only[0]["session_id"] == "after"
            upper_only = abnormal_event_service.list_events("1", end_date=selected_date)["events"]
            assert all(event["created_at"] < end_timestamp for event in upper_only)
            assert len(abnormal_event_service.list_events()["events"]) == 10
        finally:
            timezone_patch.undo()
            system_time.tzset()


def test_repo_applies_zero_timestamp_boundary(abnormal_event_service: AbnormalEventService) -> None:
    """验证零时间戳作为真实边界，不被忽略。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。

    Returns:
        返回示例：
            None  # 仅返回包含下界且小于上界的记录
    """
    repo = abnormal_event_service.abnormal_event_repo
    repo.insert(-1.0, "1", "before-epoch", "历史记录", "{}")
    repo.insert(0.0, "1", "at-epoch", "历史记录", "{}")
    events = repo.list_events("1", start_created_at=0.0, end_created_at=100.0)
    assert [event["session_id"] for event in events] == ["at-epoch"]


@pytest.mark.parametrize(
    ("payload_json", "expected_summary"),
    (
        ('{"session_errors": ["OCR_TIMEOUT", "FREQUENCY_NO_VALID_MEASUREMENT"]}', "OCR 识别超时；没有有效频率读数"),
        ('{"session_errors": ["StopGrabbing 失败", "相机采集失败"]}', "StopGrabbing 失败；相机采集失败"),
        ('{"session_errors": ["FUTURE_ERROR", null, 3, {}]}', "FUTURE_ERROR"),
        ('{"message": "相机\\n断开"}', "相机 断开"),
        ('{"event_type": "MachineClosed", "payload": null}', "事件：机器关闭"),
        ('{"event_type": "OCRFailed", "payload": "模型执行失败"}', "事件：OCR 识别执行失败；模型执行失败"),
        ('{"event_type": "FutureEvent", "payload": {"message": "已保存的消息"}}', "事件：FutureEvent；已保存的消息"),
        ('{"event_type": [], "session_errors": "错误字段格式", "message": {}}', "其他事件内容，请查看原始数据"),
        ('{"other": [1, 2, 3]}', "其他事件内容，请查看原始数据"),
        ('["unknown", "structure"]', "其他事件内容，请查看原始数据"),
        ("null", "其他事件内容，请查看原始数据"),
        ("{}", "无附加信息"),
        ('"已保存的文本"', "已保存的文本"),
        ("not JSON", "原始内容格式异常，请查看原始数据"),
        ("", "原始内容格式异常，请查看原始数据"),
    ),
)
def test_event_summary_reads_known_fields_and_handles_unknown_payload(
    payload_json: str,
    expected_summary: str,
) -> None:
    """验证可读摘要只使用已保存内容，并容忍未知和损坏内容。

    Args:
        payload_json: 需要解析的历史或运行事件内容。
        expected_summary: 预期的单行可读摘要。

    Returns:
        返回示例：
            None  # 摘要符合已保存字段，未知内容不影响列表加载
    """
    assert format_event_summary(payload_json) == expected_summary


def test_summary_supports_real_frequency_payload_and_preserves_raw_detail(
    abnormal_event_service: AbnormalEventService,
) -> None:
    """验证真实运行事件序列化后的频率摘要和未改写的原始详情。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。

    Returns:
        返回示例：
            None  # 摘要显示已保存的频率和序列号，原因和原始 JSON 完整保留
    """
    from database import serialize_value
    from enums import EventType
    from models import FrequencyMeasurement, RuntimeEvent

    # 使用运行时实际数据结构生成保存的事件 JSON。
    event = RuntimeEvent(
        EventType.FREQUENCY_MEASURED,
        "1",
        SESSION_ID,
        FrequencyMeasurement(SESSION_ID, "FM01", 0.0),
    )
    raw_json = json.dumps(serialize_value(event), ensure_ascii=False, indent=2)
    event_id = abnormal_event_service.abnormal_event_repo.insert(300.0, "1", SESSION_ID, "迟到的频率读数", raw_json)

    # 列表使用真实读数，不把零值当成缺失，也不改写原因和内容。
    events = abnormal_event_service.list_events(session_id=SESSION_ID)["events"]
    assert events[0]["payload_summary"] == "事件：收到频率读数；频率：0.0 Hz；频率仪：FM01"
    assert events[0]["reason"] == "迟到的频率读数"
    assert events[0]["payload_json"] == raw_json
    assert abnormal_event_service.get_event(event_id)["event"]["payload_json"] == raw_json


def test_summary_limits_length_without_changing_saved_payload(abnormal_event_service: AbnormalEventService) -> None:
    """验证长摘要被截断而完整原始内容仍可读取。

    Args:
        abnormal_event_service: 使用临时数据库的异常事件服务。

    Returns:
        返回示例：
            None  # 摘要最多八十字，列表和详情中的原始内容没有改动
    """
    raw_json = json.dumps({"message": "采集错误" * 100}, ensure_ascii=False, indent=2)
    event_id = abnormal_event_service.abnormal_event_repo.insert(400.0, "1", SESSION_ID, "原因保持原值", raw_json)
    event = abnormal_event_service.list_events()["events"][0]
    assert len(event["payload_summary"]) == 80
    assert event["payload_summary"].endswith("…")
    assert event["payload_json"] == raw_json
    assert abnormal_event_service.get_event(event_id)["event"]["payload_json"] == raw_json



@pytest.fixture
def event_controller(abnormal_event_service: AbnormalEventService) -> AppController:
    """建立可以读取真实异常记录和机器名称的控制器。

    Args:
        abnormal_event_service: 已保存异常事件的服务。

    Returns:
        返回示例：
            AppController(...)  # 提供机器 1 的名称，机器 2 使用编号回退
    """
    machine_service = Mock()
    machine_service.list_machines.return_value = {
        "machines": [
            {
                "id": 1,  # 机器编号
                "machine_name": "一号皮带机",  # 机器名称
            },
        ],
    }
    return AppController(machine_service, Mock(), abnormal_event_service, Path("config"))



def test_event_pages_share_filters_count_and_stable_order(abnormal_event_service: AbnormalEventService) -> None:
    """验证异常分页在筛选后计数并稳定处理同时间记录。

    Args:
        abnormal_event_service: 临时异常事件服务。

    Returns:
        None  # 两页共三条同机记录，其他机器不会混入总数
    """
    repository = abnormal_event_service.abnormal_event_repo
    repository.insert(150.0, "1", "tie-new", "测试异常", '{"message":"保留原始内容"}')
    first_page = abnormal_event_service.list_events("1", page=1, page_size=2)
    second_page = abnormal_event_service.list_events("1", page=2, page_size=2)
    assert (first_page["total"], first_page["total_pages"], first_page["page_size"]) == (3, 2, 2)
    assert [event["session_id"] for event in first_page["events"]] == ["tie-new", "session-other"]
    assert [event["session_id"] for event in second_page["events"]] == [SESSION_ID]
    assert first_page["events"][0]["payload_summary"] == "保留原始内容"
    assert first_page["events"][0]["payload_json"] == '{"message":"保留原始内容"}'


def test_event_page_filters_before_offset_and_preserves_literal_session(abnormal_event_service: AbnormalEventService):
    """验证完整周期筛选先于分页偏移且参数不被解释成 SQL。

    Args:
        abnormal_event_service: 临时异常事件服务。

    Returns:
        None  # 精确周期命中一条，超出页为空且总数保留
    """
    page = abnormal_event_service.list_events("1", SESSION_ID, page=1, page_size=1)
    assert page["total"] == 1
    assert page["events"][0]["session_id"] == SESSION_ID
    assert abnormal_event_service.list_events("1", SESSION_ID, page=2, page_size=1)["events"] == []
    empty = abnormal_event_service.list_events("1' OR 1=1 --", page=1, page_size=1)
    assert empty == {"events": [], "total": 0, "page": 1, "page_size": 1, "total_pages": 1}


def test_event_page_honors_zero_and_exclusive_end(abnormal_event_service: AbnormalEventService) -> None:
    """验证分页沿用零时间边界及排他上界。

    Args:
        abnormal_event_service: 临时异常事件服务。

    Returns:
        None  # 零时刻被保留，100 秒记录被排除
    """
    repository = abnormal_event_service.abnormal_event_repo
    repository.insert(0.0, "1", "epoch", "测试异常", "{}")
    repository.insert(-1.0, "1", "before", "测试异常", "{}")
    page = repository.get_event_page("1", start_created_at=0.0, end_created_at=100.0, limit=1)
    assert page["total"] == 1
    assert [event["session_id"] for event in page["events"]] == ["epoch"]
