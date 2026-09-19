"""验证图片与记录统一提交、幂等写入和失败文件清理。"""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

import database
from test_frequency_pipeline import create_measurement, frequency_context


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
    session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))
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
    session.measurement_frequencies.append(create_measurement(session, 1, 12, 42))
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
