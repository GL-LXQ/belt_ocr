"""组织多机测量的初始化、信号入口、结果等待和资源释放。"""

import asyncio
import logging
import shutil
from dataclasses import replace
from datetime import datetime, timezone

from camera import SessionCamera
from mvs_sdk import load_mvs_sdk
from configuration import MeasurementConfiguration
from frequency_adapter import FrequencyAdapter
from machine_manager import MachineManager
from enums import MachineState, SessionState
from models import MeasurementEvent
from recovery import run_blocking_operation, RecoveryStore
from text_recognition import TextRecognizer
from database import Database


logger = logging.getLogger(__name__)


class App:
    def __init__(self, configuration: MeasurementConfiguration) -> None:
        configuration.validate()
        self.configuration = configuration
        self.state_changed = asyncio.Event()
        self.recovery = RecoveryStore(configuration.recovery_path)
        self.database = Database(configuration, self.publish_event, self.recovery)
        self.text_recognizer = TextRecognizer(configuration)
        self.machine_managers: dict[str, MachineManager] = {}
        self.worker_tasks: list[asyncio.Task[None]] = []
        self.accepting_signals = False
        self.has_started = False
        self.stopping = False
        self.releasing_resources = False
        self.camera_sdk = None

        # 为每台机器建立独立的采集器和业务处理器。
        for machine in configuration.machines:
            camera = SessionCamera(machine, configuration, self.publish_event)
            frequency_adapter = FrequencyAdapter(machine, configuration, self.publish_event)
            self.machine_managers[machine.machine_id] = MachineManager(
                machine,
                configuration,
                camera,
                frequency_adapter,
                self.text_recognizer,
                self.database,
                self.publish_event,
                self.state_changed,
            )

    async def start(self) -> None:
        """初始化本次运行的机器状态和存储，启动监听与处理任务。

        Args:
            无外部参数。

        Returns:
            None: 完成启动并开放信号入口，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 拒绝重复启动同一个应用实例。
        if self.has_started:
            raise RuntimeError("请为新一次运行创建新的测量应用实例。")
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
            await run_blocking_operation(self.database.initialize)
        except Exception:
            self.database.available = False
            await run_blocking_operation(
                self.recovery.audit, "DATABASE_UNAVAILABLE_AT_STARTUP",
            )
            logger.exception("最终结果库暂不可用，正常结果提交时重新尝试初始化。")

        # 加载共享 MVS SDK，失败时保留应用并禁止相机测量。
        try:
            self.camera_sdk = await run_blocking_operation(
                load_mvs_sdk,
                self.configuration.mvs_development_directory,
                self.configuration.mvs_dll_directory,
            )
        except Exception:
            logger.exception("MVS SDK 加载失败，相机测量不可用。")

        # 按真实序列号打开各相机，分别登记不可用设备。
        for machine_manager in self.machine_managers.values():
            machine = machine_manager.machine
            try:
                if self.camera_sdk is None or not machine.camera_serial:
                    raise RuntimeError("SDK 不可用或尚未配置 camera_serial。")
                machine_manager.camera.device = await run_blocking_operation(
                    self.camera_sdk.open_camera,
                    machine.camera_serial,
                    pixel_format=machine.camera_pixel_format,
                    exposure_time_us=machine.camera_exposure_time_us,
                    gain=machine.camera_gain,
                )
            except Exception:
                machine_manager.device_faults.add(machine.camera_id)
                await run_blocking_operation(
                    self.recovery.audit, "CAMERA_UNAVAILABLE", machine_id=machine.machine_id,
                )
                logger.exception("相机不可用 machine_id=%s serial=%s", machine.machine_id, machine.camera_serial)

        try:
            # 检查磁盘剩余空间，设置各机器的初始容量状态。
            disk_capacity_available = await self.check_disk_capacity()
            for machine_manager in self.machine_managers.values():
                machine_manager.capacity_available = disk_capacity_available

                # 判断本机器初始状态是否为CLOSED；未关闭或状态未知时，等待本轮关闭后再接收新启动信号。
                machine_manager.waiting_cycle_reset = self.configuration.initial_machine_state != MachineState.CLOSED
                if self.configuration.initial_machine_state == MachineState.UNKNOWN:
                    # 初始状态未知时，登记“机器初始状态未知”故障。
                    machine_manager.device_faults.add("UNKNOWN_INITIAL_STATE")

                # 标记本机器初始化完成。
                machine_manager.initialized = True
        except Exception:
            # 初始化机器状态失败时释放恢复库并报告错误。
            if self.camera_sdk is not None:
                await run_blocking_operation(self.camera_sdk.close)
            self.recovery.close()
            logger.exception("初始化机器状态失败")
            raise RuntimeError("初始化机器状态失败。") from None

        # 启动每台机器的 监听任务 和 频率采集器。
        for machine_manager in self.machine_managers.values():
            self.worker_tasks.append(asyncio.create_task(machine_manager.listen_events()))
            self.worker_tasks.append(asyncio.create_task(machine_manager.frequency_adapter.run()))

        # 启动共享存储任务并监控运行状态。
        self.worker_tasks.append(asyncio.create_task(
            self.supervise_worker("STORAGE", self.database.run), name="STORAGE",
        ))

        # 启动定期检查存储容量的任务。
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

    async def send_signal(self, event_type: str, machine_id: str, payload=None) -> None:
        """把入口信号送入对应机器队列，并等待本次事件处理回执。

        Args:
            event_type: 事件类型，例如 MachineStarted 或 MachineClosed。
            machine_id: 接收信号的机器编号，例如 M01。
            payload: 随事件传递的业务数据，默认值为 None。

        Returns:
            None: 本次事件处理完成，无返回数据，不等待后台 OCR 和结果保存。
            返回示例：
                None  # 无返回数据
        """
        # 检查信号入口是否开放，以及机器是否已配置。
        if not self.accepting_signals:
            raise RuntimeError("测量系统当前未接收信号。")
        if machine_id not in self.machine_managers:
            raise ValueError(f"未配置机器：{machine_id}")

        # 创建本次事件的处理回执。
        acknowledgement = asyncio.get_running_loop().create_future()

        # 将信号和回执送入机器队列，关闭事件携带当前活动 Session 编号。
        await self.publish_event(
            MeasurementEvent(
                event_type,
                machine_id,
                session_id=(
                    self.machine_managers[machine_id].active_session_id
                    if event_type == "MachineClosed" else None
                ),
                payload=payload,
                acknowledgement=acknowledgement,
            )
        )

        # 等待机器管理员确认本次事件处理完成。
        await acknowledgement

    async def publish_event(self, event: MeasurementEvent) -> None:
        """更新事件接收时间，并将事件送入对应机器的有界队列。

        Args:
            event: 待分发的测量事件，包含事件类型、机器编号及相关业务数据。

        Returns:
            None: 事件入队或提前结束分发，无返回数据，不等待机器管理员处理。
            返回示例：
                None  # 无返回数据
        """
        # 释放资源期间取消事件回执，结束本次分发。
        if self.releasing_resources:
            if event.acknowledgement is not None:
                event.acknowledgement.cancel()
            return

        # 查找对应机器管理员，登记并隔离未知机器的事件。
        machine_manager = self.machine_managers.get(event.machine_id)
        if machine_manager is None:
            await run_blocking_operation(self.recovery.audit, "UNKNOWN_MACHINE", event)
            logger.warning("隔离未知机器事件 machine_id=%s", event.machine_id)
            return

        # 更新本次路由接收时间。
        event = replace(event, received_at=datetime.now(timezone.utc).isoformat())

        # 将事件放入对应机器队列，队列满时等待空位。
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

    async def synchronize_machine(self, machine_id: str, observed_state: MachineState | str) -> None:
        """将现场状态转换为枚举并发送机器同步事件。

        Args:
            machine_id: 需要同步状态的机器编号。
            observed_state: 已确认的现场状态，支持 MachineState 或对应字符串。

        Returns:
            None: 等待机器管理员处理同步事件，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 校验现场状态并转换为机器状态枚举。
        if observed_state not in set(MachineState):
            raise ValueError("机器状态必须是 CLOSED、OPEN 或 UNKNOWN。")
        machine_state = MachineState(observed_state)

        # 发送状态同步事件并等待处理完成。
        await self.send_signal("MachineSynchronized", machine_id, machine_state)

    async def maintain_system(self) -> None:
        """定期检查存储容量并通知机器管理员更新容量状态。

        Args:
            无外部参数。

        Returns:
            None: 持续运行直到任务被取消，无返回数据。
            返回值形式示例：
                None  # 无返回数据
        """
        while True:
            try:
                # 检查磁盘剩余空间。
                disk_capacity_available = await self.check_disk_capacity()

                # 读取正在排队或写入的数量，合并磁盘和积压检查结果。
                pending_count = len(self.database.queued_records)
                capacity_available = (
                    disk_capacity_available
                    and pending_count < self.configuration.max_persistent_records
                )

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

    async def check_disk_capacity(self) -> bool:
        """检查本地运行库和证据目录所在磁盘的剩余空间。

        Args:
            无外部参数。

        Returns:
            bool: 所有检查目录所在磁盘均达到最低剩余空间时返回 True。
            返回示例：
                True  # 所有检查目录所在磁盘空间充足
                False  # 至少一个检查目录所在磁盘空间不足
        """
        # 收集本地运行库和证据目录的路径。
        output_paths = {
            self.configuration.recovery_path.parent,
            self.configuration.evidence_directory,
        }
        # 在线程中读取各目录所在磁盘的容量信息。
        disk_states = await asyncio.gather(*(
            run_blocking_operation(shutil.disk_usage, path) for path in output_paths
        ))
        # 检查所有磁盘的剩余空间是否达到配置下限。
        return all(
            disk_state.free >= self.configuration.minimum_free_disk_bytes
            for disk_state in disk_states
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

    async def wait_until_idle(self, timeout_seconds: float = 30) -> None:
        """等待全部已受理的测量完成入库或失败清理，并结束现场周期。"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            # 检查当前状态，再等待下一次状态更新。
            self.state_changed.clear()
            if not any(machine_manager.sessions for machine_manager in self.machine_managers.values()):
                return
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                raise asyncio.TimeoutError("测量任务或现场周期尚未全部结束。")
            await asyncio.wait_for(self.state_changed.wait(), remaining_seconds)

    async def stop(self) -> None:
        """停止接收信号，中断活动周期并有限等待后台收尾。"""
        if not self.worker_tasks:
            return
        self.accepting_signals = False
        self.stopping = True

        # 停止接收新的 OCR 图片批次。
        self.text_recognizer.accepting_batches = False

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
            await self.database.queue.join()
            for machine_manager in self.machine_managers.values():
                await machine_manager.queue.join()

        # 在统一退出期限内完成后台收尾。
        try:
            await asyncio.wait_for(
                drain_measurements(), self.configuration.shutdown_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            logger.warning("退出等待到期，未完成测量将标记失败并释放资源。")

        # 保持事件处理器运行，先停止并排空所有相机任务。
        await asyncio.gather(*(
            machine_manager.camera.stop() for machine_manager in self.machine_managers.values()
        ))

        # 收集在途测量和期限任务。
        background_tasks = []
        for machine_manager in self.machine_managers.values():
            background_tasks.extend(machine_manager.deadline_tasks.values())
            background_tasks.extend(machine_manager.background_tasks)

        # 取消剩余后台工作并释放持续任务。
        self.releasing_resources = True
        all_tasks = background_tasks + self.worker_tasks
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self.worker_tasks.clear()
        # 业务任务停止后释放各周期原图和剩余批次事件。
        for machine_manager in self.machine_managers.values():
            for session in tuple(machine_manager.sessions.values()):
                # 标记退出时未完成的周期，只打印日志并执行失败清理。
                if session.state != SessionState.FAILED:
                    session.errors.append("SHUTDOWN_TIMEOUT")
                    await machine_manager.handle_measurement_failure(session)
            # 清空退出后的周期身份与未完成档案。
            machine_manager.sessions.clear()
            machine_manager.active_session_id = None
            machine_manager.frequency_adapter.active_session_id = None
            # 清空不再处理的事件，释放事件携带的图片引用。
            while not machine_manager.queue.empty():
                machine_manager.queue.get_nowait()
                machine_manager.queue.task_done()
        # 清空退出后未执行的存储请求和排队身份。
        while not self.database.queue.empty():
            self.database.queue.get_nowait()
            self.database.queue.task_done()
        self.database.queued_records.clear()
        # 最后关闭相机设备与共享 SDK，再释放恢复库。
        try:
            if self.camera_sdk is not None:
                await run_blocking_operation(self.camera_sdk.close)
        finally:
            self.recovery.close()
