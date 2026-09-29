"""组织多机测量的初始化、信号入口、结果等待和资源释放。"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from camera.camera import Camera
from camera.hikrobot_sdk import MvsError, load_mvs_sdk
from config_util import AppConfig, MachineConfig
from repo.machine_repo import MachineRepo
from frequency_adapter import FrequencyAdapter
from runtime.machine_runtime import MachineRuntime
from enums import EventType, MachineState, ProgressStage, ProgressStatus
from models import RuntimeEvent
from async_utils import run_blocking_operation
from text_recognizer import TextRecognizer
from database import Database
from modbus_client import ModbusClient


logger = logging.getLogger(__name__)


class SystemRuntime:
    def __init__(self, config: AppConfig) -> None:
        """保存运行配置，创建数据库和运行状态。

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

        # 创建数据库和共享 OCR 处理器。
        self.database = Database(config)
        self.text_recognizer = TextRecognizer()

        # 登记机器运行对象与后台任务列表。
        self.machines: dict[str, MachineRuntime] = {}
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

        # 登记 Modbus 客户端空位，客户端在启动校验通过后创建。
        self.modbus_client: ModbusClient | None = None

        # 初始化 DI 上次状态空位。
        self.io_previous_states: dict[int, bool] = {}

    def initialize_machines(
        self,
        notify_measurement_progress: Callable[[str, str, ProgressStage, ProgressStatus], None] | None = None,
        notify_camera_state: Callable[[str, str, str], None] | None = None,
        notify_ocr_result: Callable[[str, str, tuple[str, ...], tuple[str, ...]], None] | None = None,
        notify_cycle_closed: Callable[[str, str], None] | None = None,
    ) -> None:
        """在双库初始化之后读取启用机器，并逐台建立相机、频率适配器和机器运行对象。

        Args:
            notify_measurement_progress: 可选进度通知函数，接收机器编号、周期编号、处理阶段和阶段状态。
            notify_ocr_result: 可选文字通知函数，接收机器编号、周期编号、原文字和去空格文字。
            notify_camera_state: 可选相机状态通知函数，接收机器编号、状态和原因。
            notify_cycle_closed: 可选周期关闭通知函数，接收机器编号和周期编号。

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
            # 组装机器身份与公共相机运行参数。
            machine_config = MachineConfig(
                machine_id=str(machine_row["id"]),
                camera_serial=machine_row["camera_serial"],
                frequency_meter_serial=machine_row["frequency_meter_serial"],
                camera_pixel_format=self.config.camera_pixel_format,
                camera_exposure_time_us=self.config.camera_exposure_time_us,
                camera_gain=self.config.camera_gain,
                camera_line_selector=self.config.camera_line_selector,
                camera_line_mode=self.config.camera_line_mode,
                camera_line_source=self.config.camera_line_source,
                camera_strobe_enabled=self.config.camera_strobe_enabled,
            )

            # 建立本机采集器，接入事件入口与系统故障回调。
            camera = Camera(
                machine_config.machine_id,
                self.config.capture_window_ms,
                self.config.camera_timeout_ms,
                self.publish_event,
                self.handle_system_failure,
            )

            # 建立本机频率接收适配器。
            frequency_adapter = FrequencyAdapter(machine_config, self.config, self.publish_event)

            # 建立本机运行对象并按机器编号登记。
            self.machines[machine_config.machine_id] = MachineRuntime(
                machine_config,
                self.config,
                camera,
                frequency_adapter,
                self.text_recognizer,
                self.database,
                self.publish_event,
                notify_measurement_progress,
                self.handle_system_failure,
                self.state_changed,
                notify_camera_state,
                notify_ocr_result,
                notify_cycle_closed,
            )

    async def start(
        self,
        notify_camera_state: Callable[[str, str, str], None] | None = None,
        notify_measurement_progress: Callable[[str, str, ProgressStage, ProgressStatus], None] | None = None,
        notify_ocr_result: Callable[[str, str, tuple[str, ...], tuple[str, ...]], None] | None = None,
        notify_cycle_closed: Callable[[str, str], None] | None = None,
    ) -> None:
        """初始化本次运行的机器状态和存储，启动监听与处理任务。

        Args:
            notify_camera_state: 可选连接通知函数，接收机器编号、连接状态和失败原因；
                GUI 由 Controller 管理的后台 Runtime 线程提供 Qt 信号，无界面时传 None。
            notify_measurement_progress: 可选进度通知函数，接收机器编号、周期编号、处理阶段和阶段状态。
            notify_ocr_result: 可选文字通知函数，接收机器编号、周期编号、原文字和去空格文字。
            notify_cycle_closed: 可选周期关闭通知函数，接收机器编号和周期编号。

        Returns:
            None: 完成启动并开放信号入口，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 拒绝重复启动同一个应用实例。
        if self.has_started:
            raise RuntimeError("请为新一次运行创建新的测量应用实例。")

        # 记录本次启动使用的存储路径。
        logger.info(
            "Runtime开始启动 database_path=%s recovery_path=%s evidence_directory=%s",
            self.config.database_path,
            self.config.recovery_path,
            self.config.evidence_directory,
        )
        try:
            # 创建证据图片目录，此处按同步方式执行。
            self.config.evidence_directory.mkdir(parents=True, exist_ok=True)

            # 初始化本地运行库与最终结果库，此处按同步方式执行。
            self.database.initialize()

            # 读取启用机器并逐台建立机器运行对象。
            self.initialize_machines(
                notify_measurement_progress,
                notify_camera_state,
                notify_ocr_result,
                notify_cycle_closed,
            )

            # 记录本次运行加载的启用机器。
            logger.info(
                "已加载启用机器 machine_count=%s machine_ids=%s",
                len(self.machines),
                ", ".join(sorted(self.machines)),
            )

            # 校验当前启用机器的串口和 DI 通道绑定。
            self.validate_io_configuration()

            # 串口名称缺失时拒绝启动。
            serial_port = self.config.modbus_serial_port
            if serial_port is None:
                raise ValueError("未配置 Modbus RTU 串口。")

            # 创建 Modbus 客户端，连接由 IO 监听轮询时建立。
            self.modbus_client = ModbusClient(
                serial_port=serial_port,
                baudrate=self.config.modbus_baudrate,
                parity=self.config.modbus_parity,
                stopbits=self.config.modbus_stopbits,
                bytesize=self.config.modbus_bytesize,
                unit_id=self.config.modbus_unit_id,
                timeout=self.config.modbus_timeout_seconds,
            )

            # 加载相机驱动，此处按同步方式执行。
            self.camera_sdk = load_mvs_sdk(self.config.mvs_development_directory, self.config.mvs_dll_directory)

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
                        line_selector=machine_config.camera_line_selector,
                        line_mode=machine_config.camera_line_mode,
                        line_source=machine_config.camera_line_source,
                        strobe_enabled=machine_config.camera_strobe_enabled,
                    )
                # 记录单台相机设备连接失败。
                except MvsError as error:
                    logger.error(
                        "相机连接失败 machine_id=%s camera_serial=%s error=%s",
                        machine_config.machine_id,
                        machine_config.camera_serial,
                        error,
                    )
                    if notify_camera_state is not None:
                        notify_camera_state(machine_config.machine_id, "连接失败", str(error))

                    # 继续初始化其他相机。
                    continue

                # 未知错误仍交给系统启动失败流程。
                except Exception as error:
                    if notify_camera_state is not None:
                        notify_camera_state(machine_config.machine_id, "连接失败", str(error))
                    raise

                # 记录本机相机连接成功。
                logger.info(
                    "相机连接成功 machine_id=%s camera_serial=%s "
                    "pixel_format=%s exposure_time_us=%s gain=%s "
                    "line_selector=%s line_mode=%s line_source=%s strobe_enabled=%s",
                    machine_config.machine_id,
                    machine_config.camera_serial,
                    machine_config.camera_pixel_format,
                    machine_config.camera_exposure_time_us,
                    machine_config.camera_gain,
                    machine_config.camera_line_selector,
                    machine_config.camera_line_mode,
                    machine_config.camera_line_source,
                    machine_config.camera_strobe_enabled,
                )

                # 打开成功时通知界面相机已连接。
                if notify_camera_state is not None:
                    notify_camera_state(machine_config.machine_id, "相机已连接", "IO、频率仪尚未接入")

            # 所有相机连接失败时结束本次启动。
            if all(machine.camera.sdk_camera is None for machine in self.machines.values()):
                raise MvsError("所有启用机器的相机均连接失败")

            # 记录共享 OCR Engine 初始化开始。
            logger.info("开始初始化 OCR Engine")

            # 在线程中初始化共享 OCR Engine 并等待模型准备完成。
            await run_blocking_operation(self.text_recognizer.initialize)
            logger.info("OCR Engine 已准备好")

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
                    f"机器处理 machine_id={machine_config.machine_id}",
                    machine.listen_events,
                )))

                # 启动本机频率监听任务。
                self.worker_tasks.append(asyncio.create_task(self.run_worker(
                    f"频率监听 machine_id={machine_config.machine_id} "
                    f"frequency_meter_serial={machine_config.frequency_meter_serial}",
                    machine.frequency_adapter.listen_measurements,
                )))

            # 标记启动完成。
            self.has_started = True

            # 开放机器信号入口。
            self.accepting_signals = True

            # 启动 Modbus DI 监听任务。
            self.worker_tasks.append(asyncio.create_task(self.run_worker("Modbus IO监听", self.listen_io)))

            # 记录 Runtime 已正式启动并开放现场信号入口。
            logger.info(
                "Runtime启动完成 machine_count=%s worker_count=%s modbus_serial_port=%s",
                len(self.machines),
                len(self.worker_tasks),
                serial_port,
            )
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

    def validate_io_configuration(self) -> None:
        """校验启用机器对应的 Modbus 串口和 DI 通道配置。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 当前启用机器均已配置唯一的非负 DI 通道
        """
        # 未配置串口时拒绝启用 IO。
        if self.config.modbus_serial_port is None:
            raise ValueError("未配置 Modbus RTU 串口。")

        # 找出未绑定 DI 通道的启用机器。
        enabled_machine_ids = set(self.machines)
        channel_mapping = self.config.io_machine_channels
        missing_machine_ids = enabled_machine_ids - channel_mapping.keys()
        if missing_machine_ids:
            missing_ids = ", ".join(sorted(missing_machine_ids))
            raise ValueError(f"启用机器未配置 DI 通道：{missing_ids}。")

        # 检查启用机器的通道非负且互不重复。
        enabled_channels = [channel_mapping[machine_id] for machine_id in enabled_machine_ids]
        if any(
            not isinstance(channel, int) or isinstance(channel, bool) or channel < 0
            for channel in enabled_channels
        ):
            raise ValueError("DI 通道必须是大于等于零的整数。")
        if len(enabled_channels) != len(set(enabled_channels)):
            raise ValueError("启用机器不能绑定相同的 DI 通道。")

    async def listen_io(self) -> None:
        """持续读取 DI 状态并交给机器状态处理流程。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 停止流程开始后结束监听，串口由退出流程关闭
        """
        if self.modbus_client is None:
            raise RuntimeError("Modbus RTU 客户端未初始化。")

        # 根据启用机器的最大通道确定连续读取数量。
        input_count = max(
            self.config.io_machine_channels[machine_id] for machine_id in self.machines
        ) + 1
        while not self.stopping:
            # 读取全部启用机器覆盖范围内的 DI 状态。
            states = await self.modbus_client.read_discrete_inputs(
                address=self.config.modbus_input_address,
                count=input_count,
            )

            # 停止流程开始或信号入口关闭时结束监听。
            if self.stopping or not self.accepting_signals:
                break

            # 通信失败时记录日志。
            if states is None:
                logger.warning("IO 读取失败，旧 DI 状态已清空")

                # 清空旧 DI 状态。
                self.io_previous_states.clear()

                # 中断仍未收到 CLOSE 的机器周期。
                for machine_id, machine in self.machines.items():
                    session = machine.current_session
                    # 中断会写入关闭时间，此判断同时保证一次断线只通知一次。
                    if session is not None and session.capture_stop_time is None:
                        await self.send_signal(EventType.IO_INTERRUPTED, machine_id)

                # 按重连间隔等待下一轮读取。
                await asyncio.sleep(self.config.modbus_reconnect_interval_ms / 1000)
                continue

            # 处理有效状态后按正常轮询间隔等待。
            await self.handle_io_states(states)
            await asyncio.sleep(self.config.modbus_poll_interval_ms / 1000)

    async def handle_io_states(self, states: list[bool]) -> None:
        """把当前 DI 状态保存为基线或转换为启动和关闭信号。

        Args:
            states: 从 Modbus 起始地址返回的零基 DI 状态列表。

        Returns:
            返回示例：
                None  # 当前状态已保存或状态变化已交给机器入口
        """
        input_count = max(self.config.io_machine_channels[machine_id] for machine_id in self.machines) + 1

        # 状态数量不足时放弃整次读取结果。
        if len(states) < input_count:
            logger.warning("DI 状态数量不足，期望 %s，实际 %s", input_count, len(states))
            return

        # 按机器绑定通道逐一处理初始状态或状态变化。
        for machine_id, machine in self.machines.items():
            channel = self.config.io_machine_channels[machine_id]
            current_state = bool(states[channel])
            previous_state = self.io_previous_states.get(channel)

            # 首次有效状态只更新机器复位标志。
            if previous_state is None:
                machine.waiting_cycle_reset = current_state

                # 记录本机 DI 初始状态。
                logger.info(
                    "DI初始状态 machine_id=%s channel=%s state=%s",
                    machine_id,
                    channel,
                    current_state,
                )

            # 后续状态变化进入现有启动或关闭入口。
            elif previous_state != current_state:
                # 记录本次 DI 电平变化。
                logger.info(
                    "DI状态变化 machine_id=%s channel=%s previous=%s current=%s",
                    machine_id,
                    channel,
                    previous_state,
                    current_state,
                )

                if current_state:
                    await self.handle_start(machine_id)
                else:
                    await self.handle_close(machine_id)

            # 登记本次有效状态。
            self.io_previous_states[channel] = current_state

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
            RuntimeEvent(
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

    async def publish_event(self, event: RuntimeEvent) -> None:
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
            await run_blocking_operation(
                self.database.save_abnormal_event, "未知机器", event
            )
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
            machine.discard_pending_events()

    def handle_system_failure(self, error: Exception) -> None:
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

            # 记录首次系统故障的异常类型和内容。
            logger.error(
                "系统故障 error_type=%s error=%s",
                type(error).__name__,
                error,
            )

        # 关闭信号入口并通知故障与状态等待方。
        self.accepting_signals = False
        self.failure_event.set()
        self.state_changed.set()

        # 未创建退出任务时启动一次资源清理。
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(self._shutdown_system_and_release_resources())

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
                self.handle_system_failure(RuntimeError(f"后台任务意外取消：{component}"))
            raise
        except Exception as error:
            # 记录后台运行异常并交给系统故障入口。
            logger.exception("后台任务失败 component=%s", component)
            self.handle_system_failure(error)

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
            self.shutdown_task = asyncio.create_task(self._shutdown_system_and_release_resources())

        # 等待清理任务完成，不受本次调用取消的影响。
        await asyncio.shield(self.shutdown_task)

    async def _drain_pending_work(self) -> None:
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
            await self.publish_event(RuntimeEvent(
                EventType.SHUTDOWN,
                machine.machine_config.machine_id,
                acknowledgement=acknowledgement,
            ))
            await acknowledgement

        # 等待各机器周期结算完成。
        await self.wait_until_idle(self.config.shutdown_timeout_ms / 1000)

        # 等待各机器事件队列排空。
        for machine in self.machines.values():
            await machine.wait_until_event_queue_drained()

    async def _shutdown_system_and_release_resources(self) -> None:
        """停止全部测量，释放后台任务、相机和数据库资源。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 测量、任务和相机资源已清理，清理故障保存在 failure 中
        """
        # 记录本次停止开始时的故障、机器和后台任务数量。
        logger.info(
            "Runtime开始停止 failure=%s machine_count=%s worker_count=%s",
            self.failure,
            len(self.machines),
            len(self.worker_tasks),
        )

        # 关闭信号入口并标记进入停止流程。
        self.accepting_signals = False
        self.stopping = True

        # 无故障且已启动后台任务时，在退出期限内完成测量收尾。
        try:
            if self.failure is None and self.worker_tasks:
                await asyncio.wait_for(self._drain_pending_work(), self.config.shutdown_timeout_ms / 1000)
        # 收尾超时只记录日志，继续释放资源。
        except asyncio.TimeoutError:
            logger.warning("退出等待到期，未完成测量将标记失败并释放资源。")
        # 收尾失败时记录异常并登记故障。
        except Exception as error:
            if error is not self.failure:
                logger.exception("退出测量失败")
            self.handle_system_failure(error)

        # 标记进入资源释放阶段，停止事件交付。
        self.releasing_resources = True

        # 排空各机器事件队列，释放阻塞入队的相机交付任务。
        for machine in self.machines.values():
            machine.discard_pending_events()

        # 未完成周期按系统当前故障状态取退出原因描述。
        shutdown_reason = "程序运行失败" if self.failure is not None else "程序退出超时"

        # 逐台机器并行停止采集、取消本机任务并结算未完成周期。
        release_results = await asyncio.gather(*(
            machine.release_resources(shutdown_reason) for machine in self.machines.values()
        ), return_exceptions=True)

        # 逐条登记机器资源释放过程中的异常。
        for result in release_results:
            if isinstance(result, Exception):
                self.handle_system_failure(result)

        # 取消全部系统级后台任务。
        for task in self.worker_tasks:
            task.cancel()

        # 等待被取消的任务结束。
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)

        # 任务未启动或已异常退出时仍关闭 Modbus 客户端。
        if self.modbus_client is not None:
            try:
                await self.modbus_client.disconnect()
            except Exception as error:
                # 记录 Modbus 关闭失败并继续释放其他资源。
                logger.exception("关闭 Modbus 客户端失败")
                self.handle_system_failure(error)

        # 清空已登记的后台任务列表。
        self.worker_tasks.clear()

        # 清空不再处理的事件，释放事件携带的图片引用。
        for machine in self.machines.values():
            machine.discard_pending_events()

        # 关闭相机驱动与共享 SDK。
        try:
            if self.camera_sdk is not None:
                await run_blocking_operation(self.camera_sdk.close)
        except Exception as error:
            # 记录关闭相机驱动失败并登记故障。
            logger.exception("关闭相机驱动失败")
            self.handle_system_failure(error)
        finally:
            # 关闭本地记录库并释放实例锁。
            try:
                self.database.close()
            except Exception as error:
                # 记录关闭记录库失败并登记故障。
                logger.exception("关闭本地记录库失败")
                self.handle_system_failure(error)

            # 通知全部状态等待方本次退出已结束。
            self.state_changed.set()

            # 记录全部资源释放完成时的最终故障状态。
            logger.info("Runtime资源释放完成 failure=%s", self.failure)
