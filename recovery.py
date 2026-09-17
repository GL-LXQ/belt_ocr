"""保存待提交记录和异常审计。"""

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


logger = logging.getLogger(__name__)


async def run_blocking_operation(operation, *arguments, **keyword_arguments):
    """在线程中执行阻塞操作，取消时等待文件和事务释放。"""
    task = asyncio.create_task(asyncio.to_thread(
        operation, *arguments, **keyword_arguments,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            logger.exception("释放阻塞操作时发生异常")
        raise


def serialize_value(value):
    """把事件和运行状态转换为可持久化的结构。"""
    if dataclasses.is_dataclass(value):
        return {
            attribute.name: serialize_value(getattr(value, attribute.name))
            for attribute in dataclasses.fields(value)
            if attribute.name != "acknowledgement"
        }
    if isinstance(value, dict):
        return {str(key): serialize_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [serialize_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class RecoveryStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.lock_file = None
        self.available = True
        self.anchor_connection = None
        self.lock_acquired = False

    def initialize(self) -> None:
        """锁定本地实例，创建运行表并清理上次运行的待处理状态。

        Args:
            无外部参数。

        Returns:
            None: 完成本次运行的本地存储初始化，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 创建恢复库目录和实例锁文件。
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.database_path.with_suffix(".lock")
        self.lock_file = lock_path.open("a+b")
        if lock_path.stat().st_size == 0:
            self.lock_file.write(b"0")
            self.lock_file.flush()
        self.lock_file.seek(0)

        # 对同一个恢复库建立进程级独占锁。
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_acquired = True
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("该测量系统已经在运行。") from None

        # 初始化独立于最终结果库的恢复数据。
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pending_records (
                    record_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    retry_at REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS committed_records (
                    record_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_entries (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    machine_id TEXT,
                    session_id TEXT,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

            # 删除旧版本检查点表，并清理上次运行的全部待提交记录。
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DROP TABLE IF EXISTS machine_checkpoints")
                connection.execute("DELETE FROM pending_records")

        # 保持本次运行的恢复库连接。
        self.anchor_connection = sqlite3.connect(
            self.database_path, check_same_thread=False,
        )

    def close(self) -> None:
        """释放恢复库的进程锁。"""
        if self.anchor_connection is not None:
            self.anchor_connection.close()
            self.anchor_connection = None
        if self.lock_file is not None:
            if self.lock_acquired and os.name == "nt":
                import msvcrt
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            elif self.lock_acquired:
                import fcntl
                fcntl.flock(self.lock_file, fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None
            self.lock_acquired = False

    def audit(self, reason: str, event=None, machine_id: str = "") -> None:
        """保存异常原因以及对应的事件内容。"""
        payload = serialize_value(event) if event is not None else {}
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO audit_entries "
                    "(created_at, machine_id, session_id, reason, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        time.time(), getattr(event, "machine_id", machine_id),
                        getattr(event, "session_id", None), reason,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )

    def stage_record(self, request) -> bool:
        """在进入内存队列前持久保存不可变提交记录。"""
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                committed = connection.execute(
                    "SELECT payload_hash FROM committed_records WHERE record_id = ?",
                    (request.session_id,),
                ).fetchone()
                if committed:
                    if committed[0] != request.payload_hash:
                        raise ValueError("已提交记录内容冲突。")
                    return False
                existing = connection.execute(
                    "SELECT payload_hash, payload_json FROM pending_records "
                    "WHERE record_id = ?", (request.session_id,),
                ).fetchone()
                if existing and existing != (
                    request.payload_hash, request.payload_json,
                ):
                    raise ValueError("待提交记录内容冲突。")
                connection.execute(
                    "INSERT OR IGNORE INTO pending_records "
                    "(record_id, machine_id, record_type, payload_json, payload_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        request.session_id, request.machine_id, request.record_type,
                        request.payload_json, request.payload_hash,
                    ),
                )
        return True

    def pending_records(self, limit: int, due_only: bool = True) -> list[dict]:
        """读取有界数量的待提交记录。"""
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM pending_records WHERE blocked = 0 "
                "AND retry_at <= ? ORDER BY retry_at, rowid LIMIT ?",
                (time.time() if due_only else float("inf"), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_status(self, record_id: str) -> dict:
        """读取指定记录的持久化提交状态。"""
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            committed = connection.execute(
                "SELECT * FROM committed_records WHERE record_id = ?", (record_id,),
            ).fetchone()
            pending = connection.execute(
                "SELECT * FROM pending_records WHERE record_id = ?", (record_id,),
            ).fetchone()
        return {
            "committed": dict(committed) if committed else None,
            "pending": dict(pending) if pending else None,
        }

    def complete_record(self, request) -> None:
        """记录最终库已确认成功并移除待提交内容。"""
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO committed_records VALUES (?, ?)",
                    (request.session_id, request.payload_hash),
                )
                connection.execute(
                    "DELETE FROM pending_records WHERE record_id = ?",
                    (request.session_id,),
                )

    def delay_record(self, record_id: str, delay_seconds: float, blocked=False) -> None:
        """保存重试时间或完整性冲突状态。"""
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            with connection:
                connection.execute(
                    "UPDATE pending_records SET attempts = attempts + 1, "
                    "retry_at = ?, blocked = ? WHERE record_id = ?",
                    (time.time() + delay_seconds, int(blocked), record_id),
                )

    def pending_count(self) -> int:
        """读取持久化待提交积压数量。"""
        with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
            result = connection.execute(
                "SELECT COUNT(*) FROM pending_records"
            ).fetchone()
        return result[0]
