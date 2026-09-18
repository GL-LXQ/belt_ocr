"""将 MVS 流式采集接入 Session、内存图片和业务事件。"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from configuration import MachineConfiguration, MeasurementConfiguration
from models import CapturedFrame, CaptureSummary, MeasurementEvent, PublishEvent
from mvs_capture import CaptureFrame, CaptureTask, start_capture
from mvs_sdk import MvsCamera


logger = logging.getLogger(__name__)
FRAME_BATCH_SIZE = 8


def save_evidence_image(image_data: bytes, image_path: Path) -> None:
    """将编码后的图片同步写盘并原子发布。

    Args:
        image_data: 完整图片文件字节。
        image_path: 本轮证据文件路径。

    Returns:
        None  # 图片已保存，失败时抛出文件操作异常
    """
    # 为本帧生成临时文件路径。
    temporary_path = image_path.with_suffix(image_path.suffix + ".partial")
    try:
        # 将本帧内容写入临时文件并同步到磁盘。
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


@dataclass
class CaptureWindow:
    """记录单轮采集边界与任务。"""

    session_id: str
    capture_id: str
    capture_start_time: float  # 本轮相机采集图片的起始时间，使用 START 受理时间，单调时钟秒数
    capture_stop_time: float | None = None  # 本轮相机采集图片的停止截止时间，未请求停止时为 None，单调时钟秒数
    task: CaptureTask | None = None
    selected_count: int = 0
    skipped_count: int = 0
    pending_frames: list[CapturedFrame] = field(default_factory=list)


class SessionCamera:
    """把真实相机帧转换成属于指定 Session 的图片事件。"""

    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
    ) -> None:
        """登记设备绑定、事件入口和本轮任务集合。

        Args:
            machine: 当前机器及相机配置。
            configuration: 采集窗口、队列和证据保存配置。
            publish_event: 应用事件发布入口。

        Returns:
            None  # 相机适配器已创建，设备由 App.start 打开
        """
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event
        self.device: MvsCamera | None = None
        self.windows: dict[str, CaptureWindow] = {}
        self.tasks: set[asyncio.Task] = set()
        self.event_loop = None

    @property
    def available(self) -> bool:
        """返回设备是否已打开且未发生取流故障。

        Args:
            无外部参数。

        Returns:
            True  # 可采集；不可用时返回 False
        """
        return self.device is not None and not self.device.closed and not self.device.faulted

    @property
    def is_capturing(self) -> bool:
        """返回设备是否仍被现场采集占用。

        Args:
            无外部参数。

        Returns:
            True  # 仍在采集；空闲时返回 False
        """
        return self.device is not None and self.device.capture_lock.locked()

    def start_capture(self, session_id: str, capture_id: str, capture_start_time: float) -> None:
        """创建独立采集窗口，启动后台取流和逐帧处理，并安排采集封口。

        Args:
            session_id: 本轮测量编号，用于关联图片事件和证据目录。
            capture_id: 本轮采集编号，用于登记窗口和关联采集结果。
            capture_start_time: 本轮相机采集图片的起始时间，受理 START 时记录的主机单调时钟秒数。

        Returns:
            None  # 无返回数据；后台采集和异步收尾已安排，不等待图片保存或 OCR
        """
        # 保存当前事件循环，供消费线程投递图片事件。
        self.event_loop = asyncio.get_running_loop()

        # 创建本轮窗口，记录测量编号、采集编号和业务开始时间。
        window = CaptureWindow(session_id, capture_id, capture_start_time)

        # 创建独立有界队列并启动后台采集，将采集任务保存到本轮窗口。
        window.task = start_capture(
            self.device,
            # 登记逐帧处理回调，消费时携带本轮窗口筛选图片、编码图片和投递事件。
            lambda frame: self.process_frame(window, frame),
            session_id,
            # 将采集时长换算为秒，传入队列容量、单次取帧超时和采集编号。
            duration_seconds=self.configuration.capture_window_ms / 1000,
            queue_capacity=self.configuration.camera_queue_capacity,
            timeout_ms=self.configuration.camera_timeout_ms,
            capture_id=capture_id,
        )
        # 按采集编号登记窗口，供关闭信号查找本轮任务。
        self.windows[capture_id] = window

        # 安排异步收尾，等待本轮生产和消费全部结束后发布 CaptureSealed。
        task = asyncio.create_task(self.finish_capture(window))

        # 保存收尾任务供退出时统一等待，并在任务结束后自动移除。
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def is_frame_qualified(self, frame: CaptureFrame) -> bool:
        """预留无字、纯黑和截断帧筛选入口，当前放行所有帧。

        Args:
            frame: 待筛选的本轮相机帧。

        Returns:
            True  # 当前所有输入帧均合格
        """
        return True

    def process_frame(self, window: CaptureWindow, frame: CaptureFrame) -> None:
        """筛选本轮图像、保留内存 BMP，并在满八帧时交付批次。

        Args:
            window: 本轮窗口和选帧统计。
            frame: 具有独立内存及本轮身份的相机帧。

        Returns:
            None  # 合格图片已在内存中组批，满批已交付，跳过帧仅累计数量
        """
        # 读取本帧接收时间，计算本轮业务采集截止时间。
        received_time = frame.image.received_monotonic
        deadline = window.capture_start_time + self.configuration.capture_window_ms / 1000
        # 跳过超出采集或关闭边界的帧，以及超过选帧上限的帧。
        if (
            received_time > deadline
            or (window.capture_stop_time is not None and received_time > window.capture_stop_time)
            or window.selected_count >= self.configuration.max_frames_per_session
        ):
            window.skipped_count += 1
            return

        # 调用图像筛选入口，无字、纯黑、截断不合格帧只累计跳过数量。
        if not self.is_frame_qualified(frame):
            window.skipped_count += 1
            return

        # 用采集编号和 SDK 帧号命名图片，并将独立图像编码为 BMP。
        frame_id = f"{window.capture_id}-{frame.image.frame_number}"
        image_extension, image_data = self.device.encode_image(frame.image)
        # 将主机接收时间换算为 UTC 时间，整理图片身份和内存数据。
        captured_at = datetime.now(timezone.utc) - timedelta(seconds=time.monotonic() - received_time)
        captured_frame = CapturedFrame(
            session_id=window.session_id,
            capture_id=window.capture_id,
            camera_id=self.machine.camera_id,
            frame_id=frame_id,
            captured_at=captured_at.isoformat(),
            captured_monotonic=received_time,
            image_data=image_data,
        )

        # 将编码成功的合格帧加入本轮批次，并累计选中数量。
        window.pending_frames.append(captured_frame)
        window.selected_count += 1

        # 满八帧后从消费线程交付独立批次，交付完成后清空待组批列表。
        if len(window.pending_frames) == FRAME_BATCH_SIZE:
            event = MeasurementEvent(
                "FrameBatchSelected",
                self.machine.machine_id,
                window.session_id,
                tuple(window.pending_frames),
            )
            asyncio.run_coroutine_threadsafe(self.publish_event(event), self.event_loop).result()
            window.pending_frames.clear()

    async def seal_capture(self, capture_id: str, capture_stop_time: float | None = None) -> None:
        """停止指定窗口的生产，等待抓帧退出后释放现场采集位置。

        Args:
            capture_id: 需要停止的采集编号。
            capture_stop_time: 本轮相机采集图片的停止截止时间，主机单调时钟秒数；省略时使用当前时间。

        Returns:
            None  # 本轮已停止入队，消费者可能仍在编码图片
        """
        # 查找指定采集窗口，已移除的窗口直接结束。
        window = self.windows.get(capture_id)
        if window is None:
            return
        # 记录本轮相机采集图片的停止截止时间。
        if window.capture_stop_time is None:
            window.capture_stop_time = capture_stop_time if capture_stop_time is not None else time.monotonic()
        # 发出提前停止通知，唤醒控制线程执行停止取流，并通知取帧线程结束。
        window.task.stop_requested.set()
        # 等待最后取帧和入队结束；此时消费者仍可继续编码队列里的图片。
        await asyncio.shield(asyncio.wrap_future(window.task.acquisition_future))

    async def finish_capture(self, window: CaptureWindow) -> None:
        """等待消费结束，交付尾批、整理统计并发布封口事件。

        Args:
            window: 待收尾的采集窗口。

        Returns:
            None  # 尾批、统计和封口已交付，本轮窗口已移除
        """
        # 异步等待生产和消费全部结束，取得本轮最终采集结果。
        result = await asyncio.shield(asyncio.wrap_future(window.task.completion_future))
        # 消费结束后交付不足八帧的尾批，空批次不发送事件。
        if window.pending_frames:
            event = MeasurementEvent(
                "FrameBatchSelected",
                self.machine.machine_id,
                window.session_id,
                tuple(window.pending_frames),
            )
            await self.publish_event(event)
            window.pending_frames.clear()

        # 整理采集和图片交付统计，保留处理失败信息。
        statistics = {
            "capture_duration_seconds": result.capture_duration_seconds,
            "received_frame_count": result.received_frame_count,
            "enqueued_frame_count": result.enqueued_frame_count,
            "dropped_frame_count": result.dropped_frame_count,
            "processed_frame_count": result.processed_frame_count,
            "failed_frame_count": result.failed_frame_count,
            "selected_frame_count": window.selected_count,
            "camera_stopped": result.camera_stopped,
            "has_error": result.has_error,
            "capture_errors": list(result.capture_errors),
            "processing_errors": [frame.error for frame in result.frame_results if frame.error],
        }
        # 记录本轮采集或逐帧处理异常。
        if result.has_error:
            logger.error("相机采集或图片编码失败 machine_id=%s statistics=%s", self.machine.machine_id, statistics)
        # 合并业务跳过与队满丢帧数量，生成封口摘要。
        summary = CaptureSummary(
            capture_id=window.capture_id,
            skipped_frame_count=window.skipped_count + result.dropped_frame_count,
            statistics=statistics,
            errors=("CAPTURE_FAILED",) if result.has_error else (),
        )

        # 所有帧事件已入队后发布封口，并移除本轮窗口。
        await self.publish_event(MeasurementEvent(
            "CaptureSealed", self.machine.machine_id, window.session_id, summary,
        ))
        # 封口事件交付后，移除本轮窗口。
        self.windows.pop(window.capture_id, None)

    async def stop(self) -> None:
        """请求所有采集任务停止，并等待图片和事件交付结束。

        Args:
            无外部参数。

        Returns:
            None  # 所有相机任务已结束，可释放设备和 SDK
        """
        # 一次性通知所有尚未结束的窗口停止生产。
        for window in tuple(self.windows.values()):
            window.task.stop_requested.set()
        # 保持事件循环可用，等待各消费者完成最后的事件投递。
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks))
