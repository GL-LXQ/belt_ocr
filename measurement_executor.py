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
from machine_actor import MachineActor
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
        self.actors: dict[str, MachineActor] = {}
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
            self.actors[machine.machine_id] = MachineActor(
                machine, configuration, camera, frequency, self.ocr, self.storage,
                self.publish_event, self.state_changed,
            )
            self.actors[machine.machine_id].process_epoch = self.process_epoch

    async def start(self) -> None:
        """初始化存储并启动持续监听与处理任务。"""
        # 拒绝重复启动同一个执行器。
        if self.has_started:
            raise RuntimeError("请为新一次运行创建新的测量执行器。")
        try:
            # 初始化恢复库并创建证据图片目录。
            await run_blocking_operation(self.recovery.initialize)
            await run_blocking_operation(
                self.configuration.evidence_directory.mkdir,
                parents=True, exist_ok=True,
            )
            # 读取旧检查点并核对机器配置。
            checkpoints = await run_blocking_operation(self.recovery.load_checkpoints)
            if set(checkpoints) - set(self.actors):
                raise ValueError("配置缺少恢复记录中已有的机器。")
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
            for machine_id, actor in self.actors.items():
                actor.capacity_available = capacity_available
                # 读取本机检查点，无旧记录时设置模拟初始状态。
                checkpoint = checkpoints.get(machine_id, {})
                if not checkpoint:
                    checkpoint["waiting_cycle_reset"] = (
                        self.configuration.initial_machine_state != "CLOSED"
                    )
                    if self.configuration.initial_machine_state == "UNKNOWN":
                        checkpoint["device_faults"] = ["UNKNOWN_INITIAL_STATE"]
                # 恢复本机测量档案和运行状态。
                await actor.restore_measurements(checkpoint)
        except Exception:
            # 恢复失败时取消已创建的任务并等待结束。
            startup_tasks = [
                task for actor in self.actors.values()
                for task in (*actor.deadline_tasks.values(), *actor.background_tasks)
            ]
            for task in startup_tasks:
                task.cancel()
            await asyncio.gather(*startup_tasks, return_exceptions=True)
            # 释放恢复库并报告恢复失败。
            self.recovery.close()
            logger.exception("恢复测量记录失败")
            raise RuntimeError("恢复测量记录失败。") from None

        # 为每台机器安排事件处理和频率接收两个后台任务。
        for actor in self.actors.values():
            self.worker_tasks.append(asyncio.create_task(actor.run()))
            self.worker_tasks.append(asyncio.create_task(actor.frequency.run()))
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
        if machine_id not in self.actors:
            raise ValueError(f"未配置机器：{machine_id}")

        # 等待 Actor 确认本次启动或关闭，不等待测量结果。
        acknowledgement = asyncio.get_running_loop().create_future()
        await self.publish_event(MeasurementEvent(
            event_type, machine_id,
            session_id=(
                self.actors[machine_id].active_session_id
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
        actor = self.actors.get(event.machine_id)
        if actor is None:
            await run_blocking_operation(self.recovery.audit, "UNKNOWN_MACHINE", event)
            logger.warning("隔离未知机器事件 machine_id=%s", event.machine_id)
            return
        # 为内部来源补齐事件批次、序号和接收时间。
        if not event.source_id:
            source_id = "business"
            if event.event_type.startswith(("Frame", "Capture")):
                source_id = actor.machine.camera_id
            elif event.event_type.startswith("Frequency"):
                source_id = actor.machine.frequency_source_id
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
        await actor.queue.put(event)

    async def report_device_health(
        self, source_id: str, healthy: bool, machine_id: str | None = None
    ) -> None:
        """按设备绑定范围发送故障或恢复事件。"""
        if machine_id is not None and machine_id not in self.actors:
            raise ValueError(f"未配置机器：{machine_id}")
        targets = [
            actor.machine.machine_id for actor in self.actors.values()
            if machine_id == actor.machine.machine_id
            or (
                machine_id is None and source_id in {
                    "IO", "OCR", "STORAGE", actor.machine.camera_id,
                    actor.machine.frequency_source_id,
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
        """自动补交待提交记录并检查磁盘及积压容量。"""
        while True:
            try:
                await self.storage.enqueue_pending_records()
                capacity_available = await self.check_storage_capacity()
                for actor in self.actors.values():
                    if actor.capacity_available != capacity_available:
                        await self.publish_event(MeasurementEvent(
                            "CapacityChanged", actor.machine.machine_id,
                            payload=capacity_available,
                        ))
                self.recovery.available = True
            except Exception:
                self.recovery.available = False
                logger.exception("本地恢复库或容量检查失败，暂停接收新周期。")
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
            for actor in self.actors.values():
                await self.publish_event(MeasurementEvent(
                    "DeviceFault", actor.machine.machine_id, payload=component,
                ))
            await run_blocking_operation(
                self.recovery.audit, f"{component}_WORKER_EXITED",
            )
            if attempt + 1 < self.configuration.worker_restart_attempts:
                await asyncio.sleep(self.configuration.storage_retry_interval_ms / 1000)
                for actor in self.actors.values():
                    await self.publish_event(MeasurementEvent(
                        "DeviceRecovered", actor.machine.machine_id, payload=component,
                    ))
        if component == "OCR":
            self.ocr.accepting_jobs = False

    async def retry_pending_records(self) -> None:
        """重新提交进程内保留的失败记录。"""
        if not self.worker_tasks:
            raise RuntimeError("测量系统尚未启动。")
        for actor in self.actors.values():
            for session in tuple(actor.sessions.values()):
                if session.commit_state == "RETRY_PENDING":
                    await self.publish_event(MeasurementEvent(
                        "RetryCommit", actor.machine.machine_id, session.session_id,
                    ))

    async def wait_until_idle(self, timeout_seconds: float = 30) -> None:
        """等待全部已受理的测量记录获得保存确认。"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            # 检查当前状态，再等待下一次状态更新。
            self.state_changed.clear()
            if not any(actor.sessions for actor in self.actors.values()):
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
            for actor in self.actors.values():
                acknowledgement = asyncio.get_running_loop().create_future()
                await self.publish_event(MeasurementEvent(
                    "Shutdown", actor.machine.machine_id,
                    acknowledgement=acknowledgement,
                ))
                await acknowledgement

            # 等待记录提交和已经入队的业务事件。
            await self.wait_until_idle(self.configuration.shutdown_timeout_ms / 1000)
            await self.storage.queue.join()
            for actor in self.actors.values():
                await actor.queue.join()

        # 在统一退出期限内完成后台收尾。
        try:
            await asyncio.wait_for(
                drain_measurements(), self.configuration.shutdown_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            logger.warning("退出等待到期，未完成记录已保留在本地恢复库。")

        # 收集采集、在途测量和期限任务。
        background_tasks = []
        for actor in self.actors.values():
            background_tasks.extend(actor.camera.tasks)
            background_tasks.extend(actor.frequency.tasks)
            background_tasks.extend(actor.deadline_tasks.values())
            background_tasks.extend(actor.background_tasks)
            for window in actor.frequency.windows.values():
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
