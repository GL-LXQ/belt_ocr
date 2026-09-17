"""使用独立写入任务保存 SQLite 测量记录和未受理事件。"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from contextlib import closing

from configuration import MeasurementConfiguration
from models import MeasurementEvent, PublishEvent
from recovery import run_blocking_operation, RecoveryStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseRequest:
    machine_id: str
    session_id: str
    payload_json: str
    payload_hash: str
    record_type: str = "measurement"


class Database:
    def __init__(
        self, configuration: MeasurementConfiguration, publish_event: PublishEvent,
        recovery: RecoveryStore,
    ) -> None:
        self.configuration = configuration
        self.publish_event = publish_event
        self.queue: asyncio.Queue[DatabaseRequest] = asyncio.Queue(
            configuration.storage_queue_capacity
        )
        self.available = True
        self.recovery = recovery
        self.queued_records: set[str] = set()
        self.initialized = False

    def initialize(self) -> None:
        """创建数据库目录和记录表。"""
        self.configuration.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.configuration.database_path)) as connection:
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
        self.initialized = True

    async def submit(self, request: DatabaseRequest) -> bool:
        """把不可变记录加入写入队列。"""
        # 登记本次提交身份，再持久化并进入有界队列。
        if request.session_id in self.queued_records:
            return True
        self.queued_records.add(request.session_id)
        try:
            staged = await run_blocking_operation(self.recovery.stage_record, request)
            if not staged:
                self.queued_records.discard(request.session_id)
                if request.record_type == "measurement":
                    await self.publish_event(MeasurementEvent(
                        "CommitSucceeded", request.machine_id, request.session_id,
                    ))
                return True
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            self.queued_records.discard(request.session_id)
            return False
        except (asyncio.CancelledError, Exception):
            self.queued_records.discard(request.session_id)
            raise
        return True

    def write_record(self, request: DatabaseRequest) -> None:
        """在事务中检查重复记录并写入同一份冻结内容。"""
        connection = sqlite3.connect(self.configuration.database_path, timeout=1)
        with closing(connection), connection:
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
        """持续消费存储队列，执行写入和有限重试，并通过事件报告提交结果。

        Args:
            无外部参数。

        Returns:
            None: 持续运行直到任务被取消，提交结果通过事件发送，无返回数据。
            返回值形式示例：
                None  # 无返回数据
        """
        while True:
            # 等待并取出一份待写入记录。
            request = await self.queue.get()
            try:
                # 初始化本次提交的成功状态和内容冲突标记。
                succeeded = False
                integrity_conflict = False

                # 对同一份冻结记录执行有限次数的提交。
                for attempt_number in range(self.configuration.storage_retry_attempts):
                    try:
                        # 首次写入前创建数据库目录和数据表。
                        if not self.initialized:
                            await run_blocking_operation(self.initialize)

                        # 写入最终记录，并登记待提交记录已完成。
                        await run_blocking_operation(self.write_record, request)
                        await run_blocking_operation(self.recovery.complete_record, request)

                        # 标记提交成功和存储可用，结束本次重试循环。
                        succeeded = True
                        self.available = True
                        break
                    except ValueError:
                        # 登记提交内容冲突，记录审计和日志，结束本次重试循环。
                        integrity_conflict = True
                        await run_blocking_operation(self.recovery.audit, "COMMIT_INTEGRITY_CONFLICT", request)
                        logger.exception("提交内容冲突 session_id=%s", request.session_id)
                        break
                    except Exception:
                        # 标记存储不可用并记录本次写入异常。
                        self.available = False
                        logger.exception(
                            "保存失败 machine_id=%s session_id=%s attempt=%s",
                            request.machine_id,
                            request.session_id,
                            attempt_number + 1,
                        )

                        # 等待配置的重试间隔。
                        await asyncio.sleep(self.configuration.storage_retry_delay_ms / 1000)

                # 保存下一次自动补交时间，冲突记录停止自动重试。
                if not succeeded:
                    await run_blocking_operation(
                        self.recovery.delay_record,
                        request.session_id,
                        self.configuration.storage_retry_interval_ms / 1000,
                        blocked=integrity_conflict,
                    )

                # 把提交状态返回原 Session。
                if request.record_type == "measurement":
                    await self.publish_event(
                        MeasurementEvent(
                            "CommitSucceeded" if succeeded else "CommitFailed",
                            request.machine_id,
                            request.session_id,
                            {
                                "integrity_conflict": integrity_conflict,
                            },
                        )
                    )
            finally:
                # 移除本次排队标记，并通知队列任务已处理完毕。
                self.queued_records.discard(request.session_id)
                self.queue.task_done()

    async def enqueue_pending_records(self) -> None:
        """按存储队列剩余容量读取到期的待提交记录并重新入队。

        Args:
            无外部参数。

        Returns:
            None: 完成本轮补交入队，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 计算存储队列的剩余容量。
        remaining_capacity = self.queue.maxsize - self.queue.qsize()

        # 按剩余容量读取已到期且未被阻止补交的持久化记录。
        records = await run_blocking_operation(self.recovery.pending_records, remaining_capacity)

        for record in records:
            # 使用原有冻结内容和摘要组装存储请求。
            request = DatabaseRequest(
                record["machine_id"],
                record["record_id"],
                record["payload_json"],
                record["payload_hash"],
                record["record_type"],
            )

            # 提交记录，队列满时结束本轮入队。
            if not await self.submit(request):
                break
