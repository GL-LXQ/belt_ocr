"""串行处理一台机器的启动、关闭和后台结果。"""

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from camera import SessionCamera
from configuration import MachineConfiguration, MeasurementConfiguration
from frequency_adapter import FrequencyAdapter
from enums import OCRState, FrequencyState, MachineState, SessionState, EventType
from mvs_sdk import CameraFrame
from models import BeltSession, MeasurementEvent, PublishEvent
from async_utils import run_blocking_operation
from text_recognition import ImageEncodingError, TextRecognizer
from database import Database, DatabaseRequest


logger = logging.getLogger(__name__)


class MachineManager:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        camera: SessionCamera,
        frequency_adapter: FrequencyAdapter,
        text_recognizer: TextRecognizer,
        database: Database,
        publish_event: PublishEvent,
        state_changed: asyncio.Event,
    ) -> None:
        """组装一台机器的唯一当前周期及其处理依赖。

        Args:
            machine: 机器与设备身份配置。
            configuration: 采集、期限和存储配置。
            camera: 当前机器的相机适配器。
            frequency_adapter: 当前机器的频率接收适配器。
            text_recognizer: 三台机器共享的 OCR 处理器。
            database: 共享存储入口。
            publish_event: 业务事件路由入口。
            state_changed: 周期状态变化通知。

        Returns:
            None  # 本机事件队列、唯一周期空位和任务引用已初始化
        """
        # 登记业务依赖和本机串行事件队列。
        self.machine = machine
        self.configuration = configuration
        self.camera = camera
        self.frequency_adapter = frequency_adapter
        self.text_recognizer = text_recognizer
        self.database = database
        self.publish_event = publish_event
        self.state_changed = state_changed
        self.queue: asyncio.Queue[MeasurementEvent] = asyncio.Queue(
            configuration.event_queue_capacity
        )
        # 初始化唯一周期、设备复位状态和后台任务引用。
        self.current_session: BeltSession | None = None
        self.waiting_cycle_reset = False
        self.deadline_tasks: dict[EventType, asyncio.Task[None]] = {}
        self.capacity_available = True
        self.initialized = False
        self.recognition_task: asyncio.Task | None = None

    @property
    def acceptance_state(self) -> str:
        """返回本机当前是否允许接收新周期。

        Args:
            无外部参数。

        Returns:
            "READY"  # 可接收新周期；其他状态表示初始化、故障、复位等待、忙碌或容量不足
        """
        if not self.initialized:
            return "INITIALIZING"
        if not self.database.runtime_available or not self.camera.available:
            return "FAULT"
        if self.waiting_cycle_reset:
            return "WAIT_CYCLE_RESET"
        if self.current_session is not None:
            return "ACTIVE"
        if not self.capacity_available or self.camera.is_capturing:
            return "DEGRADED"
        return "READY"

    async def listen_events(self) -> None:
        """持续监听本机事件队列，按顺序处理事件并反馈处理结果。

        Args:
            无外部参数。

        Returns:
            None: 持续运行直到任务被取消，无返回数据。
            返回值形式示例：
                None  # 无返回数据
        """
        while True:
            # 等待并取出本机队列中的下一个事件。
            event = await self.queue.get()
            try:
                # 处理事件后，如果有回执，而且回执还没结束，就通知等待方：处理完成了。
                await self.handle_event(event)
                if event.acknowledgement is not None:
                    if not event.acknowledgement.done():
                        event.acknowledgement.set_result(None)
            except Exception as error:
                # 记录事件处理异常，唤醒请求方并交给应用停止全部任务。
                logger.exception(
                    "业务处理失败 machine_id=%s session_id=%s event=%s",
                    event.machine_id, event.session_id, event.event_type,
                )
                if event.acknowledgement is not None and not event.acknowledgement.done():
                    event.acknowledgement.set_exception(error)
                raise
            finally:
                # 通知状态已变化，并标记当前队列任务处理结束。
                self.state_changed.set()
                self.queue.task_done()
                event = None

    async def handle_machine_start(self) -> None:
        """检查接收条件，创建本轮测量档案并启动采集窗口和超时任务。

        Args:
            无外部参数。

        Returns:
            None: 启动本轮测量，或在不满足接收条件时提前结束，无返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 忽略活动周期内的重复启动和未同步的启动。
        if self.current_session is not None:
            logger.warning("上一轮尚未结束，跳过 START machine_id=%s", self.machine.machine_id)
            return
        if self.waiting_cycle_reset:
            return

        # 检查存储容量、设备故障和本地运行库状态。
        if (
            not self.capacity_available
            or not self.database.runtime_available
            or not self.camera.available
            or self.camera.is_capturing
        ):
            # 标记等待周期复位并打印本轮未受理日志。
            self.waiting_cycle_reset = True
            logger.error("本轮未受理 machine_id=%s", self.machine.machine_id)
            return

        # 创建本轮档案，记录设备绑定、开始时间和配置。
        session = BeltSession(
            session_id=uuid4().hex,
            machine_id=self.machine.machine_id,
            camera_id=self.machine.camera_id,
            frequency_source_id=self.machine.frequency_source_id,
            capture_id=uuid4().hex,
            start_time=datetime.now(timezone.utc).isoformat(),
            capture_start_time=asyncio.get_running_loop().time(),
        )

        # 登记本机唯一的当前周期。
        self.current_session = session

        # 记录本轮开始日志。
        logger.info("开始测量 machine_id=%s session_id=%s", session.machine_id, session.session_id)

        # 启动本轮图像采集，打开对应档案的频率窗口。
        self.camera.start_capture(session.session_id, session.capture_start_time)
        self.camera.delivery_task.add_done_callback(lambda task: self.release_finished_session())
        # 登记频率接收的当前周期，新测量按接收顺序交给本轮。
        self.frequency_adapter.active_session_id = session.session_id

        # 安排本轮运行超时和 OCR 超时事件。
        self.schedule_timeout(session, EventType.CYCLE_TIMEOUT, self.configuration.max_cycle_open_ms)
        # 登记本轮 OCR 等待期限，此处只安排超时事件。
        self.schedule_timeout(session, EventType.OCR_TIMEOUT, self.configuration.ocr_result_timeout_ms)

    async def handle_machine_close(self, interrupted: bool = False, capture_stop_time: float | None = None) -> None:
        """结束本轮采集，结算频率并检查完成条件。

        Args:
            interrupted: False 表示正常 CLOSE；True 表示故障、超时或退出中断。
            capture_stop_time: 关闭信号接收时的单调时间，省略时取当前时间。

        Returns:
            None  # 本轮现场采集和频率接收已结束，OCR 与存储按各自状态继续处理
        """
        # 空闲时的关闭用于清除等待复位状态。
        session = self.current_session
        if session is None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return
        # 已经关闭的周期继续等待结果，不重复结算频率。
        if session.capture_stop_time is not None:
            if not interrupted:
                self.waiting_cycle_reset = False
            return

        # 记录本轮关闭边界，中断时等待真实关闭复位。
        session.capture_stop_time = (
            capture_stop_time if capture_stop_time is not None else asyncio.get_running_loop().time()
        )
        self.waiting_cycle_reset = interrupted
        if interrupted:
            session.errors.append("CYCLE_INTERRUPTED")

        # 清空适配器的当前周期，停止向本轮交付频率；同时封闭本轮频率列表。
        self.frequency_adapter.active_session_id = None
        session.frequency_window_sealed = True

        # 根据周期是否中断、频率是否异常以及已有读数，确定最终频率和状态。
        if interrupted or session.frequency_state == FrequencyState.FAILED:
            # 周期中断或频率已异常时，最终频率为空，已收到的明细继续保留。
            session.final_frequency = None
            session.frequency_state = FrequencyState.FAILED
        elif session.measurement_frequencies:
            # 本轮有有效读数时，取按接收顺序保存的最后一条，标记频率正常。
            session.final_frequency = session.measurement_frequencies[-1]
            session.frequency_state = FrequencyState.SUCCESS
        else:
            # 本轮没有有效读数时，标记频率异常并记录缺少测量的错误。
            session.frequency_state = FrequencyState.FAILED
            session.errors.append("FREQUENCY_NO_VALID_MEASUREMENT")

        # 停止本轮采集，当前周期继续占用机器直到保存或失败清理完成。
        await self.camera.seal_capture()

        # 从任务表移除本轮 CycleTimeout；任务仍存在时，取消它后续的超时通知。
        deadline_task = self.deadline_tasks.pop(EventType.CYCLE_TIMEOUT, None)
        if deadline_task is not None:
            deadline_task.cancel()

        # 判断本轮是否已失败，关闭后移除保留的现场周期身份。
        if session.state == SessionState.FAILED:
            self.release_finished_session()
            return

        # 判断本轮是否中断，中断只打印日志并清理资源。
        if interrupted:
            await self.handle_measurement_failure(session)
            return

        # 检查正常关闭周期的结果，条件满足时提交数据库。
        await self.try_finalize(session)

    async def handle_event(self, event: MeasurementEvent) -> None:
        """按事件类型处理机器和测量周期业务，并检查本轮是否完成。

        Args:
            event: 待处理的测量事件，包含事件身份、来源信息和业务数据。

        Returns:
            None: 完成业务处理，或隔离事件后提前结束。
            返回示例：
                None  # 无返回数据
        """
        # 分派不依赖测量档案的机器级事件。
        match event.event_type:
            case EventType.MACHINE_STARTED:
                await self.handle_machine_start()
                return
            case EventType.MACHINE_CLOSED:
                # 旧周期关闭事件不能操作当前周期。
                if (
                    self.current_session is not None
                    and event.session_id is not None
                    and event.session_id != self.current_session.session_id
                ):
                    await run_blocking_operation(
                        self.database.save_abnormal_event, "CLOSE_SESSION_MISMATCH", event,
                    )
                    return
                await self.handle_machine_close(capture_stop_time=event.received_monotonic)
                return
            case EventType.SHUTDOWN:
                await self.handle_machine_close(interrupted=True)
                return
            case EventType.MACHINE_SYNCHRONIZED:
                # 中断原活动周期，更新机器复位状态。
                if self.current_session is not None:
                    await self.handle_machine_close(interrupted=True)
                self.waiting_cycle_reset = event.payload != MachineState.CLOSED
                return
            case EventType.CAPACITY_CHANGED:
                self.capacity_available = event.payload
                return

        # 隔离没有周期身份的频率，不分配给当前或历史 Session。
        if event.event_type == EventType.FREQUENCY_MEASURED and not event.session_id:
            await run_blocking_operation(
                self.database.save_abnormal_event, "AMBIGUOUS_MEASUREMENT", event,
            )
            return

        # 只接收当前周期的结果，忽略已经清理的旧结果。
        session = self.current_session
        if session is None or event.session_id != session.session_id:
            logger.warning(
                "隔离未知或已结算事件 machine_id=%s session_id=%s event=%s",
                event.machine_id,
                event.session_id,
                event.event_type,
            )
            return

        # 处理数据库提交回调。
        match event.event_type:
            case EventType.COMMIT_SUCCEEDED | EventType.COMMIT_FAILED:
                await self.handle_commit_result(session, event)
                return

        # 判断活动周期是否超过关闭期限，超时后进入机器复位流程。
        if event.event_type == EventType.CYCLE_TIMEOUT:
            if session.capture_stop_time is None:
                session.errors.append("CYCLE_TIMEOUT")
                await self.handle_machine_close(interrupted=True)
            return

        # 判断本轮是否仍在处理，丢弃失败或等待入库后的迟到结果。
        if session.state != SessionState.RUNNING:
            logger.warning(
                "忽略迟到结果 session_id=%s state=%s event=%s",
                session.session_id,
                session.state.value,
                event.event_type,
            )
            return

        # 分派采集结果，保留各事件是否继续结算的处理决定。
        should_finalize = True
        match event.event_type:
            case EventType.CAPTURE_COMPLETED | EventType.CAPTURE_FAILED:
                # 仅接收等待阶段的采集结果，保留整轮统计。
                if session.ocr_state != OCRState.WAITING:
                    return
                capture_result = event.payload
                session.capture_summary = capture_result.statistics
                if capture_result.errors:
                    session.ocr_state = OCRState.FAILED
                    session.errors.extend(capture_result.errors)
                else:
                    # 启动一个整轮后台任务并登记完成回调。
                    session.ocr_state = OCRState.RUNNING
                    task = asyncio.create_task(self.recognize_session(session, capture_result.frames))
                    self.recognition_task = task
                    task.add_done_callback(self.handle_recognition_task_finished)
                    return
            case EventType.OCR_COMPLETED:
                # 只接收正在处理周期的一次最终结果。
                if session.ocr_state != OCRState.RUNNING:
                    return
                session.ocr_result = event.payload
                session.ocr_state = OCRState.SUCCESS
                # 撤销已经成功周期的 OCR 超时通知。
                deadline = self.deadline_tasks.pop(EventType.OCR_TIMEOUT, None)
                if deadline is not None:
                    deadline.cancel()
            case EventType.OCR_FAILED | EventType.OCR_TIMEOUT:
                # 忽略已有终态，登记处理失败或超时。
                if session.ocr_state not in {OCRState.WAITING, OCRState.RUNNING}:
                    return
                session.ocr_state = OCRState.TIMED_OUT if event.event_type == EventType.OCR_TIMEOUT else OCRState.FAILED
                session.errors.append(event.payload or "OCR_TIMEOUT")
            case EventType.FREQUENCY_MEASURED:
                should_finalize = await self.handle_frequency_measured(session, event)
            case EventType.FREQUENCY_FAILED:
                # 登记本轮频率故障，保留明细但不确认最终频率。
                if session.frequency_window_sealed:
                    return
                session.frequency_state = FrequencyState.FAILED
                session.final_frequency = None
                session.errors.append(event.payload)
            case _:
                await run_blocking_operation(
                    self.database.save_abnormal_event, "UNKNOWN_EVENT_TYPE", event,
                )

        # 已忽略的事件不继续处理本轮结果。
        if not should_finalize:
            return

        # 统一处理本轮失败或满足条件后的提交。
        await self.try_finalize(session)

    async def handle_commit_result(self, session: BeltSession, event: MeasurementEvent) -> None:
        """处理提交成功或失败回调，更新原测量档案的提交状态。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            None: 更新业务状态，不返回数据。
            返回示例：
                None  # 无返回数据
        """
        # 判断本轮是否正在等待数据库结果。
        if session.state != SessionState.WAITING_COMMIT_DB:
            return

        # 判断提交是否成功，标记已入库并移除本轮档案。
        if event.event_type == EventType.COMMIT_SUCCEEDED:
            session.state = SessionState.COMMITTED
            logger.info(
                "已保存 machine_id=%s session_id=%s",
                session.machine_id,
                session.session_id,
            )
            self.release_finished_session()
        else:
            # 登记提交失败原因，打印日志并清理本轮档案。
            session.errors.append(event.payload["error_code"])
            await self.handle_measurement_failure(session)

    async def handle_measurement_failure(self, session: BeltSession) -> None:
        """标记本轮失败并清理资源，保留尚未关闭的现场周期身份。

        Args:
            session: 处理失败、中断或提交失败的测量档案。

        Returns:
            None  # 已打印失败日志并清理资源，活动周期保留至 CLOSE 或中断
        """
        # 标记本轮失败并打印机器、周期和错误明细。
        session.state = SessionState.FAILED
        session.finish_time = datetime.now(timezone.utc).isoformat()
        logger.error(
            "测量失败 machine_id=%s session_id=%s errors=%s",
            session.machine_id,
            session.session_id,
            session.errors,
        )

        # 取消等待或正在执行的识别任务，后台线程结束后自行释放锁。
        recognition_task = self.recognition_task
        if recognition_task is not None:
            recognition_task.cancel()
        session.ocr_result = None
        session.frozen_payload = None
        session.payload_hash = None

        # 取消处理期限，未关闭的失败周期继续等待真实 CLOSE。
        for event_type in tuple(self.deadline_tasks):
            if session.capture_stop_time is None and event_type == EventType.CYCLE_TIMEOUT:
                continue
            self.deadline_tasks.pop(event_type).cancel()

        # 停止设备交付，已关闭周期在后台资源释放后清空。
        self.frequency_adapter.active_session_id = None
        session.frequency_window_sealed = True
        if session.frequency_state == FrequencyState.RUNNING:
            session.frequency_state = FrequencyState.FAILED
        await self.camera.seal_capture()
        self.release_finished_session()

    def release_finished_session(self) -> None:
        """在周期关闭且后台资源释放后清空唯一的当前周期。

        Args:
            无外部参数。

        Returns:
            None  # 条件满足时清空周期并通知等待方，否则继续等待
        """
        # 仅释放已经关闭且已经成功或失败的周期。
        session = self.current_session
        if session is None or session.capture_stop_time is None:
            return
        if session.state not in {SessionState.COMMITTED, SessionState.FAILED}:
            return
        # 等待实际工作结束，避免下一轮与取消中的线程重叠。
        if self.recognition_task is not None:
            return
        if self.camera.delivery_task is not None:
            return
        # 释放最终结果和期限任务，恢复当前周期空位。
        session.ocr_result = None
        for task in self.deadline_tasks.values():
            task.cancel()
        self.deadline_tasks.clear()
        self.current_session = None
        self.state_changed.set()

    def handle_recognition_task_finished(self, task: asyncio.Task) -> None:
        """释放本轮识别任务，报告交付异常并尝试结束当前周期。

        Args:
            task: 已结束或取消的整轮识别任务。

        Returns:
            None  # 任务引用已释放，已关闭的结束周期已清理
        """
        # 读取后台交付异常，取消不作为设备故障。
        self.recognition_task = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.camera.report_failure(error, f"OCR 结果交付 machine_id={self.machine.machine_id}")
        # 线程实际结束后，失败周期才允许释放当前空位。
        self.release_finished_session()

    async def recognize_session(self, session: BeltSession, frames: tuple[CameraFrame, ...]) -> None:
        """等待共享锁并执行整轮 OCR，向所属周期交付一次结果。

        Args:
            session: 原测量周期，任务执行前检查其状态。
            frames: 本轮全部原始帧。

        Returns:
            None  # 最终结果或失败原因通过事件交付
        """
        # 等待共享锁，失效周期不再调用编码和模型。
        async with self.text_recognizer.processing_lock:
            if session.state != SessionState.RUNNING:
                return
            try:
                result = await run_blocking_operation(
                    self.text_recognizer.process_session_frames,
                    session.session_id,
                    session.capture_id,
                    session.camera_id,
                    frames,
                    self.camera.device.encode_image,
                )
            except Exception as error:
                logger.exception("OCR 处理失败 session_id=%s", session.session_id)
                if isinstance(error, ImageEncodingError):
                    self.camera.report_failure(
                        error.__cause__,
                        f"相机编码 machine_id={session.machine_id} camera_id={session.camera_id}",
                    )
                event_type, payload = EventType.OCR_FAILED, str(error)
            else:
                event_type, payload = EventType.OCR_COMPLETED, result
        # 释放原始帧后只交付仍然有效周期的结果。
        frames = ()
        if session.state == SessionState.RUNNING:
            await self.publish_event(MeasurementEvent(event_type, session.machine_id, session.session_id, payload))

    async def handle_frequency_measured(self, session: BeltSession, event: MeasurementEvent) -> bool:
        """按接收顺序保存黑盒交付的新有效测量。

        Args:
            session: 事件所属的测量档案。
            event: 包含事件类型、机器编号、测量编号和数据的业务事件。

        Returns:
            bool: 是否继续执行本轮完成检查。
            返回示例：
                True  # 继续检查本轮能否结算
                False  # 忽略当前事件，不执行完成检查
        """
        # 终态后到达的测量只记录异常信息，不修改已封闭列表。
        if session.frequency_window_sealed:
            await run_blocking_operation(
                self.database.save_abnormal_event, "LATE_FREQUENCY", event,
            )
            return False

        # 按机器事件队列的接收顺序追加明细，不重复检查黑盒保证的数据约束。
        session.measurement_frequencies.append(event.payload)
        return True

    async def try_finalize(self, session: BeltSession) -> None:
        """检查本轮结果，失败时清理，正常结果冻结后提交数据库。

        Args:
            session: 待检查完成条件的本轮测量档案。

        Returns:
            None  # 条件不足时继续等待，否则冻结并提交本轮记录
        """

        # 判断本轮是否仍在处理。
        if session.state != SessionState.RUNNING:
            return

        # 判断 OCR 或频率是否失败，失败时只打印日志并清理资源。
        if (
            session.ocr_state in {OCRState.FAILED, OCRState.TIMED_OUT}
            or session.frequency_state == FrequencyState.FAILED
        ):
            await self.handle_measurement_failure(session)
            return

        # 正常关闭并且 OCR、频率均成功后，才准备提交。
        if (
            not session.frequency_window_sealed
            or session.capture_stop_time is None
            or session.ocr_state != OCRState.SUCCESS
            or session.frequency_state != FrequencyState.SUCCESS
        ):
            return

        # 记录结算时间，获取最终频率和 OCR 结果。
        session.finish_time = datetime.now(timezone.utc).isoformat()
        final_frequency = session.final_frequency
        ocr_result = session.ocr_result
        # 按机器、周期和帧编号生成最终图片路径。
        evidence_directory = self.configuration.evidence_directory / session.machine_id / session.session_id
        image_paths = {
            frame.frame_id: str(evidence_directory / f"{frame.frame_id}.bmp")
            for frame in ocr_result.selected_frames
        }
        # 组装本轮身份、测量结果、采集统计和配置数据。
        payload = {
            "session_id": session.session_id,
            "machine_id": session.machine_id,
            "camera_id": session.camera_id,
            "frequency_source_id": session.frequency_source_id,
            "start_time": session.start_time,
            "finish_time": session.finish_time,
            "ordered_lines": list(ocr_result.ordered_lines),
            "evidence_refs": list(image_paths.values()),
            "line_evidence_refs": [
                [image_paths[frame_id] for frame_id in frame_ids]
                for frame_ids in ocr_result.line_frame_ids
            ],
            "final_frequency_hz": final_frequency.value_hz,
            "measurement_frequencies": [
                asdict(measurement)
                for measurement in session.measurement_frequencies
            ],
            "capture_summary": session.capture_summary,
            "configuration_version": self.configuration.configuration_version,
        }

        # 冻结提交内容并计算内容哈希。
        session.frozen_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.payload_hash = hashlib.sha256(
            session.frozen_payload.encode()
        ).hexdigest()
        # 撤销本轮剩余期限任务。
        for task in self.deadline_tasks.values():
            task.cancel()
        self.deadline_tasks.clear()
        # 提交本轮冻结记录。
        await self.submit_frozen_record(session)

    async def submit_frozen_record(self, session: BeltSession) -> None:
        """提交本轮正常结果，提交异常或队列满时结束本轮任务。

        Args:
            session: 已冻结正常结果的测量档案。

        Returns:
            None  # 本轮等待数据库回调，或已标记失败并完成清理
        """
        # 标记等待入库，组装本轮冻结记录。
        session.state = SessionState.WAITING_COMMIT_DB
        request = DatabaseRequest(
            session.machine_id, session.session_id,
            session.frozen_payload, session.payload_hash, session.ocr_result.selected_frames,
        )

        # 将最终图片所有权交给提交请求，Session 不再保留图片。
        session.ocr_result = None

        # 提交存储队列，登记入队异常或容量不足。
        try:
            accepted = await self.database.submit(request)
        except Exception:
            logger.exception("提交异常 session_id=%s", session.session_id)
            session.errors.append("DATABASE_SUBMIT_FAILED")
        else:
            if accepted:
                return
            session.errors.append("DATABASE_QUEUE_FULL")

        # 打印失败日志并清理本轮档案。
        await self.handle_measurement_failure(session)

    def schedule_timeout(self, session: BeltSession, event_type: EventType, timeout_ms: int) -> None:
        """为当前周期安排指定类型的期限通知。

        Args:
            session: 当前测量周期。
            event_type: 周期关闭或 OCR 超时事件类型。
            timeout_ms: 等待毫秒数。

        Returns:
            None  # 期限任务已按事件类型登记
        """
        async def publish_timeout() -> None:
            """等待期限并交付携带原周期身份的超时事件。

            Args:
                无外部参数。

            Returns:
                None  # 超时事件已交付，或任务被提前取消
            """
            await asyncio.sleep(timeout_ms / 1000)
            await self.publish_event(MeasurementEvent(
                event_type, session.machine_id, session.session_id,
            ))

        # 保存本轮该类型的唯一期限任务。
        self.deadline_tasks[event_type] = asyncio.create_task(
            publish_timeout()
        )
