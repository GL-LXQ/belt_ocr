"""从文件夹循环读取图片，模拟每台相机的独立取流。"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from configuration import MachineConfiguration, MeasurementConfiguration
from recovery import run_blocking_operation
from models import CapturedFrame, MeasurementEvent, PublishEvent


logger = logging.getLogger(__name__)


def save_evidence_image(source_path: Path, image_path: Path) -> None:
    """保存并同步图片副本，再原子发布最终证据文件。"""
    temporary_path = image_path.with_suffix(image_path.suffix + ".partial")
    try:
        with source_path.open("rb") as source_file:
            with temporary_path.open("wb") as evidence_file:
                shutil.copyfileobj(source_file, evidence_file)
                evidence_file.flush()
                os.fsync(evidence_file.fileno())
        if temporary_path.stat().st_size == 0:
            raise ValueError("模拟图片为空。")
        os.replace(temporary_path, image_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@dataclass
class CaptureWindow:
    session_id: str
    capture_id: str
    start_boundary: float
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)


class FolderCamera:
    def __init__(
        self,
        machine: MachineConfiguration,
        configuration: MeasurementConfiguration,
        publish_event: PublishEvent,
    ) -> None:
        self.machine = machine
        self.configuration = configuration
        self.publish_event = publish_event
        self.windows: dict[str, CaptureWindow] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def start_capture(
        self, session_id: str, capture_id: str, start_boundary: float
    ) -> None:
        """登记采集窗口并启动独立取流任务。"""
        window = CaptureWindow(session_id, capture_id, start_boundary)
        self.windows[capture_id] = window
        task = asyncio.create_task(self.capture_images(window))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def seal_capture(self, capture_id: str) -> None:
        """通知指定采集窗口结束取流。"""
        window = self.windows.get(capture_id)
        if window is not None:
            window.stop_requested.set()

    async def capture_images(self, window: CaptureWindow) -> None:
        """读取图片、保存本轮副本并按顺序发布帧和封口事件。"""
        selected_count = 0
        skipped_count = 0
        try:
            # 获取文件夹中的模拟图像列表。
            image_extensions = {
                ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".ppm",
            }
            image_paths = await run_blocking_operation(
                lambda: sorted(
                    image_path for image_path in self.machine.image_directory.iterdir()
                    if image_path.is_file()
                    and image_path.suffix.lower() in image_extensions
                )
            )
            if not image_paths:
                raise ValueError("模拟图片文件夹中没有图片。")

            # 建立本轮证据目录和采集截止时间。
            evidence_directory = (
                self.configuration.evidence_directory / window.session_id
            )
            await run_blocking_operation(
                evidence_directory.mkdir, parents=True, exist_ok=True,
            )
            event_loop = asyncio.get_running_loop()
            deadline = (
                window.start_boundary + self.configuration.capture_window_ms / 1000
            )
            frame_number = 0

            # 按固定间隔取帧，超过选帧上限时记录跳帧数量。
            while not window.stop_requested.is_set() and event_loop.time() < deadline:
                captured_monotonic = event_loop.time()
                captured_at = datetime.now(timezone.utc).isoformat()
                source_path = image_paths[frame_number % len(image_paths)]
                frame_number += 1
                if selected_count < self.configuration.max_frames_per_session:
                    frame_id = f"{window.capture_id}-{frame_number}"
                    image_path = evidence_directory / f"{frame_id}{source_path.suffix}"
                    await run_blocking_operation(
                        save_evidence_image, source_path, image_path,
                    )

                    # 发布带固定归属的帧，随后继续取流。
                    frame = CapturedFrame(
                        window.session_id, window.capture_id, self.machine.camera_id,
                        frame_id, captured_at, captured_monotonic, str(image_path),
                        window.capture_id, datetime.now(timezone.utc).isoformat(),
                    )
                    await self.publish_event(MeasurementEvent(
                        "FrameSelected", self.machine.machine_id,
                        window.session_id, frame,
                    ))
                    selected_count += 1
                else:
                    skipped_count += 1

                # 等待下一帧或本轮提前关闭。
                remaining_seconds = deadline - event_loop.time()
                if remaining_seconds <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        window.stop_requested.wait(),
                        min(
                            self.configuration.frame_interval_ms / 1000,
                            remaining_seconds,
                        ),
                    )
                except asyncio.TimeoutError:
                    pass
        except Exception:
            logger.exception(
                "模拟取流失败 machine_id=%s session_id=%s",
                self.machine.machine_id, window.session_id,
            )
            await self.publish_event(MeasurementEvent(
                "CaptureFailed", self.machine.machine_id, window.session_id,
                "CAPTURE_FAILED",
            ))
        finally:
            # 清理本轮采集窗口。
            self.windows.pop(window.capture_id, None)

        # 在所有帧发布后发送封口事件。
        await self.publish_event(MeasurementEvent(
            "CaptureSealed", self.machine.machine_id, window.session_id, skipped_count,
        ))
