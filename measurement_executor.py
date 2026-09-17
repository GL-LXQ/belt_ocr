"""组织多机测量的初始化、信号入口、结果等待和资源释放。"""

import asyncio
import logging
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from camera import FolderCamera
from configuration import MeasurementConfiguration
from frequency import SimulatedFrequency
from machine_manager import MachineManager
from models import MeasurementEvent
from recovery import run_blocking_operation, RecoveryStore
from ocr import SimulatedOCR
from storage import SQLiteWriter


logger = logging.getLogger(__name__)


class MeasurementExecutor:
    def __init__(self, configuration: MeasurementConfiguration) -> None:
        configuration.validate()
        self.configuration = configuration
        self.state_changed = asyncio.Event()
        self.recovery = RecoveryStore(configuration.recovery_path)
        self.storage = SQLiteWriter(configuration, self.publish_event, self.recovery)
        self.ocr = SimulatedOCR(configuration, self.publish_event)
        self.machine_managers: dict[str, MachineManager] = {}
        self.worker_tasks: list[asyncio.Task[None]] = []
        self.accepting_signals = False
        self.source_sequences: dict[tuple[str, str], int] = {}
        self.process_epoch = uuid4().hex
        self.has_started = False
        self.stopping = False
        self.releasing_resources = False

        # 为每台机器建立独立的采集器和业务处理器。
        for machine in configuration.machines:
            camera = FolderCamera(machine, configuration, self.publish_event)
            frequency = SimulatedFrequency(machine, configuration, self.publish_event)
            self.machine_managers[machine.machine_id] = MachineManager(
                machine, configuration, camera, frequency, self.ocr, self.storage,
                self.publish_event, self.state_changed,
            )
            self.machine_managers[machine.machine_id].process_epoch = self.process_epoch

    async def start(self) -> None:
        """初始化本次运行的机器状态和存储，启动监听与处理任务。

        Args:
            无外部参数。

        Returns:
            None: 完成启动并开放信号入口，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 拒绝重复启动同一个执行器。
        if self.has_started:
            raise RuntimeError("请为新一次运行创建新的测量执行器。")
        try:
            # 锁定并初始化恢复库，清理上次运行的待处理状态。
            await run_blocking_operation(self.recovery.initialize)

            # 创建本次运行使用的证据图片目录。
            await run_blocking_operation(
                self.configuration.evidence_directory.mkdir,
                parents=True, exist_ok=True,
            )
        except Exception:
            # 初始化失败时释放恢复库并报告错误。
            self.recovery.close()
            logger.exception("测量系统初始化失败")
            raise RuntimeError("测量系统初始化失败。") from None

        # 初始化最终结果库，失败时标记不可用并记录审计。
        try:
            await run_blocking_operation(self.storage.initialize)
        except Exception:
            self.storage.available = False
            await run_blocking_operation(
                self.recovery.audit, "DATABASE_UNAVAILABLE_AT_STARTUP",
            )
            logger.exception("最终结果库暂不可用，将使用本地待提交区。")

        try:
            # 检查磁盘和待提交积压，设置各机器的容量状态。
            capacity_available = await self.check_storage_capacity()
            for machine_manager in self.machine_managers.values():
                machine_manager.capacity_available = capacity_available

                # 按本次配置设置机器复位状态和初始故障。
                machine_manager.waiting_cycle_reset = self.configuration.initial_machine_state != "CLOSED"
                if self.configuration.initial_machine_state == "UNKNOWN":
                    machine_manager.device_faults.add("UNKNOWN_INITIAL_STATE")

                # 标记本机初始化完成并保存本次空档案状态。
                machine_manager.initialized = True
                await run_blocking_operation(self.recovery.checkpoint, machine_manager)
        except Exception:
            # 初始化机器状态失败时释放恢复库并报告错误。
            self.recovery.close()
            logger.exception("初始化机器状态失败")
            raise RuntimeError("初始化机器状态失败。") from None

        # 为每台机器安排事件处理和频率接收两个后台任务。
        for machine_manager in self.machine_managers.values():
            self.worker_tasks.append(asyncio.create_task(machine_manager.run()))
            self.worker_tasks.append(asyncio.create_task(machine_manager.frequency.run()))
        # 启动共享 OCR 任务并监控运行状态。
        self.worker_tasks.append(asyncio.create_task(
            self.supervise_worker("OCR", self.ocr.run), name="OCR",
        ))
        # 启动共享存储任务并监控运行状态。
        self.worker_tasks.append(asyncio.create_task(
            self.supervise_worker("STORAGE", self.storage.run), name="STORAGE",
        ))
        # 启动定期补交待提交记录和检查存储容量的任务。
        self.worker_tasks.append(asyncio.create_task(self.maintain_system()))
        # 标记启动完成，开放启动和关闭信号入口。
        self.has_started = True
        self.accepting_signals = True

    async def handle_start(self, machine_id: str) -> None:
        """处理某台皮带机启动，不依赖信号来源。"""
        await self.send_signal("MachineStarted", machine_id)

    async def handle_close(self, machine_id: str) -> None:
        """处理某台皮带机正常关闭，不等待后台识别和保存。"""
        await self.send_signal("MachineClosed", machine_id)

    async def send_signal(
        self, event_type: str, machine_id: str, payload=None
    ) -> None:
        """把入口信号送入机器队列并等待本次业务处理完成。"""
        if not self.accepting_signals:
            raise RuntimeError("测量系统当前未接收信号。")
        if machine_id not in self.machine_managers:
            raise ValueError(f"未配置机器：{machine_id}")

        # 等待 机器管理员 确认本次启动或关闭，不等待测量结果。
        acknowledgement = asyncio.get_running_loop().create_future()
        await self.publish_event(MeasurementEvent(
            event_type, machine_id,
            session_id=(
                self.machine_managers[machine_id].active_session_id
                if event_type == "MachineClosed" else None
            ),
            payload=payload, acknowledgement=acknowledgement,
        ))
        await acknowledgement

    async def publish_event(self, event: MeasurementEvent) -> None:
        """将事件路由到对应机器的有界队列。"""
        if self.releasing_resources:
            if event.acknowledgement is not None:
                event.acknowledgement.cancel()
            return
        machine_manager = self.machine_managers.get(event.machine_id)
        if machine_manager is None:
            await run_blocking_operation(self.recovery.audit, "UNKNOWN_MACHINE", event)
            logger.warning("隔离未知机器事件 machine_id=%s", event.machine_id)
            return
        # 为内部来源补齐事件批次、序号和接收时间。
        if not event.source_id:
            source_id = "business"
            if event.event_type.startswith(("Frame", "Capture")):
                source_id = machine_manager.machine.camera_id
            elif event.event_type.startswith("Frequency"):
                source_id = machine_manager.machine.frequency_source_id
            elif event.event_type.startswith("OCR"):
                source_id = "OCR"
            elif event.event_type.startswith("Commit"):
                source_id = "STORAGE"
            source_key = (event.machine_id, source_id)
            sequence = self.source_sequences.get(source_key, 0) + 1
            self.source_sequences[source_key] = sequence
            event = replace(
                event, source_id=source_id, source_epoch=self.process_epoch,
                source_sequence=sequence,
                received_at=datetime.now(timezone.utc).isoformat(),
            )
        await machine_manager.queue.put(event)

    async def report_device_health(
        self, source_id: str, healthy: bool, machine_id: str | None = None
    ) -> None:
        """按设备绑定范围发送故障或恢复事件。"""
        if machine_id is not None and machine_id not in self.machine_managers:
            raise ValueError(f"未配置机器：{machine_id}")
        targets = [
            machine_manager.machine.machine_id for machine_manager in self.machine_managers.values()
            if machine_id == machine_manager.machine.machine_id
            or (
                machine_id is None and source_id in {
                    "IO", "OCR", "STORAGE", machine_manager.machine.camera_id,
                    machine_manager.machine.frequency_source_id,
                }
            )
        ]
        if not targets:
            raise ValueError(f"未配置设备来源：{source_id}")
        for target in targets:
            await self.send_signal(
                "DeviceRecovered" if healthy else "DeviceFault", target, source_id,
            )

    async def synchronize_machine(self, machine_id: str, observed_state: str) -> None:
        """接收已确认的现场初始状态或重连状态。"""
        if observed_state not in {"CLOSED", "OPEN", "UNKNOWN"}:
            raise ValueError("机器状态必须是 CLOSED、OPEN 或 UNKNOWN。")
        await self.send_signal("MachineSynchronized", machine_id, observed_state)

    async def synchronize_source(
        self, machine_id: str, source_id: str, source_epoch: str,
        source_sequence: int = 0,
    ) -> None:
        """登记已确认的来源批次和序号基线。"""
        if not source_id or not source_epoch or source_sequence < 0:
            raise ValueError("来源、批次不能为空，序号不能为负数。")
        await self.send_signal("SourceSynchronized", machine_id, {
            "source_id": source_id, "epoch": source_epoch,
            "sequence": source_sequence,
        })

    async def maintain_system(self) -> None:
        """定期补交待提交记录，检查存储容量并通知机器管理员更新容量状态。

        Args:
            无外部参数。

        Returns:
            None: 持续运行直到任务被取消，无返回数据。
            返回值形式示例：
                None  # 无返回数据
        """
        while True:
            try:
                # 将已到期的待提交记录重新加入存储队列。
                await self.storage.enqueue_pending_records()

                # 检查待提交记录积压数量和磁盘剩余空间。
                capacity_available = await self.check_storage_capacity()

                # 向容量状态发生变化的机器管理员发送更新事件。
                for machine_manager in self.machine_managers.values():
                    if machine_manager.capacity_available != capacity_available:
                        await self.publish_event(
                            MeasurementEvent(
                                "CapacityChanged",
                                machine_manager.machine.machine_id,
                                payload=capacity_available,
                            )
                        )

                # 标记本地恢复库可用。
                self.recovery.available = True
            except Exception:
                # 标记本地恢复库不可用并记录维护异常。
                self.recovery.available = False
                logger.exception("本地恢复库或容量检查失败，暂停接收新周期。")

            # 等待配置的维护间隔，再开始下一轮处理。
            await asyncio.sleep(self.configuration.maintenance_interval_ms / 1000)

    async def check_storage_capacity(self) -> bool:
        """检查本地待提交数量和输出目录所在磁盘的剩余空间。"""
        pending_count = await run_blocking_operation(self.recovery.pending_count)
        output_paths = {
            self.configuration.recovery_path.parent,
            self.configuration.evidence_directory,
        }
        disk_states = await asyncio.gather(*(
            run_blocking_operation(shutil.disk_usage, path) for path in output_paths
        ))
        return (
            pending_count < self.configuration.max_persistent_records
            and all(
                disk_state.free >= self.configuration.minimum_free_disk_bytes
                for disk_state in disk_states
            )
        )

    async def supervise_worker(self, component: str, run_worker) -> None:
        """监督共享工作任务，有限重启异常退出的工作单元。"""
        for attempt in range(self.configuration.worker_restart_attempts):
            try:
                await run_worker()
                if self.stopping:
                    return
                raise RuntimeError("工作任务意外退出。")
            except asyncio.CancelledError:
                if self.stopping:
                    raise
                logger.error("工作任务意外取消 component=%s", component)
            except Exception:
                logger.exception("工作任务异常退出 component=%s", component)

            # 记录退出事件并按影响范围限制接收。
            for machine_manager in self.machine_managers.values():
                await self.publish_event(MeasurementEvent(
                    "DeviceFault", machine_manager.machine.machine_id, payload=component,
                ))
            await run_blocking_operation(
                self.recovery.audit, f"{component}_WORKER_EXITED",
            )
            if attempt + 1 < self.configuration.worker_restart_attempts:
                await asyncio.sleep(self.configuration.storage_retry_interval_ms / 1000)
                for machine_manager in self.machine_managers.values():
                    await self.publish_event(MeasurementEvent(
                        "DeviceRecovered", machine_manager.machine.machine_id, payload=component,
                    ))
        if component == "OCR":
            self.ocr.accepting_jobs = False

    async def retry_pending_records(self) -> None:
        """重新提交进程内保留的失败记录。"""
        if not self.worker_tasks:
            raise RuntimeError("测量系统尚未启动。")
        for machine_manager in self.machine_managers.values():
            for session in tuple(machine_manager.sessions.values()):
                if session.commit_state == "RETRY_PENDING":
                    await self.publish_event(MeasurementEvent(
                        "RetryCommit", machine_manager.machine.machine_id, session.session_id,
                    ))

    async def wait_until_idle(self, timeout_seconds: float = 30) -> None:
        """等待全部已受理的测量记录获得保存确认。"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            # 检查当前状态，再等待下一次状态更新。
            self.state_changed.clear()
            if not any(machine_manager.sessions for machine_manager in self.machine_managers.values()):
                return
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                raise asyncio.TimeoutError("测量结果尚未全部保存。")
            await asyncio.wait_for(self.state_changed.wait(), remaining_seconds)

    async def stop(self) -> None:
        """停止接收信号，中断活动周期并有限等待后台收尾。"""
        if not self.worker_tasks:
            return
        self.accepting_signals = False
        self.stopping = True

        async def drain_measurements() -> None:
            # 将尚未关闭的现场周期标记为中断。
            for machine_manager in self.machine_managers.values():
                acknowledgement = asyncio.get_running_loop().create_future()
                await self.publish_event(MeasurementEvent(
                    "Shutdown", machine_manager.machine.machine_id,
                    acknowledgement=acknowledgement,
                ))
                await acknowledgement

            # 等待记录提交和已经入队的业务事件。
            await self.wait_until_idle(self.configuration.shutdown_timeout_ms / 1000)
            await self.storage.queue.join()
            for machine_manager in self.machine_managers.values():
                await machine_manager.queue.join()

        # 在统一退出期限内完成后台收尾。
        try:
            await asyncio.wait_for(
                drain_measurements(), self.configuration.shutdown_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            logger.warning("退出等待到期，未完成记录已保留在本地恢复库。")

        # 收集采集、在途测量和期限任务。
        background_tasks = []
        for machine_manager in self.machine_managers.values():
            background_tasks.extend(machine_manager.camera.tasks)
            background_tasks.extend(machine_manager.frequency.tasks)
            background_tasks.extend(machine_manager.deadline_tasks.values())
            background_tasks.extend(machine_manager.background_tasks)
            for window in machine_manager.frequency.windows.values():
                background_tasks.extend(window.pending_deliveries)

        # 取消剩余后台工作并释放持续任务。
        self.releasing_resources = True
        self.ocr.stopping = True
        all_tasks = background_tasks + self.worker_tasks
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self.worker_tasks.clear()
        self.recovery.close()
