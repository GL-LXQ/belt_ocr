"""使用独立写入任务保存 SQLite 测量记录和未受理事件。"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass

from configuration import MeasurementConfiguration
from models import MeasurementEvent, PublishEvent


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageRequest:
    machine_id: str
    session_id: str
    payload_json: str
    payload_hash: str
    record_type: str = "measurement"


class SQLiteWriter:
    def __init__(
        self, configuration: MeasurementConfiguration, publish_event: PublishEvent
    ) -> None:
        self.configuration = configuration
        self.publish_event = publish_event
        self.queue: asyncio.Queue[StorageRequest] = asyncio.Queue(
            configuration.storage_queue_capacity
        )
        self.available = True

    def initialize(self) -> None:
        """创建数据库目录和记录表。"""
        self.configuration.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.configuration.database_path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS measurements (
                    session_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    close_time TEXT,
                    finish_time TEXT NOT NULL,
                    ordered_lines TEXT NOT NULL,
                    final_frequency_hz REAL,
                    final_measurement_id TEXT,
                    evidence_refs TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    error_codes TEXT NOT NULL,
                    is_simulated INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rejected_cycles (
                    event_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
            """)

    def submit(self, request: StorageRequest) -> bool:
        """把不可变记录加入写入队列。"""
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            self.available = False
            return False
        return True

    def write_record(self, request: StorageRequest) -> None:
        """在事务中检查重复记录并写入同一份冻结内容。"""
        with sqlite3.connect(self.configuration.database_path, timeout=1) as connection:
            # 保存本轮未受理事件。
            if request.record_type == "rejected_cycle":
                connection.execute(
                    "INSERT OR IGNORE INTO rejected_cycles VALUES (?, ?, ?)",
                    (request.session_id, request.machine_id, request.payload_json),
                )
                return

            # 检查同一 Session 已有记录是否与本次提交一致。
            connection.execute("BEGIN IMMEDIATE")
            existing_record = connection.execute(
                "SELECT payload_hash, payload_json FROM measurements "
                "WHERE session_id = ?",
                (request.session_id,),
            ).fetchone()
            if existing_record is not None:
                if existing_record != (request.payload_hash, request.payload_json):
                    raise ValueError("同一 Session 的提交内容不一致。")
                return

            # 写入查询字段和完整冻结内容。
            payload = json.loads(request.payload_json)
            connection.execute(
                "INSERT INTO measurements VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.session_id, request.machine_id, payload["start_time"],
                    payload["close_time"], payload["finish_time"],
                    json.dumps(payload["ordered_lines"], ensure_ascii=False),
                    payload["final_frequency_hz"], payload["final_measurement_id"],
                    json.dumps(payload["evidence_refs"], ensure_ascii=False),
                    payload["outcome"], json.dumps(payload["error_codes"]),
                    int(payload["is_simulated"]), request.payload_json,
                    request.payload_hash,
                ),
            )

    async def run(self) -> None:
        """独立提交记录，并对临时写入失败进行有限重试。"""
        while True:
            request = await self.queue.get()
            try:
                # 对同一份冻结记录执行有限次数的提交。
                succeeded = False
                for attempt_number in range(self.configuration.storage_retry_attempts):
                    try:
                        await asyncio.to_thread(self.write_record, request)
                        succeeded = True
                        self.available = True
                        break
                    except Exception:
                        self.available = False
                        logger.exception(
                            "保存失败 machine_id=%s session_id=%s attempt=%s",
                            request.machine_id, request.session_id, attempt_number + 1,
                        )
                        await asyncio.sleep(
                            self.configuration.storage_retry_delay_ms / 1000
                        )

                # 把提交状态返回原 Session。
                if request.record_type == "measurement":
                    await self.publish_event(MeasurementEvent(
                        "CommitSucceeded" if succeeded else "CommitFailed",
                        request.machine_id, request.session_id,
                    ))
            finally:
                self.queue.task_done()
