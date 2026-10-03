"""独立于 HTTP 连接管理监测线程、生命周期和有界状态快照。"""

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading

from async_utils import run_blocking_operation
from config_util import load_config
from runtime.hardware_lock import HardwareOwnershipError, HardwareOwnershipLock
from runtime.system_runtime import SystemRuntime
from src.controller.result import Result


logger = logging.getLogger(__name__)


class RuntimeHost:
    """持有唯一监测任务，向客户端提供可完整替换的状态快照。"""

    def __init__(
        self,
        configuration_directory: Path,
        machine_reader: Callable[[], dict],
        ocr_config_path: Path | None = None,
        runtime_factory: Callable = SystemRuntime,
        configuration_loader: Callable = load_config,
        max_clients: int = 16,
        queue_capacity: int = 8,
    ) -> None:
        """保存启动依赖并建立命令锁和客户端容量限制。

        Args:
            configuration_directory: 公共 YAML 配置目录。
            machine_reader: 返回机器列表的业务查询函数。
            ocr_config_path: 可选的独立 OCR 配置路径。
            runtime_factory: 创建 SystemRuntime 的工厂，可在测试中替换。
            configuration_loader: 配置读取函数，可在测试中替换。
            max_clients: 同时连接的 SSE 客户端上限。
            queue_capacity: 每个客户端最多等待发送的完整快照数。

        Returns:
            None  # 监测尚未启动，未连接现场硬件
        """
        self.configuration_directory = configuration_directory
        self.machine_reader = machine_reader
        self.ocr_config_path = ocr_config_path
        self.runtime_factory = runtime_factory
        self.configuration_loader = configuration_loader
        self.command_lock = asyncio.Lock()
        self.stop_requested = threading.Event()
        self.task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

        # 状态和订阅队列只在 API 事件循环中修改。
        self.status = "stopped"
        self.failure = ""
        self.started_at: str | None = None
        self.sequence = 0
        self.machine_states: list[dict] = []
        self.sessions: list[dict] = []
        self.clients: set[asyncio.Queue] = set()
        self.max_clients = max_clients
        self.queue_capacity = queue_capacity
        self.closing = False
        self.cleanup_failed = False
        self.retained_lock: HardwareOwnershipLock | None = None

    @property
    def is_active(self) -> bool:
        """判断机器和配置写入是否仍需保持锁定。

        Args:
            无外部参数。

        Returns:
            True  # 启动、运行、清理或清理失败时仍禁止配置写入
        """
        return self.status in {"starting", "running", "stopping"} or self.cleanup_failed

    def snapshot(self) -> dict:
        """复制当前完整机器和周期快照供查询或重连恢复。

        Args:
            无外部参数。

        Returns:
            {
                "status": "stopped",  # 宿主生命周期
                "running": False,  # 是否占用监测资源
                "failure": "",  # 最近故障
                "started_at": None,  # 本次启动时间
                "sequence": 0,  # 单调快照编号
                "machines": [],  # 全部机器的运行状态
                "sessions": [],  # 全部活动周期和有界最近周期
            }
        """
        return deepcopy({
            "status": self.status,
            "running": self.is_active,
            "failure": self.failure,
            "started_at": self.started_at,
            "sequence": self.sequence,
            "machines": self.machine_states,
            "sessions": self.sessions,
        })

    def publish_snapshot(self) -> None:
        """发布完整快照，慢客户端积压时丢弃旧快照并保留最新状态。

        Args:
            无外部参数。

        Returns:
            None  # 客户端收到的每一条都是完整可恢复状态
        """
        self.sequence += 1
        snapshot = self.snapshot()
        for queue in self.clients:
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
            queue.put_nowait(snapshot)

    def subscribe(self) -> asyncio.Queue:
        """登记一个有界客户端，并立即放入当前完整快照。

        Args:
            无外部参数。

        Returns:
            asyncio.Queue()  # 首项是完整快照，连接过多时抛出 RuntimeError
        """
        if self.closing or len(self.clients) >= self.max_clients:
            raise RuntimeError("实时连接已达上限或服务正在退出，请稍后重试。")
        queue = asyncio.Queue(maxsize=self.queue_capacity)
        self.clients.add(queue)
        queue.put_nowait(self.snapshot())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """移除客户端，不改变后台监测任务。

        Args:
            queue: 需要移除的客户端队列。

        Returns:
            None  # 监测继续运行，客户端队列已解除引用
        """
        self.clients.discard(queue)

    async def refresh_machines(self) -> None:
        """在未运行时刷新机器基线，保留一致的展示字段。

        Args:
            无外部参数。

        Returns:
            None  # 机器配置已读入快照
        """
        rows = (await asyncio.to_thread(self.machine_reader))["machines"]
        self.machine_states = self.prepare_machine_states(rows)
        self.publish_snapshot()

    @staticmethod
    def prepare_machine_states(rows: list[dict]) -> list[dict]:
        """把机器配置转换为完整的初始展示状态。

        Args:
            rows: 业务服务读取的全部机器配置。

        Returns:
            list[dict]  # 每台机器包含身份、连接、周期占用和提示字段
        """
        return [dict(
            row,
            id=str(row["id"]),
            camera_state="未连接",
            camera_error="",
            status="offline",
            warning="",
            active_session_id=None,
            waiting_cycle_reset=False,
            inflight_count=0,
        ) for row in rows]

    async def start(self) -> Result:
        """原子登记一次启动请求，重复请求复用已有监测。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 已受理或已经启动
                data={},  # 当前完整运行快照
                message="",  # 说明
            )
        """
        async with self.command_lock:
            if self.closing or self.status == "stopping" or self.cleanup_failed:
                return Result.error(
                    "监测仍在清理或服务正在退出，请等待完全停止。",
                    self.snapshot(),
                )
            if self.status in {"starting", "running"}:
                return Result.ok(self.snapshot())

            # 在任何异步准备前锁定机器和配置写入。
            self.status = "starting"
            self.failure = ""
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.sessions = []
            self.stop_requested.clear()
            self.loop = asyncio.get_running_loop()
            self.publish_snapshot()
            self.task = asyncio.create_task(self.run_monitoring(), name="beltvision-monitoring")
            self.task.add_done_callback(self.recover_cancelled_start)
            return Result.ok(self.snapshot())

    def recover_cancelled_start(self, task: asyncio.Task) -> None:
        """清理首次运行前即被取消的启动任务。

        Args:
            task: 已经结束的监测入口任务。

        Returns:
            None  # 尚未创建工作线程的取消请求恢复为停止状态
        """
        if task.cancelled() and self.task is task:
            self.task = None
            self.status = "stopped"
            self.publish_snapshot()

    async def stop(self) -> Result:
        """原子提交停止请求，在资源真正释放前继续禁止配置写入。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 停止请求已受理
                data={},  # 当前完整运行快照
                message="",  # 说明
            )
        """
        async with self.command_lock:
            if self.task is not None:
                self.stop_requested.set()
                self.status = "stopping"
                self.publish_snapshot()
            return Result.ok(self.snapshot())

    async def run_mutation(self, operation: Callable[[], Result]) -> Result:
        """串行执行机器或配置写入，并防止其他进程同时启动设备。

        Args:
            operation: 已绑定参数的同步业务写入。

        Returns:
            Result(
                success=True,  # 业务写入成功
                data=None,  # 原业务结果数据
                message="",  # 原业务提示
            )
        """
        async with self.command_lock:
            if self.is_active or self.closing:
                return Result.error(
                    "监测启动、运行或停止清理中，请等待完全停止后再修改配置。",
                )

            # 请求断开也要等写入结束后才释放命令锁。
            result = await run_blocking_operation(self.perform_locked_mutation, operation)
            if result.success:
                await self.refresh_machines()
            return result

    def perform_locked_mutation(self, operation: Callable[[], Result]) -> Result:
        """在跨进程设备锁内执行一次业务写入。

        Args:
            operation: 已绑定参数的同步业务写入。

        Returns:
            Result(
                success=False,  # 设备所有权冲突
                data=None,  # 无附加数据
                message="其他监测实例正在占用设备。",  # 可读提示
            )
        """
        try:
            with HardwareOwnershipLock():
                return operation()
        except HardwareOwnershipError as error:
            return Result.error(str(error))

    async def run_monitoring(self) -> None:
        """在独立线程完成监测启动、持续运行和资源释放。

        Args:
            无外部参数。

        Returns:
            None  # 监测已终止，结果写入最终快照
        """
        worker = asyncio.get_running_loop().run_in_executor(None, self.run_runtime_thread)
        try:
            while True:
                try:
                    await asyncio.shield(worker)
                    break
                except asyncio.CancelledError:
                    if worker.cancelled():
                        raise
                    self.stop_requested.set()
                    self.status = "stopping"
                    self.publish_snapshot()
        except Exception as error:
            logger.exception("监测宿主运行失败")
            self.failure = str(error)
        finally:
            self.status = "failed" if self.failure else "stopped"
            for machine in self.machine_states:
                machine.update(status="offline", active_session_id=None, inflight_count=0)
            self.task = None
            self.publish_snapshot()

    def run_runtime_thread(self) -> None:
        """创建专用事件循环，使同步 SDK 启动不会阻塞 HTTP 服务。

        Args:
            无外部参数。

        Returns:
            None  # 专用事件循环已完成清理
        """
        asyncio.run(self.run_runtime())

    async def run_runtime(self) -> None:
        """独占设备后读取配置，复用 SystemRuntime 主流程并持续收集状态。

        Args:
            无外部参数。

        Returns:
            None  # 正常停止；启动或运行故障继续传给宿主
        """
        ownership = HardwareOwnershipLock()
        ownership.acquire()
        runtime = None
        session_states: OrderedDict[str, dict] = OrderedDict()
        try:
            rows = self.machine_reader()["machines"]
            machine_states = {machine["id"]: machine for machine in self.prepare_machine_states(rows)}
            self.loop.call_soon_threadsafe(self.receive_runtime_snapshot, deepcopy(list(machine_states.values())), [])
            configuration = self.configuration_loader(self.configuration_directory)
            runtime = self.runtime_factory(
                configuration,
                hardware_lock=ownership,
                ocr_config_path=self.ocr_config_path,
            )
            callbacks = self.create_callbacks(machine_states, session_states, runtime)
            await runtime.start(*callbacks)
            self.loop.call_soon_threadsafe(self.mark_running)

            # 周期状态由运行线程读取，避免跨线程读取活动周期容器。
            while not self.stop_requested.is_set() and runtime.failure is None:
                self.collect_runtime_snapshot(runtime, machine_states, session_states)
                await asyncio.sleep(0.1)
        finally:
            if runtime is not None:
                try:
                    await runtime.stop()
                    if runtime.cleanup_failed:
                        raise RuntimeError("设备资源清理失败，请退出后端进程后检查设备连接。")
                except BaseException:
                    self.cleanup_failed = True
                    self.retained_lock = ownership
                    raise
            ownership.release()
        if runtime is not None:
            self.collect_runtime_snapshot(runtime, machine_states, session_states)
        if runtime is not None and runtime.failure is not None:
            raise runtime.failure

    def mark_running(self) -> None:
        """启动完成后进入运行状态，保留已经提交的停止请求。

        Args:
            无外部参数。

        Returns:
            None  # 宿主状态与停止请求保持一致
        """
        if self.status == "starting":
            self.status = "running"
            self.publish_snapshot()

    def create_callbacks(self, machines: dict, sessions: OrderedDict, runtime) -> tuple:
        """创建纯 Python 运行回调，周期通知始终按 Session ID 归档。

        Args:
            machines: 运行线程拥有的机器状态字典。
            sessions: 运行线程拥有的周期状态字典。
            runtime: 当前系统运行时。

        Returns:
            tuple()  # 按 SystemRuntime.start 参数顺序排列的七个回调
        """
        def camera(machine_id, state, reason):
            """保存相机连接状态。

            Args:
                machine_id: 机器编号。
                state: 连接状态。
                reason: 失败原因。

            Returns:
                None  # 机器连接状态已更新
            """
            machines[machine_id].update(camera_state=state, camera_error=reason)
            self.collect_runtime_snapshot(runtime, machines, sessions)

        def progress(machine_id, session_id, stage, status):
            """按周期保存阶段变化。

            Args:
                machine_id: 机器编号。
                session_id: 周期编号。
                stage: 阶段枚举。
                status: 阶段状态枚举。

            Returns:
                None  # 指定周期的阶段已更新
            """
            session = self.ensure_session(sessions, machine_id, session_id)
            session["stages"][getattr(stage, "value", stage)] = getattr(status, "value", status)
            self.collect_runtime_snapshot(runtime, machines, sessions)

        def ocr(machine_id, session_id, lines):
            """按周期保存识别文字。

            Args:
                machine_id: 机器编号。
                session_id: 周期编号。
                lines: 正式识别文字。

            Returns:
                None  # 指定周期文字已更新
            """
            self.ensure_session(sessions, machine_id, session_id)["recognized_lines"] = list(lines)
            self.collect_runtime_snapshot(runtime, machines, sessions)

        def closed(machine_id, session_id):
            """仅关闭对应现场周期，不清除其他周期状态。

            Args:
                machine_id: 机器编号。
                session_id: 已关闭周期编号。

            Returns:
                None  # 指定周期已标记关闭
            """
            self.ensure_session(sessions, machine_id, session_id)["cycle_closed"] = True
            self.collect_runtime_snapshot(runtime, machines, sessions)

        def status(machine_id, state):
            """保存机器整体状态。

            Args:
                machine_id: 机器编号。
                state: online、offline 或 fault。

            Returns:
                None  # 机器整体状态已更新
            """
            machines[machine_id]["status"] = state
            self.collect_runtime_snapshot(runtime, machines, sessions)

        def warning(machine_id, message):
            """保存机器积压提示。

            Args:
                machine_id: 机器编号。
                message: 提示文字。

            Returns:
                None  # 机器提示已更新
            """
            machines[machine_id]["warning"] = message
            self.collect_runtime_snapshot(runtime, machines, sessions)

        def finished(session):
            """在周期回收时保存完整终态并立即发布。

            Args:
                session: 已结束且已经释放上下文的业务周期。

            Returns:
                None  # 终态错误、结束时间和频率已完整保留
            """
            display = self.ensure_session(sessions, session.machine_id, session.session_id)
            self.update_session_state(display, session)
            self.collect_runtime_snapshot(runtime, machines, sessions)

        return (camera, progress, ocr, closed, status, warning, finished)

    @staticmethod
    def ensure_session(sessions: OrderedDict, machine_id: str, session_id: str) -> dict:
        """为首次出现的周期建立完整展示字段。

        Args:
            sessions: 当前运行的周期字典。
            machine_id: 周期所属机器编号。
            session_id: 周期编号。

        Returns:
            {
                "machine_id": "1",  # 所属机器
                "session_id": "cycle-1",  # 周期编号
                "state": "RUNNING",  # 周期状态
                "cycle_closed": False,  # 现场周期是否关闭
                "stages": {},  # 各阶段状态
                "recognized_lines": [],  # 识别文字
                "final_frequency_hz": None,  # 最终频率
                "start_time": None,  # 开始时间
                "finish_time": None,  # 结束时间
                "errors": [],  # 周期错误
            }
        """
        if session_id not in sessions:
            sessions[session_id] = {
                "machine_id": machine_id,
                "session_id": session_id,
                "state": "RUNNING",
                "cycle_closed": False,
                "stages": {},
                "recognized_lines": [],
                "final_frequency_hz": None,
                "start_time": None,
                "finish_time": None,
                "errors": [],
            }
        return sessions[session_id]

    @staticmethod
    def update_session_state(display: dict, session) -> None:
        """复制活动周期或终态周期的纯业务字段。

        Args:
            display: 按周期编号维护的展示状态。
            session: 运行时业务周期对象。

        Returns:
            None  # 状态、起止时间、错误和频率已更新
        """
        display.update(
            state=session.state.value,
            start_time=session.start_time,
            finish_time=session.finish_time,
            errors=list(session.errors),
            final_frequency_hz=(session.final_frequency.value_hz if session.final_frequency else None),
        )

    def collect_runtime_snapshot(self, runtime, machines: dict, sessions: OrderedDict) -> None:
        """读取全部活动周期，并仅保留有界的最近结束周期。

        Args:
            runtime: 当前 SystemRuntime。
            machines: 运行线程的机器展示状态。
            sessions: 运行线程的周期展示状态。

        Returns:
            None  # 完整状态已投递给 API 事件循环
        """
        active_ids = set()
        for machine_id, machine in runtime.machines.items():
            machines[machine_id].update(
                active_session_id=machine.active_session_id,
                waiting_cycle_reset=machine.waiting_cycle_reset,
                inflight_count=len(machine.cycles),
            )
            for session_id, cycle in machine.cycles.items():
                active_ids.add(session_id)
                session = cycle.session
                display = self.ensure_session(sessions, machine_id, session_id)
                self.update_session_state(display, session)

        # 已回收周期保留阶段终态，防止晚到的旧周期覆盖新周期。
        for session_id, session in sessions.items():
            if session_id not in active_ids:
                storage = session["stages"].get("evidence_storage")
                if storage == "success":
                    session["state"] = "COMMITTED"
                elif session["state"] in {"RUNNING", "SAVING_RESULT"}:
                    session["state"] = "FAILED"
        retired = [session_id for session_id in sessions if session_id not in active_ids]
        for session_id in retired[:-100]:
            del sessions[session_id]
        self.loop.call_soon_threadsafe(
            self.receive_runtime_snapshot,
            deepcopy(list(machines.values())),
            deepcopy(list(sessions.values())),
        )

    def receive_runtime_snapshot(self, machines: list[dict], sessions: list[dict]) -> None:
        """在 API 事件循环中发布变化后的完整机器与周期快照。

        Args:
            machines: 所有机器的展示状态。
            sessions: 当前完整周期展示状态。

        Returns:
            None  # 未变化时不增加消息数量
        """
        if self.machine_states == machines and self.sessions == sessions:
            return

        # 保存更新前每台机器的活动周期编号。
        previous_session_ids = {
            machine["id"]: machine["active_session_id"]
            for machine in self.machine_states
        }

        # 更新机器与周期状态并发布完整快照。
        self.machine_states = machines
        self.sessions = sessions
        self.publish_snapshot()

        # 只在新周期首次进入快照时记录发布位置。
        for machine in machines:
            session_id = machine["active_session_id"]
            if session_id and previous_session_ids.get(machine["id"]) != session_id:
                logger.info(
                    "%s 已发布测量启动状态 trace=MEASUREMENT_STATE_PUBLISHED "
                    "machine_id=%s session_id=%s sequence=%s client_count=%s "
                    "published_at=%s",
                    machine["machine_name"],
                    machine["id"],
                    session_id,
                    self.sequence,
                    len(self.clients),
                    datetime.now(timezone.utc).isoformat(),
                )

    async def shutdown(self) -> None:
        """停止监测并等待资源释放，然后结束全部 SSE 连接。

        Args:
            无外部参数。

        Returns:
            None  # 正常清理完成后才允许服务进程退出
        """
        self.closing = True
        await self.stop()
        task = self.task
        if task is not None:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            self.recover_cancelled_start(task)
        for queue in self.clients:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
