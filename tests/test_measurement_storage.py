"""验证图片与记录统一提交、幂等写入和失败文件清理。"""

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import database
from test_frequency_pipeline import create_measurement, frequency_context


def test_cancelled_storage_failure_stops_before_next_request(frequency_context, monkeypatch, caplog):
    """验证取消中的写入失败不会继续消费下一条存储请求。

    Args:
        frequency_context: 已准备 OCR 结果的应用、处理器和周期。
        monkeypatch: 存储操作替换工具。
        caplog: 日志捕获器。

    Returns:
        None  # 首条请求已释放，下一条请求未执行，写入错误只记录一次
    """
    app, manager, session = frequency_context
    writing_started = threading.Event()
    release_write = threading.Event()
    writing_failure = OSError("取消期间写入失败")

    def fail_write_after_release(request):
        """等待测试释放后抛出存储异常。

        Args:
            request: 当前冻结的存储请求。

        Returns:
            无返回值  # 等待结束后抛出 OSError
        """
        writing_started.set()
        assert release_write.wait(5)
        raise writing_failure

    async def cancel_storage_during_write():
        """准备两个请求并在首条写入期间取消消费者。

        Args:
            无外部参数。

        Returns:
            None  # 消费者已取消，第二条请求仍在队列中
        """
        # 通过正式结算流程准备请求，并追加下一条待处理请求。
        session.measurement_frequencies.append(create_measurement(session, 12, 42))
        await manager.handle_machine_close()
        app.database.queue.put_nowait(database.DatabaseRequest("M01", "next-session", "{}", "hash"))
        publisher = AsyncMock()
        app.database.publish_event = publisher
        worker = asyncio.create_task(app.database.run())
        try:
            # 重复取消仍等待实际写入结束。
            assert await asyncio.to_thread(writing_started.wait, 2)
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            await asyncio.sleep(0)
            assert not worker.done()
            release_write.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2)

            # 不发布取消后的结果，也不开始下一条存储请求。
            publisher.assert_not_awaited()
            assert session.session_id not in app.database.queued_records
            pending_request = app.database.queue.get_nowait()
            assert pending_request.session_id == "next-session"
            app.database.queue.task_done()
            await asyncio.wait_for(app.database.queue.join(), 1)
        finally:
            release_write.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    monkeypatch.setattr(app.database, "persist_measurement", fail_write_after_release)
    asyncio.run(cancel_storage_during_write())
    failure_logs = [record for record in caplog.records if record.exc_info]
    assert len(failure_logs) == 1
    assert failure_logs[0].exc_info[1] is writing_failure


@pytest.mark.parametrize("failure", ["save", "sync", "replace", "write", "acknowledgement", "unknown", "none"])
def test_persistence_failure_cleanup_and_idempotency(frequency_context, monkeypatch, failure):
    """验证提交失败只清理确认未入库的新文件。

    Args:
        frequency_context: 已准备 OCR 结果的独立周期。
        monkeypatch: 故障注入工具。
        failure: 保存、同步、替换、数据库写入、确认丢失或查询故障。

    Returns:
        None  # 图片、记录和幂等写入结果已核对
    """
    app, manager, session = frequency_context
    session.measurement_frequencies.append(create_measurement(session, 12, 42))
    asyncio.run(manager.handle_machine_close())
    request = app.database.queue.get_nowait()
    payload = json.loads(request.payload_json)
    image_path = Path(payload["evidence_refs"][0])
    original_write = app.database.write_record
    write_calls = []

    def write_with_failure(current_request):
        """按场景注入事务之前或之后的故障。

        Args:
            current_request: 冻结存储请求。

        Returns:
            None  # 正常保存，指定场景抛出异常
        """
        write_calls.append(current_request.session_id)
        if failure in {"write", "unknown"}:
            raise OSError("写入失败")
        original_write(current_request)
        if failure == "acknowledgement":
            raise OSError("提交确认丢失")

    # 注入图片保存或数据库边界故障。
    monkeypatch.setattr(app.database, "write_record", write_with_failure)
    if failure in {"save", "sync", "replace"}:
        target = {"save": "save_evidence_image", "sync": "os.fsync", "replace": "os.replace"}[failure]
        monkeypatch.setattr(f"database.{target}", Mock(side_effect=OSError("图片保存失败")))
    if failure == "unknown":
        monkeypatch.setattr(database.sqlite3, "connect", Mock(side_effect=OSError("无法查询")))
    if failure == "none":
        app.database.persist_measurement(request)
        app.database.persist_measurement(request)
    else:
        with pytest.raises(OSError):
            app.database.persist_measurement(request)

    # 明确未写入时清理，已写入或状态未知时保留图片。
    assert image_path.exists() == (failure in {"none", "acknowledgement", "unknown"})
    assert not list(image_path.parent.glob("*.partial"))
    if failure in {"save", "sync", "replace"}:
        assert not write_calls
    if failure != "unknown":
        with sqlite3.connect(app.configuration.database_path) as connection:
            count = connection.execute("SELECT count(*) FROM measurements").fetchone()[0]
        assert count == (1 if failure in {"none", "acknowledgement"} else 0)
    app.database.queue.task_done()


def test_existing_image_is_never_deleted_on_failure(frequency_context, monkeypatch):
    """验证提交失败不会覆盖或删除原有图片。

    Args:
        frequency_context: 已完成 OCR 的独立周期。
        monkeypatch: 故障注入工具。

    Returns:
        None  # 原有文件保留原始内容
    """
    app, manager, session = frequency_context
    session.measurement_frequencies.append(create_measurement(session, 12, 42))
    asyncio.run(manager.handle_machine_close())
    request = app.database.queue.get_nowait()
    image_path = Path(json.loads(request.payload_json)["evidence_refs"][0])
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"existing")
    monkeypatch.setattr(app.database, "write_record", Mock(side_effect=OSError("写入失败")))
    with pytest.raises(OSError):
        app.database.persist_measurement(request)
    assert image_path.read_bytes() == b"existing"
    app.database.queue.task_done()
