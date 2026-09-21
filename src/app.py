"""组织多机测量的初始化、信号入口、结果等待和资源释放。"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from camera import Camera
from mvs_sdk import load_mvs_sdk
from config_util import AppConfig, MachineConfig
from repo.machine_repo import MachineRepo
from frequency_adapter import FrequencyAdapter
from machine import Machine
from enums import MachineState, SessionState, EventType
from models import MeasurementEvent
from async_utils import run_blocking_operation
from text_recognition import TextRecognizer
from database import Database


logger = logging.getLogger(__name__)


class App:
    def __init__(self, config: AppConfig) -> None:
        """保存运行配置，创建共享存储和运行状态。

        Args:
            config: 数据库路径、采集期限和退出参数。

        Returns:
            返回示例：
                None  # 共享依赖已登记，尚未读取机器和连接相机
        """
        config.validate()
        self.config = config
        self.state_changed = asyncio.Event()
        self.database = Database(config, self.publish_event)
        self.text_recognizer = TextRecognizer()
        self.machines: dict[str, Machine] = {}
        self.worker_tasks: list[asyncio.Task[None]] = []
        self.accepting_signals = False
        self.has_started = False
        self.stopping = False
        self.releasing_resources = False
        self.camera_sdk = None
        self.failure: Exception | None = None
        self.failure_event = asyncio.Event()
        self.shutdown_task: asyncio.Task[None] | None = None

    def initialize_machines(self) -> None:
        """在双库初始化之后读取启用机器，并逐台建立相机、频率适配器和机器运行对象。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 每台启用机器已按机器编号登记运行对象，没有启用机器时抛出 ValueError
        """
        # 读取业务库中启用的机器。
        enabled_machines = MachineRepo(self.config.database_path).list_enabled()
        if not enabled_machines:
            raise ValueError("没有启用的机器，请先在机器管理页添加并启用机器。")

        # 为每台机器建立独立的采集器、频率适配器和机器运行对象。
        for machine_row in enabled_machines:
            machine_config = MachineConfig(
                machine_id=str(machine_row["id"]),
                camera_serial=machine_row["camera_serial"],
                frequency_meter_serial=machine_row["frequency_meter_serial"],
            )
            camera = Camera(
                machine_config.machine_id,
                self.config.capture_window_ms,
                self.config.camera_timeout_ms,
                self.publish_event,
                self.report_failure,
            )
            frequency_adapter = FrequencyAdapter(machine_config, self.config, self.publish_event)
            self.machines[machine_config.machine_id] = Machine(
                machine_config,
                self.config,
                camera,
                frequency_adapter,
                self.text_recognizer,
                self.database,
                self.publish_event,
                self.state_changed,
            )

    async def start(self, notify_camera_state: Callable[[str, str, str], None] | None = None) -> None:
        """初始化本次运行的机器状态和存储，启动监听与处理任务。

        Args:
            notify_camera_state: 可选连接通知函数，接收机器编号、连接状态和失败原因；GUI 由 MonitoringService 的信号提供，无界面时传 None。

        Returns:
            None: 完成启动并开放信号入口，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 拒绝重复启动同一个应用实例。
        if self.has_started:
            raise RuntimeError("请为新一次运行创建新的测量应用实例。")
        try:
            # 初始化图片目录、本地运行库和最终结果库，此处按同步方式执行。
            self.config.evidence_directory.mkdir(parents=True, exist_ok=True)
            self.database.initialize()

            # 读取启用机器并逐台建立机器运行对象，没有启用机器时拒绝启动。
            self.initialize_machines()

            # 加载相机驱动，按配置逐台打开相机，此处按同步方式执行。
            self.camera_sdk = load_mvs_sdk(
                self.config.mvs_development_directory,
                self.config.mvs_dll_directory,
            )
            for machine in self.machines.values():
                machine_config = machine.machine_config
                # 通知界面连接状态，并将相机绑定到本机器。
                if notify_camera_state is not None:
                    notify_camera_state(machine_config.machine_id, "连接中", "")
                try:
                    machine.camera.sdk_camera = self.camera_sdk.open_camera(
                        machine_config.camera_serial,
                        pixel_format=machine_config.camera_pixel_format,
                        exposure_time_us=machine_config.camera_exposure_time_us,
                        gain=machine_config.camera_gain,
                    )
                except Exception as error:
                    if notify_camera_state is not None:
                        notify_camera_state(machine_config.machine_id, "连接失败", str(error))
                    raise
                if notify_camera_state is not None:
                    notify_camera_state(machine_config.machine_id, "相机已连接", "IO、频率仪尚未接入")

            # 设置现场初始状态，运行中或未知时等待真实关闭。
            for machine in self.machines.values():
                machine.waiting_cycle_reset = self.config.initial_machine_state != MachineState.CLOSED
                machine.initialized = True

            # 启动独立机器处理和频率监听，任一任务异常都停止应用。
            for machine in self.machines.values():
                machine_config = machine.machine_config
                self.worker_tasks.append(asyncio.create_task(self.run_worker(
                    f"机器处理 machine_id={machine_config.machine_id}", machine.listen_events,
                )))
                self.worker_tasks.append(asyncio.create_task(self.run_worker(
                    f"频率监听 machine_id={machine_config.machine_id} "
                    f"frequency_meter_serial={machine_config.frequency_meter_serial}",
                    machine.frequency_adapter.run,
                )))

            # 启动共享存储，不执行自动重启。
            self.worker_tasks.append(asyncio.create_task(self.run_worker("STORAGE", self.database.run)))
            self.has_started = True
            self.accepting_signals = True
        except BaseException as error:
            # 启动失败时记录异常并释放已打开的相机和本地记录库。
            logger.exception("测量系统初始化失败")
            if isinstance(error, Exception) and self.failure is None:
                self.failure = error
            # 等待启动资源清理完成，保留取消期间发生的原始启动异常。
            while True:
                try:
                    await self.stop()
                    break
                except asyncio.CancelledError:
                    if self.shutdown_task.cancelled():
                        raise
            raise

    async def handle_start(self, machine_id: str) -> None:
        """处理某台皮带机启动，不依赖信号来源。"""
        await self.send_signal(EventType.MACHINE_STARTED, machine_id)

    async def handle_close(self, machine_id: str) -> None:
        """处理某台皮带机正常关闭，不等待后台识别和保存。"""
        await self.send_signal(EventType.MACHINE_CLOSED, machine_id)

    async def send_signal(self, event_type: EventType, machine_id: str, payload=None) -> None:
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
        if machine_id not in self.machines:
            raise ValueError(f"未配置机器：{machine_id}")

        # 创建本次事件的处理回执。
        acknowledgement = asyncio.get_running_loop().create_future()

        # 将信号和回执送入机器队列，关闭事件携带当前活动 Session 编号。
        await self.publish_event(
            MeasurementEvent(
                event_type,
                machine_id,
                session_id=(
                    self.machines[machine_id].current_session.session_id
                    if event_type == EventType.MACHINE_CLOSED
                    and self.machines[machine_id].current_session is not None else None
                ),
                payload=payload,
                acknowledgement=acknowledgement,
            )
        )

        # 等待对应机器确认本次事件处理完成。
        await acknowledgement

    async def publish_event(self, event: MeasurementEvent) -> None:
        """更新事件接收时间，并将事件送入对应机器的有界队列。

        Args:
            event: 待分发的测量事件，包含事件类型、机器编号及相关业务数据。

        Returns:
            None: 事件入队或提前结束分发，无返回数据，不等待对应机器处理。
            返回示例：
                None  # 无返回数据
        """
        # 释放资源期间取消事件回执，结束本次分发。
        if self.releasing_resources:
            if event.acknowledgement is not None:
                event.acknowledgement.cancel()
            return

        # 查找对应机器，登记并隔离未知机器的事件。
        machine = self.machines.get(event.machine_id)
        if machine is None:
            await run_blocking_operation(self.database.save_abnormal_event, "UNKNOWN_MACHINE", event)
            logger.warning("隔离未知机器事件 machine_id=%s", event.machine_id)
            return

        # 更新本次路由接收时间。
        event = replace(
            event,
            received_at=datetime.now(timezone.utc).isoformat(),
            received_monotonic=time.monotonic(),
        )

        # 将事件放入对应机器队列，队列满时等待空位。
        await machine.queue.put(event)
        # 退出期间释放此前阻塞入队的事件和回执。
        if self.releasing_resources:
            while not machine.queue.empty():
                pending_event = machine.queue.get_nowait()
                if pending_event.acknowledgement is not None and not pending_event.acknowledgement.done():
                    pending_event.acknowledgement.cancel()
                machine.queue.task_done()

    async def synchronize_machine(self, machine_id: str, observed_state: MachineState | str) -> None:
        """将现场状态转换为枚举并发送机器同步事件。

        Args:
            machine_id: 需要同步状态的机器编号。
            observed_state: 已确认的现场状态，支持 MachineState 或对应字符串。

        Returns:
            None: 等待对应机器处理同步事件，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 校验现场状态并转换为机器状态枚举。
        if observed_state not in set(MachineState):
            raise ValueError("机器状态必须是 CLOSED、OPEN 或 UNKNOWN。")
        machine_state = MachineState(observed_state)

        # 发送状态同步事件并等待处理完成。
        await self.send_signal(EventType.MACHINE_SYNCHRONIZED, machine_id, machine_state)

    def report_failure(self, error: Exception) -> None:
        """保存故障、关闭信号入口并安排整个应用退出。

        Args:
            error: 相机或后台任务抛出的异常。

        Returns:
            None  # 故障已记录，资源清理已安排
        """
        # 保存首次故障供主流程接收，通知所有状态等待方。
        if self.failure is None:
            self.failure = error
        self.accepting_signals = False
        self.failure_event.set()
        self.state_changed.set()

        # 只启动一次资源清理，后续 stop 调用等待同一个退出任务。
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(self.release_resources())

    async def run_worker(self, component: str, run_worker) -> None:
        """运行后台任务，异常或意外结束时通知应用退出。

        Args:
            component: 任务名称及机器身份。
            run_worker: 持续运行的异步任务入口。

        Returns:
            None  # 正常退出，或故障已通知主流程
        """
        try:
            # 持续执行任务，正常运行期间不允许任务自行结束。
            await run_worker()
            if not self.stopping:
                raise RuntimeError(f"后台任务意外结束：{component}")
        except asyncio.CancelledError:
            # 退出期间允许取消，其余取消视为后台任务故障。
            if not self.stopping:
                logger.exception("后台任务意外取消 component=%s", component)
                self.report_failure(RuntimeError(f"后台任务意外取消：{component}"))
            raise
        except Exception as error:
            # 记录后台运行异常，再安排整个应用退出。
            logger.exception("后台任务失败 component=%s", component)
            self.report_failure(error)

    async def wait_for_failure(self) -> None:
        """等待首次故障并向主流程抛出原始异常。

        Args:
            无外部参数。

        Returns:
            无正常返回值  # 收到故障后抛出异常
        """
        # 等待后台故障通知，再交付故障原因。
        await self.failure_event.wait()
        raise self.failure

    async def wait_until_idle(self, timeout_seconds: float = 30) -> None:
        """等待测量结算，收到程序故障时立即抛出异常。

        Args:
            timeout_seconds: 等待测量完成的最长秒数。

        Returns:
            None  # 全部测量已结算；超时或程序故障时抛出异常
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            # 检查当前状态，再等待下一次状态更新。
            self.state_changed.clear()
            if self.failure is not None:
                raise self.failure
            if not any(machine.current_session is not None for machine in self.machines.values()):
                return
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                raise asyncio.TimeoutError("测量任务或现场周期尚未全部结束。")
            await asyncio.wait_for(self.state_changed.wait(), remaining_seconds)

    async def stop(self) -> None:
        """停止应用并等待同一个资源清理任务完成。

        Args:
            无外部参数。

        Returns:
            None  # 相机、后台任务和本地记录库已释放
        """
        # 复用故障或正常退出创建的清理任务。
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(self.release_resources())
        await asyncio.shield(self.shutdown_task)

    async def release_resources(self) -> None:
        """停止全部测量，释放后台任务、相机和数据库资源。

        Args:
            无外部参数。

        Returns:
            None  # 测量、任务和相机资源已清理，清理故障保存在 failure 中
        """
        self.accepting_signals = False
        self.stopping = True

        async def drain_measurements() -> None:
            """中断活动周期并等待现有业务队列排空。

            Args:
                无外部参数。

            Returns:
                None  # 现有测量和队列已结算
            """
            # 将尚未关闭的现场周期标记为中断。
            for machine in self.machines.values():
                acknowledgement = asyncio.get_running_loop().create_future()
                await self.publish_event(MeasurementEvent(
                    EventType.SHUTDOWN, machine.machine_config.machine_id,
                    acknowledgement=acknowledgement,
                ))
                await acknowledgement

            # 等待记录提交和已经入队的业务事件。
            await self.wait_until_idle(self.config.shutdown_timeout_ms / 1000)
            await self.database.queue.join()
            for machine in self.machines.values():
                await machine.queue.join()

        # 在统一退出期限内完成后台收尾。
        try:
            if self.failure is None and self.worker_tasks:
                await asyncio.wait_for(
                    drain_measurements(), self.config.shutdown_timeout_ms / 1000,
                )
        except asyncio.TimeoutError:
            logger.warning("退出等待到期，未完成测量将标记失败并释放资源。")
        except Exception as error:
            if error is not self.failure:
                logger.exception("退出测量失败")
            self.report_failure(error)

        # 停止事件交付并取消业务任务，禁止退出期间启动新的采集。
        self.releasing_resources = True
        # 排空退出时的事件队列，释放阻塞入队的相机交付任务。
        for machine in self.machines.values():
            while not machine.queue.empty():
                event = machine.queue.get_nowait()
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.cancel()
                machine.queue.task_done()

        # 停止并排空相机线程，异常不阻止其他相机释放。
        camera_results = await asyncio.gather(*(
            machine.camera.stop() for machine in self.machines.values()
        ), return_exceptions=True)
        for result in camera_results:
            if isinstance(result, Exception):
                self.report_failure(result)

        # 取消后台工作并等待在途阻塞操作完成。
        background_tasks = []
        for machine in self.machines.values():
            background_tasks.extend(machine.deadline_tasks.values())
            if machine.recognition_task is not None:
                background_tasks.append(machine.recognition_task)
        all_tasks = background_tasks + self.worker_tasks
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self.worker_tasks.clear()

        # 业务任务停止后释放各周期最终图片和剩余事件。
        for machine in self.machines.values():
            session = machine.current_session
            if session is not None:
                # 标记退出时未完成的周期，只打印日志并执行失败清理。
                if session.state != SessionState.FAILED:
                    session.errors.append("PROGRAM_FAILED" if self.failure is not None else "SHUTDOWN_TIMEOUT")
                    try:
                        await machine.handle_measurement_failure(session)
                    except Exception as error:
                        self.report_failure(error)
            # 清空退出后的周期身份与未完成档案。
            machine.current_session = None
            machine.frequency_adapter.active_session_id = None
            # 清空不再处理的事件，释放事件携带的图片引用。
            while not machine.queue.empty():
                event = machine.queue.get_nowait()
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.cancel()
                machine.queue.task_done()
        # 清空退出后未执行的存储请求和排队身份。
        while not self.database.queue.empty():
            self.database.queue.get_nowait()
            self.database.queue.task_done()
        self.database.queued_records.clear()
        # 最后关闭相机与共享 SDK，再释放数据库实例锁。
        try:
            if self.camera_sdk is not None:
                await run_blocking_operation(self.camera_sdk.close)
        except Exception as error:
            logger.exception("关闭相机驱动失败")
            self.report_failure(error)
        finally:
            try:
                self.database.close()
            except Exception as error:
                logger.exception("关闭本地记录库失败")
                self.report_failure(error)
            self.state_changed.set()
