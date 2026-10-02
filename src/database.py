"""管理本地数据库、机器表、实例锁、异常事件和测量结果写入。"""

import dataclasses
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from config_util import AppConfig
from repo.machine_repo import MachineRepo
from repo.measurement_record_repo import MeasurementRecordRepo
from repo.abnormal_event_repo import AbnormalEventRepo


logger = logging.getLogger(__name__)


def save_evidence_image(image_data: bytes, image_path: Path) -> None:
    """将编码后的图片同步写盘并原子发布。

    Args:
        image_data: 完整图片文件字节。
        image_path: 本轮证据文件路径。

    Returns:
        返回示例：
            None  # 图片已保存，失败时抛出文件操作异常
    """
    # 为本帧生成临时文件路径。
    temporary_path = image_path.with_suffix(image_path.suffix + ".partial")
    try:
        # 创建图片目录，将本帧内容写入临时文件并同步到磁盘。
        image_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("wb") as evidence_file:
            evidence_file.write(image_data)
            evidence_file.flush()
            os.fsync(evidence_file.fileno())

        # 将写入完成的临时文件发布为正式证据文件。
        os.replace(temporary_path, image_path)
    finally:
        # 删除写入失败后可能留下的临时文件。
        if temporary_path.exists():
            temporary_path.unlink()


def serialize_value(value):
    """把事件和运行状态转换为可持久化的结构。

    Args:
        value: 待转换的数据，支持数据类、集合、字典和路径。

    Returns:
        返回示例：
            {
                "machine_id": "M01",  # 机器编号
                "session_id": "session-1",  # 测量周期编号
            }
    """
    # 数据类逐字段转换，跳过不入库的处理回执字段。
    if dataclasses.is_dataclass(value):
        return {
            attribute.name: serialize_value(getattr(value, attribute.name))
            for attribute in dataclasses.fields(value)
            if attribute.name != "acknowledgement"
        }

    # 字典按键转字符串后逐项转换。
    if isinstance(value, dict):
        return {str(key): serialize_value(item) for key, item in value.items()}

    # 集合逐项转换为列表。
    if isinstance(value, (tuple, list, set)):
        return [serialize_value(item) for item in value]

    # 路径转换为字符串。
    if isinstance(value, Path):
        return str(value)

    # 其余标量原样返回。
    return value


@dataclass(frozen=True)
class MeasurementRecord:
    """保存一轮测量需要写入数据库的业务字段。"""

    machine_id: str  # 机器编号
    session_id: str  # 测量周期编号
    start_time: str  # 本轮开始时间
    finish_time: str  # 本轮结算时间
    recognized_lines: tuple[str, ...]  # 大写且去除所有空白的正式识别文字
    final_frequency_hz: float | None  # 最终频率
    measurement_frequencies: tuple[dict, ...]  # 频率明细
    evidence_directory: Path  # 本轮证据图片目录
    needs_review: bool  # 是否需要人工复核
    review_reason: str | None  # 人工复核原因


class CommitIntegrityConflictError(ValueError):
    """标记同一 Session 已有记录与本次提交内容冲突。"""


class Database:
    def __init__(self, config: AppConfig) -> None:
        """初始化各表访问对象和连接状态。

        Args:
            config: 数据库路径配置。

        Returns:
            返回示例：
                None  # 数据库管理对象已初始化，尚未打开数据库
        """
        # 保存数据库配置。
        self.config = config

        # 创建机器与异常事件表的访问对象。
        self.machine_repo = MachineRepo(config.database_path)
        self.abnormal_event_repo = AbnormalEventRepo(config.recovery_path)

        # 创建测量记录写入锁。
        self._measurement_write_lock = threading.Lock()

        # 初始化实例锁文件与运行库连接状态。
        self.lock_file = None
        self.anchor_connection = None
        self.lock_acquired = False

    def initialize(self) -> None:
        """初始化运行库、实例锁和最终结果库。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 本次运行需要的数据库和实例锁已就绪
        """
        # 首次初始化时创建运行库并抢占实例锁。
        if not self.lock_acquired:
            self.initialize_runtime_database()

        # 每次调用都检查最终结果库结构。
        self.initialize_result_database()

        # 记录双库初始化完成及所在路径。
        logger.info(
            "数据库初始化完成 database_path=%s recovery_path=%s",
            self.config.database_path,
            self.config.recovery_path,
        )

    def initialize_runtime_database(self) -> None:
        """锁定本地实例并创建本地运行表。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 本地运行库和异常事件表已就绪
        """
        # 取出运行库路径并创建所在目录。
        runtime_database_path = self.config.recovery_path
        runtime_database_path.parent.mkdir(parents=True, exist_ok=True)

        # 打开实例锁文件。
        lock_path = runtime_database_path.with_suffix(".lock")
        self.lock_file = lock_path.open("a+b")

        # 锁文件为空时写入一个占位字节。
        if lock_path.stat().st_size == 0:
            self.lock_file.write(b"0")
            self.lock_file.flush()
        self.lock_file.seek(0)

        # 对同一个运行库建立进程级独占锁。
        try:
            # Windows 使用 msvcrt 加锁。
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            # 其他平台使用 fcntl 加锁。
            else:
                import fcntl
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # 标记本次运行已持有实例锁。
            self.lock_acquired = True
        except OSError:
            # 加锁失败时关闭锁文件并按已有实例退出。
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("该测量系统已经在运行。") from None

        # 创建异常事件审计表。
        with closing(sqlite3.connect(runtime_database_path, timeout=1)) as connection:
            # 开启 WAL 日志与同步写入。
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                self.abnormal_event_repo.create_table(connection)

        # 保持本次运行的运行库连接。
        self.anchor_connection = sqlite3.connect(runtime_database_path, check_same_thread=False)

    def initialize_result_database(self) -> None:
        """创建业务数据库中的机器表和测量结果表。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 机器表和测量结果表已就绪
        """
        # 创建业务数据库目录并打开连接。
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.config.database_path)) as connection, connection:
            # 创建机器身份、绑定和维护信息表。
            self.machine_repo.create_table(connection)

            # 创建测量结果表。
            MeasurementRecordRepo.create_table(connection)

    def write_measurement_record(self, record: MeasurementRecord) -> None:
        """在事务中幂等写入本轮测量记录，并重试一次写锁竞争。

        Args:
            record: 本轮测量的业务字段和证据图片目录。

        Returns:
            返回示例：
                None  # 记录已写入或已存在相同内容，冲突时抛出异常
        """
        # 将业务字段整理为测量表对应的列值。
        record_values = (
            record.machine_id,
            record.start_time,
            record.finish_time,
            json.dumps(record.recognized_lines, ensure_ascii=False),
            record.final_frequency_hz,
            str(record.evidence_directory),
            json.dumps(record.measurement_frequencies, ensure_ascii=False, sort_keys=True),
            int(record.needs_review),
            record.review_reason,
        )

        # 串行执行测量记录的数据库事务。
        with self._measurement_write_lock:
            for attempt_number in range(2):
                try:
                    # 每次尝试都重新连接并执行完整事务。
                    with closing(
                        sqlite3.connect(self.config.database_path, timeout=1)
                    ) as connection:
                        with connection:
                            connection.execute("BEGIN IMMEDIATE")

                            # 查询同一周期已保存的业务字段。
                            existing_record = connection.execute(
                                "SELECT machine_id, start_time, finish_time, "
                                "recognized_lines, final_frequency_hz, "
                                "evidence_directory, measurement_frequencies, "
                                "needs_review, review_reason "
                                "FROM measurement_records WHERE session_id = ?",
                                (record.session_id,),
                            ).fetchone()

                            # 已有记录时核对内容，一致则结束写入。
                            if existing_record is not None:
                                if existing_record != record_values:
                                    raise CommitIntegrityConflictError(
                                        "同一 Session 的提交内容不一致。"
                                    )

                                # 记录本轮内容一致，跳过重复写入。
                                logger.warning(
                                    "测量记录已存在且内容一致，跳过重复写入 session_id=%s",
                                    record.session_id,
                                )
                                return

                            # 写入本轮测量记录。
                            connection.execute(
                                "INSERT INTO measurement_records (session_id, machine_id, "
                                "start_time, finish_time, recognized_lines, "
                                "final_frequency_hz, evidence_directory, "
                                "measurement_frequencies, needs_review, "
                                "review_reason) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (record.session_id, *record_values),
                            )
                    return
                except sqlite3.OperationalError as error:
                    # 识别当前 Python 版本提供的 SQLite 写锁错误信息。
                    error_name = getattr(error, "sqlite_errorname", None)
                    write_lock_busy = (
                        error_name.startswith("SQLITE_BUSY")
                        if error_name is not None
                        else str(error).lower() == "database is locked"
                    )
                    if not write_lock_busy or attempt_number == 1:
                        raise

                    # 关闭本次连接后等待下一次写入尝试。
                    logger.warning(
                        "测量记录写锁竞争，准备重试 session_id=%s",
                        record.session_id,
                    )
                    time.sleep(0.1)

    def close(self) -> None:
        """关闭运行库连接并释放进程锁。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 运行库连接和实例锁已释放，关闭失败时抛出首次异常
        """
        first_error: Exception | None = None

        # 关闭本次运行保持的数据库连接。
        if self.anchor_connection is not None:
            try:
                self.anchor_connection.close()
            except Exception as error:
                first_error = error
            else:
                self.anchor_connection = None

        # 释放进程锁。
        if self.lock_file is not None:
            try:
                if self.lock_acquired and os.name == "nt":
                    import msvcrt
                    self.lock_file.seek(0)
                    msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    self.lock_acquired = False
                elif self.lock_acquired:
                    import fcntl
                    fcntl.flock(self.lock_file, fcntl.LOCK_UN)
                    self.lock_acquired = False
            except Exception as error:
                if first_error is None:
                    first_error = error

            # 关闭锁文件并复位持锁状态。
            try:
                self.lock_file.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
            else:
                self.lock_file = None
                self.lock_acquired = False

        # 所有资源处理后抛出最先发生的关闭异常。
        if first_error is not None:
            raise first_error

    def save_abnormal_event(
        self,
        reason: str,
        event=None,
        machine_id: str = "",
        session_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """保存业务事件或 Session 失败的异常记录。

        Args:
            reason: 异常原因描述。
            event: 对应的业务事件，未提供时使用独立字段。
            machine_id: 未提供业务事件时使用的机器编号。
            session_id: 未提供业务事件时使用的周期编号。
            payload: 未提供业务事件时保存的附加内容。

        Returns:
            返回示例：
                None  # 异常事件已经写入本地运行库
        """
        # 按事件或独立字段整理审计内容。
        event_payload = serialize_value(event) if event is not None else payload or {}
        event_machine_id = event.machine_id if event is not None else machine_id
        event_session_id = event.session_id if event is not None else session_id

        # 写入异常事件记录。
        self.abnormal_event_repo.insert(
            time.time(),
            event_machine_id,
            event_session_id,
            reason,
            json.dumps(event_payload, ensure_ascii=False),
        )
