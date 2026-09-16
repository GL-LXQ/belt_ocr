"""组织多机测量的初始化、信号入口、结果等待和资源释放。"""

import asyncio
import logging

from camera import FolderCamera
from configuration import MeasurementConfiguration
from frequency import SimulatedFrequency
from machine_actor import MachineActor
from models import MeasurementEvent
from ocr import SimulatedOCR
from storage import SQLiteWriter


logger = logging.getLogger(__name__)


class MeasurementExecutor:
    def __init__(self, configuration: MeasurementConfiguration) -> None:
        configuration.validate()
        self.configuration = configuration
        self.state_changed = asyncio.Event()
        self.storage = SQLiteWriter(configuration, self.publish_event)
        self.ocr = SimulatedOCR(configuration, self.publish_event)
        self.actors: dict[str, MachineActor] = {}
        self.worker_tasks: list[asyncio.Task[None]] = []
        self.accepting_signals = False

        # 为每台机器建立独立的采集器和业务处理器。
        for machine in configuration.machines:
            camera = FolderCamera(machine, configuration, self.publish_event)
            frequency = SimulatedFrequency(machine, configuration, self.publish_event)
            self.actors[machine.machine_id] = MachineActor(
                machine, configuration, camera, frequency, self.ocr, self.storage,
                self.publish_event, self.state_changed,
            )

    async def start(self) -> None:
        """初始化存储并启动持续监听与处理任务。"""
        if self.worker_tasks:
            raise RuntimeError("测量执行器已经启动。")
        try:
            await asyncio.to_thread(self.storage.initialize)
            await asyncio.to_thread(
                self.configuration.evidence_directory.mkdir,
                parents=True, exist_ok=True,
            )
        except Exception:
            logger.exception("测量系统初始化失败")
            raise RuntimeError("测量系统初始化失败。") from None

        # 启动各机器事件处理和持续频率接收。
        for actor in self.actors.values():
            self.worker_tasks.append(asyncio.create_task(actor.run()))
            self.worker_tasks.append(asyncio.create_task(actor.frequency.run()))
        self.worker_tasks.append(asyncio.create_task(self.ocr.run()))
        self.worker_tasks.append(asyncio.create_task(self.storage.run()))
        self.accepting_signals = True

    async def handle_start(self, machine_id: str) -> None:
        """处理某台皮带机启动，不依赖信号来源。"""
        await self.send_signal("MachineStarted", machine_id)

    async def handle_close(self, machine_id: str) -> None:
        """处理某台皮带机正常关闭，不等待后台识别和保存。"""
        await self.send_signal("MachineClosed", machine_id)

    async def send_signal(self, event_type: str, machine_id: str) -> None:
        """把入口信号送入机器队列并等待本次业务处理完成。"""
        if not self.accepting_signals:
            raise RuntimeError("测量系统当前未接收信号。")
        if machine_id not in self.actors:
            raise ValueError(f"未配置机器：{machine_id}")

        # 等待 Actor 确认本次启动或关闭，不等待测量结果。
        acknowledgement = asyncio.get_running_loop().create_future()
        await self.publish_event(MeasurementEvent(
            event_type, machine_id, acknowledgement=acknowledgement,
        ))
        await acknowledgement

    async def publish_event(self, event: MeasurementEvent) -> None:
        """将事件路由到对应机器的有界队列。"""
        actor = self.actors.get(event.machine_id)
        if actor is None:
            logger.warning("隔离未知机器事件 machine_id=%s", event.machine_id)
            return
        await actor.queue.put(event)

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
            logger.error("退出等待超时，仍有未保存记录。当前版本不支持崩溃恢复。")

        # 收集采集、在途测量和期限任务。
        background_tasks = []
        for actor in self.actors.values():
            background_tasks.extend(actor.camera.tasks)
            background_tasks.extend(actor.frequency.tasks)
            background_tasks.extend(actor.deadline_tasks.values())
            for window in actor.frequency.windows.values():
                background_tasks.extend(window.pending_deliveries)

        # 取消剩余后台工作并释放持续任务。
        all_tasks = background_tasks + self.worker_tasks
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self.worker_tasks.clear()
