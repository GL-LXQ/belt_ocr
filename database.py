"""使用独立写入任务保存正常测量结果，失败时返回错误并打印日志。"""

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
        """创建记录表并升级历史库中的频率明细字段。

        Args:
            无外部参数。

        Returns:
            None  # 数据库已就绪，历史记录保留原始提交内容和哈希
        """
        # 创建存储目录和当前版本的测量、未受理记录表。
        self.configuration.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.configuration.database_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS measurements (
                    session_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    close_time TEXT,
                    finish_time TEXT NOT NULL,
                    ordered_lines TEXT NOT NULL,
                    final_frequency_hz REAL,
                    measurement_frequencies TEXT NOT NULL DEFAULT '[]',
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
            # 检查旧库字段，首次升级时增加频率明细列并回填已有记录。
            column_names = {column[1] for column in connection.execute("PRAGMA table_info(measurements)")}
            if "measurement_frequencies" not in column_names:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "ALTER TABLE measurements ADD COLUMN measurement_frequencies TEXT NOT NULL DEFAULT '[]'"
                )
                # 按测量时间回填旧明细，仅更新新增查询列。
                for session_id, payload_json in connection.execute(
                    "SELECT session_id, payload_json FROM measurements"
                ).fetchall():
                    payload = json.loads(payload_json)
                    frequencies = payload.get("frequency_candidates", [])
                    frequencies.sort(key=lambda measurement: (
                        measurement["measured_monotonic"], measurement["source_sequence"],
                    ))
                    connection.execute(
                        "UPDATE measurements SET measurement_frequencies = ? WHERE session_id = ?",
                        (json.dumps(frequencies, ensure_ascii=False), session_id),
                    )
        self.initialized = True

    async def submit(self, request: DatabaseRequest) -> bool:
        """将正常结果加入存储队列，不保存待补交记录。

        Args:
            request: 本轮机器身份、冻结 JSON 和内容哈希。

        Returns:
            True  # 请求已在队列中或本次入队成功，等待数据库回调
            False  # 队列已满，本轮提交失败
        """
        # 判断同一记录是否已经排队。
        if request.session_id in self.queued_records:
            return True

        # 将本轮请求加入队列，队列满时返回失败。
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            return False

        # 登记本轮排队身份。
        self.queued_records.add(request.session_id)
        return True

    def write_record(self, request: DatabaseRequest) -> None:
        """在事务中幂等写入冻结记录及频率明细和最终值。

        Args:
            request: 包含记录身份、冻结 JSON 和内容哈希的提交请求。

        Returns:
            None  # 记录已写入或已存在相同内容，冲突时抛出异常
        """
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
                "INSERT INTO measurements ("
                "session_id, machine_id, start_time, close_time, finish_time, ordered_lines, "
                "final_frequency_hz, final_measurement_id, evidence_refs, outcome, error_codes, "
                "is_simulated, payload_json, payload_hash, measurement_frequencies) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.session_id, request.machine_id, payload["start_time"],
                    payload["close_time"], payload["finish_time"],
                    json.dumps(payload["ordered_lines"], ensure_ascii=False),
                    payload["final_frequency_hz"], payload["final_measurement_id"],
                    json.dumps(payload["evidence_refs"], ensure_ascii=False),
                    payload["outcome"], json.dumps(payload["error_codes"]),
                    int(payload["is_simulated"]), request.payload_json,
                    request.payload_hash,
                    json.dumps(payload["measurement_frequencies"], ensure_ascii=False),
                ),
            )

    async def run(self) -> None:
        """逐条写入正常结果，交付成功或失败回调，不自动重试。

        Args:
            无外部参数。

        Returns:
            None  # 持续消费队列，直到应用取消存储任务
        """
        while True:
            # 读取本轮冻结请求，准备成功回调。
            request = await self.queue.get()
            event_type = "CommitSucceeded"
            payload = None
            try:
                try:
                    # 初始化最终数据库并写入本轮正常结果。
                    if not self.initialized:
                        await run_blocking_operation(self.initialize)
                    await run_blocking_operation(self.write_record, request)
                    self.available = True
                except Exception as error:
                    # 判断是否为内容冲突，打印错误并准备失败回调。
                    self.available = False
                    event_type = "CommitFailed"
                    payload = {
                        "error_code": (
                            "COMMIT_INTEGRITY_CONFLICT"
                            if isinstance(error, ValueError)
                            else "DATABASE_WRITE_FAILED"
                        ),
                    }
                    logger.exception(
                        "保存失败，不自动重试 machine_id=%s session_id=%s",
                        request.machine_id,
                        request.session_id,
                    )

                # 将本次提交结果返回原 Session。
                await self.publish_event(MeasurementEvent(
                    event_type, request.machine_id, request.session_id, payload,
                ))
            finally:
                # 移除排队身份并结算本次队列任务。
                self.queued_records.discard(request.session_id)
                self.queue.task_done()
