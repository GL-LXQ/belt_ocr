"""组织多机测量的初始化、信号入口、结果等待和资源释放。"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from camera.camera import Camera
from camera.hikrobot_sdk import load_mvs_sdk
from config_util import AppConfig, MachineConfig
from repo.machine_repo import MachineRepo
from frequency_adapter import FrequencyAdapter
from machine import Machine
from enums import EventType, MachineState, ProgressStage, ProgressStatus, SessionState
from models import MeasurementEvent
from async_utils import run_blocking_operation
from text_recognition import TextRecognizer
from database import Database


logger = logging.getLogger(__name__)


class SystemRuntime:
    def __init__(self, config: AppConfig) -> None:
        """保存运行配置，创建共享存储和运行状态。

        Args:
            config: 数据库路径、采集期限和退出参数。

        Returns:
            返回示例：
                None  # 共享依赖已登记，尚未读取机器和连接相机
        """
        # 校验并保存运行配置。
        config.validate()
        self.config = config

        # 创建周期状态变化通知。
        self.state_changed = asyncio.Event()

        # 创建共享存储与共享 OCR 处理器。
        self.database = Database(config, self.publish_event)
        self.text_recognizer = TextRecognizer()

        # 登记机器运行对象与后台任务列表。
        self.machines: dict[str, Machine] = {}
        self.worker_tasks: list[asyncio.Task[None]] = []

        # 登记启动、停止与资源释放标志。
        self.accepting_signals = False
        self.has_started = False
        self.stopping = False
        self.releasing_resources = False
        self.camera_sdk = None

        # 登记故障记录、故障通知与退出任务引用。
        self.failure: Exception | None = None
        self.failure_event = asyncio.Event()
        self.shutdown_task: asyncio.Task[None] | None = None

    def initialize_machines(
        self,
        notify_measurement_progress: Callable[[str, str, ProgressStage, ProgressStatus], None] | None = None,
    ) -> None:
        """在双库初始化之后读取启用机器，并逐台建立相机、频率适配器和机器运行对象。

        Args:
            notify_measurement_progress: 可选进度通知函数，接收机器编号、周期编号、处理阶段和阶段状态。

        Returns:
            返回示例：
                None  # 每台启用机器已按机器编号登记运行对象，没有启用机器时抛出 ValueError
        """
        # 读取业务库中启用的机器。
        enabled_machines = MachineRepo(self.config.database_path).list_enabled()

        # 没有启用机器时拒绝启动。
        if not enabled_machines:
            raise ValueError("没有启用的机器，请先在机器管理页添加并启用机器。")

        # 遍历启用机器，逐台建立运行对象并登记。
        for machine_row in enabled_machines:
            # 用业务库自增编号的字符串形式作为机器编号，组装机器身份配置。
            machine_config = MachineConfig(
                machine_id=str(machine_row["id"]),
                camera_serial=machine_row["camera_serial"],
                frequency_meter_serial=machine_row["frequency_meter_serial"],
            )

            # 建立本机采集器，接入事件入口与致命故障回调。
            camera = Camera(
                machine_config.machine_id,
                self.config.capture_window_ms,
                self.config.camera_timeout_ms,
                self.publish_event,
                self.handle_fatal_error,
            )

            # 建立本机频率接收适配器。
            frequency_adapter = FrequencyAdapter(machine_config, self.config, self.publish_event)

            # 建立本机运行对象并按机器编号登记。
            self.machines[machine_config.machine_id] = Machine(
                machine_config,
                self.config,
                camera,
                frequency_adapter,
                self.text_recognizer,
                self.database,
                self.publish_event,
                notify_measurement_progress,
                self.handle_fatal_error,
                self.state_changed,
            )

    async def start(
        self,
        notify_camera_state: Callable[[str, str, str], None] | None = None,
        notify_measurement_progress: Callable[[str, str, ProgressStage, ProgressStatus], None] | None = None,
    ) -> None:
        """初始化本次运行的机器状态和存储，启动监听与处理任务。

        Args:
            notify_camera_state: 可选连接通知函数，接收机器编号、连接状态和失败原因；GUI 由 MonitoringService 的信号提供，无界面时传 None。
            notify_measurement_progress: 可选进度通知函数，接收机器编号、周期编号、处理阶段和阶段状态。

        Returns:
            None: 完成启动并开放信号入口，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 拒绝重复启动同一个应用实例。
        if self.has_started:
            raise RuntimeError("请为新一次运行创建新的测量应用实例。")
        try:
            # 创建证据图片目录，此处按同步方式执行。
            self.config.evidence_directory.mkdir(parents=True, exist_ok=True)

            # 初始化本地运行库与最终结果库，此处按同步方式执行。
            self.database.initialize()

            # 读取启用机器并逐台建立机器运行对象。
            self.initialize_machines(notify_measurement_progress)

            # 加载相机驱动，此处按同步方式执行。
            self.camera_sdk = load_mvs_sdk(
                self.config.mvs_development_directory,
                self.config.mvs_dll_directory,
            )

            # 逐台机器打开相机并绑定到本机采集器。
            for machine in self.machines.values():
                machine_config = machine.machine_config

                # 通知界面本机正在连接。
                if notify_camera_state is not None:
                    notify_camera_state(machine_config.machine_id, "连接中", "")

                # 按本机序列号和相机参数打开相机。
                try:
                    machine.camera.sdk_camera = self.camera_sdk.open_camera(
                        machine_config.camera_serial,
                        pixel_format=machine_config.camera_pixel_format,
                        exposure_time_us=machine_config.camera_exposure_time_us,
                        gain=machine_config.camera_gain,
                    )
                # 打开失败时通知界面失败原因并抛出。
                except Exception as error:
                    if notify_camera_state is not None:
                        notify_camera_state(machine_config.machine_id, "连接失败", str(error))
                    raise

                # 打开成功时通知界面相机已连接。
                if notify_camera_state is not None:
                    notify_camera_state(machine_config.machine_id, "相机已连接", "IO、频率仪尚未接入")

            # 按现场初始状态设置各机器的等待复位标志。
            for machine in self.machines.values():
                machine.waiting_cycle_reset = self.config.initial_machine_state != MachineState.CLOSED

                # 标记本机启动准备完成。
                machine.initialized = True

            # 逐台机器启动事件处理与频率监听后台任务。
            for machine in self.machines.values():
                machine_config = machine.machine_config

                # 启动本机事件处理任务。
                self.worker_tasks.append(asyncio.create_task(self.run_worker(
                    f"机器处理 machine_id={machine_config.machine_id}", machine.listen_events,
                )))

                # 启动本机频率监听任务。
                self.worker_tasks.append(asyncio.create_task(self.run_worker(
                    f"频率监听 machine_id={machine_config.machine_id} "
                    f"frequency_meter_serial={machine_config.frequency_meter_serial}",
                    machine.frequency_adapter.listen_measurements,
                )))

            # 启动共享的测量存储任务。
            self.worker_tasks.append(asyncio.create_task(self.run_worker("测量存储", self.database.consume_storage_queue)))

            # 标记启动完成并开放信号入口。
            self.has_started = True
            self.accepting_signals = True
        except BaseException as error:
            # 记录启动失败异常。
            logger.exception("测量系统初始化失败")

            # 保存启动故障，供主流程读取。
            if isinstance(error, Exception) and self.failure is None:
                self.failure = error

            # 等待资源清理完成，清理任务被取消时继续等待。
            while True:
                try:
                    await self.stop()
                    break
                except asyncio.CancelledError:
                    if self.shutdown_task.cancelled():
                        raise
            raise

    async def handle_start(self, machine_id: str) -> None:
        """处理某台皮带机启动，不依赖信号来源。

        Args:
            machine_id: 接收启动信号的机器编号。

        Returns:
            返回示例：
                None  # 启动事件已处理完成，不等待采集与识别结果
        """
        await self.send_signal(EventType.MACHINE_STARTED, machine_id)

    async def handle_close(self, machine_id: str) -> None:
        """处理某台皮带机正常关闭，不等待后台识别和保存。

        Args:
            machine_id: 接收关闭信号的机器编号。

        Returns:
            返回示例：
                None  # 关闭事件已处理完成，不等待后台识别和保存
        """
        await self.send_signal(EventType.MACHINE_CLOSED, machine_id)

    async def send_signal(self, event_type: EventType, machine_id: str, payload=None) -> None:
        """把入口信号送入对应机器队列，并等待本次事件处理回执。

        Args:
            event_type: 事件类型，例如 MachineStarted 或 MachineClosed。
            machine_id: 接收信号的机器编号，例如 "1"。
            payload: 随事件传递的业务数据，默认值为 None。

        Returns:
            None: 本次事件处理完成，无返回数据，不等待后台 OCR 和结果保存。
            返回示例：
                None  # 无返回数据
        """
        # 信号入口未开放时拒绝本次信号。
        if not self.accepting_signals:
            raise RuntimeError("测量系统当前未接收信号。")

        # 机器未登记时拒绝本次信号。
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

        # 按事件里的机器编号查找对应机器。
        machine = self.machines.get(event.machine_id)

        # 未知机器的事件写入审计并记录日志后结束分发。
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

    def handle_fatal_error(self, error: Exception) -> None:
        """保存故障、关闭信号入口并安排整个应用退出。

        Args:
            error: 相机或后台任务抛出的异常。

        Returns:
            返回示例：
                None  # 故障已记录，资源清理任务已安排
        """
        # 只保存首次故障。
        if self.failure is None:
            self.failure = error

        # 关闭信号入口并通知故障与状态等待方。
        self.accepting_signals = False
        self.failure_event.set()
        self.state_changed.set()

        # 未创建退出任务时启动一次资源清理。
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(self.release_resources())

    async def run_worker(self, component: str, run_worker) -> None:
        """运行后台任务，异常或意外结束时通知应用退出。

        Args:
            component: 任务名称及机器身份。
            run_worker: 持续运行的异步任务入口。

        Returns:
            返回示例：
                None  # 任务正常结束，或故障已交给主流程
        """
        try:
            # 持续执行任务入口。
            await run_worker()

            # 非停止流程中任务自行结束时按故障抛出。
            if not self.stopping:
                raise RuntimeError(f"后台任务意外结束：{component}")
        except asyncio.CancelledError:
            # 非停止流程中的取消按故障登记。
            if not self.stopping:
                logger.exception("后台任务意外取消 component=%s", component)
                self.handle_fatal_error(RuntimeError(f"后台任务意外取消：{component}"))
            raise
        except Exception as error:
            # 记录后台运行异常并交给致命故障入口。
            logger.exception("后台任务失败 component=%s", component)
            self.handle_fatal_error(error)

    async def wait_for_failure(self) -> None:
        """等待首次故障并向主流程抛出原始异常。

        Args:
            无外部参数。

        Returns:
            无正常返回值。
            返回示例：
                None  # 等待到故障后抛出该异常，不返回数据
        """
        # 等待后台故障通知。
        await self.failure_event.wait()

        # 抛出首次故障。
        raise self.failure

    async def wait_until_idle(self, timeout_seconds: float = 30) -> None:
        """等待测量结算，收到程序故障时立即抛出异常。

        Args:
            timeout_seconds: 等待测量完成的最长秒数。

        Returns:
            返回示例：
                None  # 全部测量已结算；超时或程序故障时抛出异常
        """
        # 记录本次等待的到期时间。
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            # 取出本轮开始前的状态通知。
            self.state_changed.clear()

            # 出现程序故障时立即抛出。
            if self.failure is not None:
                raise self.failure

            # 所有机器都没有活动周期时结束等待。
            if not any(machine.current_session is not None for machine in self.machines.values()):
                return

            # 剩余期限不足时按超时抛出。
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                raise asyncio.TimeoutError("测量任务或现场周期尚未全部结束。")

            # 等待下一次状态变化通知。
            await asyncio.wait_for(self.state_changed.wait(), remaining_seconds)

    async def stop(self) -> None:
        """停止应用并等待同一个资源清理任务完成。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 相机、后台任务和本地记录库已释放
        """
        # 未创建退出任务时启动一次资源清理。
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(self.release_resources())

        # 等待清理任务完成，不受本次调用取消的影响。
        await asyncio.shield(self.shutdown_task)

    async def release_resources(self) -> None:
        """停止全部测量，释放后台任务、相机和数据库资源。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 测量、任务和相机资源已清理，清理故障保存在 failure 中
        """
        # 关闭信号入口并标记进入停止流程。
        self.accepting_signals = False
        self.stopping = True

        async def drain_measurements() -> None:
            """中断活动周期并等待现有业务队列排空。

            Args:
                无外部参数。

            Returns:
                返回示例：
                    None  # 现有测量和队列已结算
            """
            # 向每台机器发送中断事件并等待处理回执。
            for machine in self.machines.values():
                acknowledgement = asyncio.get_running_loop().create_future()
                await self.publish_event(MeasurementEvent(
                    EventType.SHUTDOWN, machine.machine_config.machine_id,
                    acknowledgement=acknowledgement,
                ))
                await acknowledgement

            # 等待各机器周期结算完成。
            await self.wait_until_idle(self.config.shutdown_timeout_ms / 1000)

            # 等待共享存储队列排空。
            await self.database.queue.join()

            # 等待各机器事件队列排空。
            for machine in self.machines.values():
                await machine.queue.join()

        # 无故障且已启动后台任务时，在退出期限内完成测量收尾。
        try:
            if self.failure is None and self.worker_tasks:
                await asyncio.wait_for(
                    drain_measurements(), self.config.shutdown_timeout_ms / 1000,
                )
        # 收尾超时只记录日志，继续释放资源。
        except asyncio.TimeoutError:
            logger.warning("退出等待到期，未完成测量将标记失败并释放资源。")
        # 收尾失败时记录异常并登记故障。
        except Exception as error:
            if error is not self.failure:
                logger.exception("退出测量失败")
            self.handle_fatal_error(error)

        # 标记进入资源释放阶段，停止事件交付。
        self.releasing_resources = True

        # 排空各机器事件队列，释放阻塞入队的相机交付任务。
        for machine in self.machines.values():
            while not machine.queue.empty():
                event = machine.queue.get_nowait()
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.cancel()
                machine.queue.task_done()

        # 停止全部相机的采集线程。
        camera_results = await asyncio.gather(*(
            machine.camera.stop() for machine in self.machines.values()
        ), return_exceptions=True)

        # 逐条登记相机释放过程中的异常。
        for result in camera_results:
            if isinstance(result, Exception):
                self.handle_fatal_error(result)

        # 收集各机器的期限任务与识别任务。
        background_tasks = []
        for machine in self.machines.values():
            background_tasks.extend(machine.deadline_tasks.values())
            if machine.recognition_task is not None:
                background_tasks.append(machine.recognition_task)

        # 取消全部后台任务。
        all_tasks = background_tasks + self.worker_tasks
        for task in all_tasks:
            task.cancel()

        # 等待被取消的任务结束。
        await asyncio.gather(*all_tasks, return_exceptions=True)

        # 清空已登记的后台任务列表。
        self.worker_tasks.clear()

        # 业务任务停止后释放各周期最终图片和剩余事件。
        for machine in self.machines.values():
            session = machine.current_session

            # 退出时未完成的周期按故障或超时登记，并执行失败清理。
            if session is not None:
                if session.state != SessionState.FAILED:
                    session.errors.append("PROGRAM_FAILED" if self.failure is not None else "SHUTDOWN_TIMEOUT")
                    try:
                        await machine.handle_measurement_failure(session)
                    except Exception as error:
                        self.handle_fatal_error(error)

            # 清空退出后的周期身份与未完成档案。
            machine.current_session = None
            machine.frequency_adapter.active_session_id = None

            # 清空不再处理的事件，释放事件携带的图片引用。
            while not machine.queue.empty():
                event = machine.queue.get_nowait()
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.cancel()
                machine.queue.task_done()

        # 清空未执行的存储请求。
        while not self.database.queue.empty():
            self.database.queue.get_nowait()
            self.database.queue.task_done()

        # 清空存储队列记录的排队身份。
        self.database.queued_records.clear()

        # 关闭相机驱动与共享 SDK。
        try:
            if self.camera_sdk is not None:
                await run_blocking_operation(self.camera_sdk.close)
        except Exception as error:
            # 记录关闭相机驱动失败并登记故障。
            logger.exception("关闭相机驱动失败")
            self.handle_fatal_error(error)
        finally:
            # 关闭本地记录库并释放实例锁。
            try:
                self.database.close()
            except Exception as error:
                # 记录关闭记录库失败并登记故障。
                logger.exception("关闭本地记录库失败")
                self.handle_fatal_error(error)

            # 通知全部状态等待方本次退出已结束。
            self.state_changed.set()
