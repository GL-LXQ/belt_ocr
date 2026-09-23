"""管理本地数据库、机器表、实例锁、异常事件和测量结果写入。"""

import dataclasses
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from config_util import AppConfig
from repo.machine_repo import MachineRepo
from repo.measurement_repo import MeasurementRepo
from repo.abnormal_event_repo import AbnormalEventRepo


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

        # 创建各表的数据库访问对象。
        self.machine_repo = MachineRepo(config.database_path)
        self.measurement_repo = MeasurementRepo(config.database_path)
        self.abnormal_event_repo = AbnormalEventRepo(config.recovery_path)

        # 创建测量记录写入锁。
        self.measurement_write_lock = threading.Lock()

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
            self.measurement_repo.create_table(connection)

    def close(self) -> None:
        """关闭运行库连接并释放进程锁。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 运行库连接和实例锁已释放
        """
        # 关闭本次运行保持的数据库连接。
        if self.anchor_connection is not None:
            self.anchor_connection.close()
            self.anchor_connection = None

        # 释放进程锁并关闭锁文件。
        if self.lock_file is not None:
            # Windows 按字节区间解锁。
            if self.lock_acquired and os.name == "nt":
                import msvcrt
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            # 其他平台释放文件锁。
            elif self.lock_acquired:
                import fcntl
                fcntl.flock(self.lock_file, fcntl.LOCK_UN)

            # 关闭锁文件并复位持锁状态。
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
            返回示例：
                None  # 异常事件已经写入本地运行库
        """
        # 序列化事件内容，没有事件时写入空对象。
        payload = serialize_value(event) if event is not None else {}

        # 写入异常事件记录。
        self.abnormal_event_repo.insert(
            time.time(),
            getattr(event, "machine_id", machine_id),
            getattr(event, "session_id", None),
            reason,
            json.dumps(payload, ensure_ascii=False),
        )
