"""验证 Database 自行等待存储完成与丢弃退出时未执行的请求。"""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from config_util import AppConfig
from database import Database, DatabaseRequest
from local_test_support import build_config


def build_database(config: AppConfig) -> Database:
    """建立已初始化双库的测试存储入口。

    Args:
        config: 业务库、运行库和存储队列配置。

    Returns:
        返回示例：
            Database  # 表结构已就绪，提交结果回调不做业务处理
    """
    async def ignore_commit_result(event) -> None:
        """丢弃存储任务交付的提交结果事件。

        Args:
            event: 提交成功或失败事件。

        Returns:
            返回示例：
                None  # 测试只核对写入结果与队列状态
        """
        return None

    database = Database(config, ignore_commit_result)
    database.initialize()
    return database


def build_request(session_id: str) -> DatabaseRequest:
    """组装一条不携带证据图片的最小测量请求。

    Args:
        session_id: 测量周期编号。

    Returns:
        返回示例：
            DatabaseRequest(
                machine_id="1",  # 机器编号
                session_id="session-a",  # 测量周期编号
                start_time="2026-09-22T00:00:00+00:00",  # 本轮开始时间
                finish_time="2026-09-22T00:00:01+00:00",  # 本轮结算时间
                ordered_lines=("MODEL-1",),  # 最终文字顺序
                final_frequency_hz=42.0,  # 本轮最终频率
                measurement_frequencies=({"value_hz": 42.0},),  # 本轮频率明细
                evidence_refs=(),  # 最终图片路径
                selected_frames=(),  # 最终选中图片内容
            )
    """
    return DatabaseRequest(
        machine_id="1",
        session_id=session_id,
        start_time="2026-09-22T00:00:00+00:00",
        finish_time="2026-09-22T00:00:01+00:00",
        ordered_lines=("MODEL-1",),
        final_frequency_hz=42.0,
        measurement_frequencies=({"value_hz": 42.0},),
        evidence_refs=(),
    )


def list_written_session_ids(database_path: Path) -> list[str]:
    """读取测量结果表中已写入的周期编号。

    Args:
        database_path: 业务库文件路径。

    Returns:
        返回示例：
            ["session-a"]  # 已写入的周期编号，按数据库返回顺序排列
    """
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT session_id FROM measurements ORDER BY session_id").fetchall()
    return [row[0] for row in rows]


def test_wait_until_queue_drained_finishes_queued_request(tmp_path: Path) -> None:
    """验证等待存储完成会把已提交请求写入数据库，不丢弃待写数据。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 队列请求已写入，排队身份已清空
    """
    config = build_config(tmp_path)
    database = build_database(config)
    request = build_request("session-a")

    async def submit_and_wait() -> None:
        """启动存储消费任务后提交请求，等待队列处理完成。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 等待存储完成，消费任务已取消
        """
        consumer = asyncio.create_task(database.consume_storage_queue())
        try:
            assert await database.submit(request) is True
            assert database.queued_records == {"session-a"}

            # 等待本次已提交请求写入完成。
            await asyncio.wait_for(database.wait_until_queue_drained(), 2)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(submit_and_wait())

    assert list_written_session_ids(config.database_path) == ["session-a"]
    assert database.queued_records == set()


def test_discard_pending_requests_clears_queue_and_records(tmp_path: Path) -> None:
    """验证强制退出丢弃未执行请求后队列与排队身份均无残留。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 未执行请求已丢弃，后续等待存储完成不会卡住
    """
    config = build_config(tmp_path)
    database = build_database(config)

    async def submit_then_discard() -> None:
        """无消费任务时提交两条请求，丢弃后核对队列状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 队列已清空，等待存储完成立即结束
        """
        assert await database.submit(build_request("session-a")) is True
        assert await database.submit(build_request("session-b")) is True
        assert database.queued_records == {"session-a", "session-b"}

        # 丢弃尚未消费的请求。
        database.discard_pending_requests()
        assert database.queue.empty()
        assert database.queued_records == set()

        # 队列任务计数正确时等待存储完成立即结束。
        await asyncio.wait_for(database.wait_until_queue_drained(), 1)

    asyncio.run(submit_then_discard())

    assert list_written_session_ids(config.database_path) == []


def test_measurement_review_fields_are_saved_and_compared(tmp_path: Path) -> None:
    """验证普通记录与待复核记录的字段写入及同周期内容比较。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 两类记录的复核字段和幂等比较已验证
    """
    # 初始化新业务库并写入普通测量记录。
    config = build_config(tmp_path)
    database = build_database(config)
    database.persist_measurement(build_request("normal-session"))

    # 写入缺少频率的待复核记录并重复提交相同内容。
    review_request = replace(
        build_request("review-session"),
        final_frequency_hz=None,
        measurement_frequencies=(),
        needs_review=True,
        review_reason="FREQUENCY_NO_VALID_MEASUREMENT",
    )
    database.persist_measurement(review_request)
    database.persist_measurement(review_request)

    # 核对两类记录的复核标志、原因和最终频率。
    with closing(sqlite3.connect(config.database_path)) as connection:
        records = connection.execute(
            "SELECT session_id, final_frequency_hz, needs_review, review_reason "
            "FROM measurements ORDER BY session_id"
        ).fetchall()
    assert records == [
        ("normal-session", 42.0, 0, None),
        ("review-session", None, 1, "FREQUENCY_NO_VALID_MEASUREMENT"),
    ]

    # 同一周期的复核理由变化时拒绝覆盖已存记录。
    with pytest.raises(ValueError, match="提交内容不一致"):
        database.persist_measurement(replace(review_request, review_reason="OTHER_REASON"))
