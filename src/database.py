"""管理本地数据库、设备表、实例锁、异常事件和测量结果写入。"""

from enums import EventType
import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from async_utils import run_blocking_operation
from configuration import MeasurementConfiguration
from models import CapturedFrame, MeasurementEvent, PublishEvent
from repo.machine_repo import MachineRepo
from repo.measurement_repo import MeasurementRepo
from repo.abnormal_event_repo import AbnormalEventRepo


logger = logging.getLogger(__name__)


def save_evidence_image(image_data: bytes, image_path: Path) -> None:
    """将编码后的图片同步写盘并原子发布。

    Args:
        image_data: 完整图片文件字节。
        image_path: 本轮证据文件路径。

    Returns:
        None  # 图片已保存，失败时抛出文件操作异常
    """
    # 为本帧生成临时文件路径。
    temporary_path = image_path.with_suffix(image_path.suffix + ".partial")
    try:
        # 将本帧内容写入临时文件并同步到磁盘。
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
        object: 可由 JSON 序列化的数据。
        返回示例：
            {
                "machine_id": "M01",  # 机器编号
                "session_id": "session-1",  # 测量周期编号
            }
    """
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


@dataclass(frozen=True)
class DatabaseRequest:
    machine_id: str
    session_id: str
    start_time: str
    finish_time: str
    ordered_lines: tuple[str, ...]
    final_frequency_hz: float
    measurement_frequencies: tuple[dict, ...]
    evidence_refs: tuple[str, ...]
    selected_frames: tuple[CapturedFrame, ...] = ()


class Database:
    def __init__(self, configuration: MeasurementConfiguration, publish_event: PublishEvent) -> None:
        """初始化各表访问对象、存储队列和连接状态。

        Args:
            configuration: 数据库路径和存储队列配置。
            publish_event: 提交结果的异步事件发布函数。

        Returns:
            返回示例：
                None  # 数据库管理对象已初始化，尚未打开数据库
        """
        # 保存存储配置和结果事件发布入口。
        self.configuration = configuration
        self.publish_event = publish_event
        # 创建各表的数据库访问对象。
        self.machine_repo = MachineRepo(configuration.database_path)
        self.measurement_repo = MeasurementRepo(configuration.database_path)
        self.abnormal_event_repo = AbnormalEventRepo(configuration.recovery_path)

        # 创建存储队列并初始化连接与实例锁状态。
        self.queue: asyncio.Queue[DatabaseRequest] = asyncio.Queue(configuration.storage_queue_capacity)
        self.runtime_available = True
        self.queued_records: set[str] = set()
        self.initialized = False
        self.lock_file = None
        self.anchor_connection = None
        self.lock_acquired = False

    def initialize(self) -> None:
        """初始化运行库、实例锁和最终结果库。

        Args:
            无外部参数。

        Returns:
            None: 本次运行需要的数据库和实例锁已就绪。
            返回示例：
                None  # 无返回数据
        """
        # 首次初始化本地运行库，每次调用都检查最终结果库结构。
        if not self.lock_acquired:
            self.initialize_runtime_database()
        self.initialize_result_database()
        self.initialized = True

    def initialize_runtime_database(self) -> None:
        """锁定本地实例并创建本地运行表。

        Args:
            无外部参数。

        Returns:
            None: 本地运行库和异常事件表已就绪。
            返回示例：
                None  # 无返回数据
        """
        # 创建运行库目录和实例锁文件。
        runtime_database_path = self.configuration.recovery_path
        runtime_database_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = runtime_database_path.with_suffix(".lock")
        self.lock_file = lock_path.open("a+b")
        if lock_path.stat().st_size == 0:
            self.lock_file.write(b"0")
            self.lock_file.flush()
        self.lock_file.seek(0)

        # 对同一个运行库建立进程级独占锁。
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

        # 创建异常事件审计表。
        with closing(sqlite3.connect(runtime_database_path, timeout=1)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                self.abnormal_event_repo.create_table(connection)

        # 保持本次运行的运行库连接。
        self.anchor_connection = sqlite3.connect(runtime_database_path, check_same_thread=False)

    def initialize_result_database(self) -> None:
        """创建业务数据库中的设备表和测量结果表。

        Args:
            无外部参数。

        Returns:
            None: 设备表和测量结果表已就绪。
            返回示例：
                None  # 无返回数据
        """
        # 创建业务数据库目录并打开连接。
        self.configuration.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.configuration.database_path)) as connection, connection:
            # 创建机器身份、设备绑定和维护信息表。
            self.machine_repo.create_table(connection)

            # 创建测量结果表。
            self.measurement_repo.create_table(connection)

    def close(self) -> None:
        """关闭运行库连接并释放进程锁。

        Args:
            无外部参数。

        Returns:
            None: 运行库连接和实例锁已释放。
            返回示例：
                None  # 无返回数据
        """
        # 关闭本次运行保持的数据库连接。
        if self.anchor_connection is not None:
            self.anchor_connection.close()
            self.anchor_connection = None

        # 释放进程锁并关闭锁文件。
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

    def save_abnormal_event(self, reason: str, event=None, machine_id: str = "") -> None:
        """保存异常原因以及对应的事件内容。

        Args:
            reason: 异常原因标识。
            event: 对应的业务事件，未提供时保存空对象。
            machine_id: 未提供业务事件时使用的机器编号。

        Returns:
            None: 异常事件已经写入本地运行库。
            返回示例：
                None  # 无返回数据
        """
        # 序列化事件，并写入异常事件记录表。
        payload = serialize_value(event) if event is not None else {}
        self.abnormal_event_repo.insert(
            time.time(),
            getattr(event, "machine_id", machine_id),
            getattr(event, "session_id", None),
            reason,
            json.dumps(payload, ensure_ascii=False),
        )

    async def submit(self, request: DatabaseRequest) -> bool:
        """将正常结果加入存储队列，不保存待补交记录。

        Args:
            request: 本轮机器身份、业务字段和选中图片。

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

    def persist_measurement(self, request: DatabaseRequest) -> None:
        """保存最终图片后写入测量记录，明确未提交时清理本次新建图片。

        Args:
            request: 冻结内容、图片和周期身份。

        Returns:
            None  # 图片和测量记录保存成功，失败时抛出原始异常
        """
        # 读取冻结路径，记录本次创建的文件。
        created_paths = []
        database_attempted = False
        try:
            for frame, evidence_ref in zip(request.selected_frames, request.evidence_refs):
                image_path = Path(evidence_ref)
                if not image_path.exists():
                    created_paths.append(image_path)
                    save_evidence_image(frame.image_data, image_path)
            # 全部图片写入成功后才执行数据库事务。
            database_attempted = True
            self.measurement_repo.write_record(request)
        except Exception:
            # 查询提交结果，无法确认时保留图片并记录异常。
            definitely_uncommitted = not database_attempted
            if database_attempted:
                try:
                    definitely_uncommitted = not self.measurement_repo.exists_by_session_id(request.session_id)
                except Exception:
                    logger.exception("无法确认提交结果，保留图片 session_id=%s", request.session_id)
            # 仅清理本次创建且确认没有入库的图片。
            if definitely_uncommitted:
                for image_path in created_paths:
                    try:
                        image_path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("清理未提交图片失败 path=%s", image_path)
            raise

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
            event_type = EventType.COMMIT_SUCCEEDED
            payload = None
            try:
                try:
                    # 初始化最终数据库并写入本轮正常结果。
                    if not self.initialized:
                        await run_blocking_operation(self.initialize)
                    await run_blocking_operation(self.persist_measurement, request)
                except Exception as error:
                    # 判断是否为内容冲突，打印错误并准备失败回调。
                    event_type = EventType.COMMIT_FAILED
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
                    # 响应阻塞写入期间的取消请求，停止后续结果发布和队列消费。
                    await asyncio.sleep(0)

                # 将本次提交结果返回原 Session。
                await self.publish_event(MeasurementEvent(
                    event_type, request.machine_id, request.session_id, payload,
                ))
            finally:
                # 移除排队身份并结算本次队列任务。
                self.queued_records.discard(request.session_id)
                self.queue.task_done()
                request = None
