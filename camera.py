"""将 MVS 流式采集接入 Session、证据保存和业务事件。"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from configuration import MachineConfiguration, MeasurementConfiguration
from models import CapturedFrame, CaptureSummary, MeasurementEvent, PublishEvent
from mvs_capture import CaptureFrame, CaptureTask, start_capture
from mvs_sdk import MvsCamera


logger = logging.getLogger(__name__)


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
    start_boundary: float
    close_boundary: float | None = None
    task: CaptureTask | None = None
    selected_count: int = 0
    skipped_count: int = 0


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

    def start_capture(self, session_id: str, capture_id: str, start_boundary: float) -> None:
        """创建独立采集窗口，启动后台取流和逐帧处理，并安排采集封口。

        Args:
            session_id: 本轮测量编号，用于关联图片事件和证据目录。
            capture_id: 本轮采集编号，用于登记窗口和关联采集结果。
            start_boundary: 本轮业务启动的主机单调时间，单位为秒，用于筛选窗口内的帧。

        Returns:
            None  # 无返回数据；后台采集和异步收尾已安排，不等待图片保存或 OCR
        """
        # 保存当前事件循环，供消费线程投递图片事件。
        self.event_loop = asyncio.get_running_loop()

        # 创建本轮窗口，记录测量编号、采集编号和业务开始时间。
        window = CaptureWindow(session_id, capture_id, start_boundary)

        # 创建独立有界队列并启动后台采集，将采集任务保存到本轮窗口。
        window.task = start_capture(
            self.device,
            # 登记逐帧处理回调，消费时携带本轮窗口筛选图片、保存证据和投递事件。
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

    def process_frame(self, window: CaptureWindow, frame: CaptureFrame) -> None:
        """筛选本轮图像、保存证据并送回业务事件队列。

        Args:
            window: 本轮窗口和选帧统计。
            frame: 具有独立内存及本轮身份的相机帧。

        Returns:
            None  # 证据和 FrameSelected 已交付，跳过帧仅累计数量
        """
        # 读取本帧接收时间，计算本轮业务采集截止时间。
        received_time = frame.image.received_monotonic
        deadline = window.start_boundary + self.configuration.capture_window_ms / 1000
        # 跳过超出采集或关闭边界的帧，以及超过选帧上限的帧。
        if (
            received_time > deadline
            or (window.close_boundary is not None and received_time > window.close_boundary)
            or window.selected_count >= self.configuration.max_frames_per_session
        ):
            window.skipped_count += 1
            return

        # 用采集编号和 SDK 帧号命名图片，并将独立图像编码为 BMP。
        frame_id = f"{window.capture_id}-{frame.image.frame_number}"
        extension, image_data = self.device.encode_image(frame.image)
        # 将图片保存到本轮 Session 的证据目录。
        image_path = self.configuration.evidence_directory / window.session_id / f"{frame_id}{extension}"
        save_evidence_image(image_data, image_path)
        # 将主机接收时间换算为 UTC 时间，整理图片身份和证据路径。
        captured_at = datetime.now(timezone.utc) - timedelta(seconds=time.monotonic() - received_time)
        captured_frame = CapturedFrame(
            session_id=window.session_id,
            capture_id=window.capture_id,
            camera_id=self.machine.camera_id,
            frame_id=frame_id,
            captured_at=captured_at.isoformat(),
            captured_monotonic=received_time,
            image_path=str(image_path),
            source_epoch=window.capture_id,
            received_at=datetime.now(timezone.utc).isoformat(),
        )

        # 从消费线程投递事件，并等待事件进入本机业务队列。
        event = MeasurementEvent("FrameSelected", self.machine.machine_id, window.session_id, captured_frame)
        asyncio.run_coroutine_threadsafe(self.publish_event(event), self.event_loop).result()
        # 图片保存且事件入队后，累计本轮成功选中数量。
        window.selected_count += 1

    async def seal_capture(self, capture_id: str, close_boundary: float | None = None) -> None:
        """停止指定窗口的生产，等待抓帧退出后释放现场采集位置。

        Args:
            capture_id: 需要停止的采集编号。
            close_boundary: 业务关闭的单调时间，省略时使用当前时间。

        Returns:
            None  # 本轮已停止入队，消费者可能仍在保存证据
        """
        # 查找指定采集窗口，已移除的窗口直接结束。
        window = self.windows.get(capture_id)
        if window is None:
            return
        # 固定关闭边界，停止本轮生产并等待最后入队完成。
        if window.close_boundary is None:
            window.close_boundary = close_boundary if close_boundary is not None else time.monotonic()
        # 发出提前停止通知，唤醒控制线程执行停止取流，并通知取帧线程结束。
        window.task.stop_requested.set()
        # 等待最后取帧和入队结束；此时消费者仍可继续保存队列里的图片。
        await asyncio.shield(asyncio.wrap_future(window.task.acquisition_future))

    async def finish_capture(self, window: CaptureWindow) -> None:
        """等待消费结束，保存本轮统计并发布最后的封口事件。

        Args:
            window: 待收尾的采集窗口。

        Returns:
            None  # 统计和封口已发布，本轮窗口已移除
        """
        # 异步等待生产和消费全部结束，取得本轮最终采集结果。
        result = await asyncio.shield(asyncio.wrap_future(window.task.completion_future))
        # 整理采集和证据交付统计，保留处理失败信息。
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
            logger.error("相机采集或证据保存失败 machine_id=%s statistics=%s", self.machine.machine_id, statistics)
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
        """请求所有采集任务停止，并等待证据和事件交付结束。

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
