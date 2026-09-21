"""按整轮顺序执行图片编码、筛选、识别和文字图片终选。"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from models import CapturedFrame, OCRResult
from mvs_sdk import CameraFrame


logger = logging.getLogger(__name__)


class ImageEncodingError(RuntimeError):
    """标记相机编码失败并保留原始异常。"""


class TextRecognizer:
    """提供共享串行处理锁与无业务状态的 OCR 主流程。"""

    def __init__(self) -> None:
        """创建三台机器共用的整轮处理锁。

        Args:
            无外部参数。

        Returns:
            None  # 处理锁已创建
        """
        self.processing_lock = asyncio.Lock()

    def process_session_frames(
        self,
        session_id: str,
        capture_id: str,
        camera_serial: str,
        frames: tuple[CameraFrame, ...],
        encode_image: Callable[[CameraFrame], bytes],
    ) -> OCRResult:
        """将整轮原始帧顺序处理为最终文字和对应内存图片。

        Args:
            session_id: 测量周期编号。
            capture_id: 采集编号。
            camera_serial: 相机序列号。
            frames: 本轮全部原始帧，按接收顺序排列。
            encode_image: 将原始帧编码为内存 BMP 的相机接口。

        Returns:
            OCRResult(
                ordered_lines=("ABC",),  # 最终文字顺序
                selected_frames=(  # 最终选中的内存图片
                    CapturedFrame(
                        session_id="session",  # 测量周期编号
                        capture_id="capture",  # 采集编号
                        camera_serial="CAM01",  # 相机序列号
                        frame_id="capture-1",  # 图片编号
                        captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                        captured_monotonic=1.0,  # 单调接收时间
                        image_data=b"BM...",  # BMP 文件字节
                    ),
                ),
                line_frame_ids=(("capture-1",),),  # 每条文字对应的图片编号
            )
        """
        if not frames:
            raise ValueError("CAPTURE_NO_FRAMES")
        # 编码本轮图片，保留身份和接收时间。
        captured_frames = []
        for frame in frames:
            try:
                image_data = encode_image(frame)
            except Exception as error:
                # 记录编码相机异常及所属周期，再终止本轮识别。
                logger.exception(
                    "相机编码失败 camera_serial=%s session_id=%s", camera_serial, session_id,
                )
                raise ImageEncodingError("相机图片编码失败") from error
            captured_at = datetime.now(timezone.utc) - timedelta(seconds=time.monotonic() - frame.received_monotonic)
            captured_frames.append(CapturedFrame(
                session_id=session_id,
                capture_id=capture_id,
                camera_serial=camera_serial,
                frame_id=f"{capture_id}-{frame.frame_number}",
                captured_at=captured_at.isoformat(),
                captured_monotonic=frame.received_monotonic,
                image_data=image_data,
            ))
        # 筛选合格图片，无合格图片时结束本轮。
        qualified_frames = self.filter_qualified_frames(tuple(captured_frames))
        captured_frames.clear()
        if not qualified_frames:
            raise ValueError("OCR_NO_QUALIFIED_FRAMES")
        # 调用模型并按输入顺序关联帧身份。
        image_results = self.recognize_images([frame.image_data for frame in qualified_frames])
        if len(image_results) != len(qualified_frames):
            raise ValueError("OCR_RESULT_COUNT_MISMATCH")
        frame_results = [
            {
                "frame_id": frame.frame_id,
                "blocks": image_result["blocks"],
            }
            for frame, image_result in zip(qualified_frames, image_results)
        ]
        # 生成最终文字和图片，无有效结果时按整轮失败处理。
        result = self.generate_final_text_and_images(frame_results, qualified_frames)
        if not result.ordered_lines:
            raise ValueError("OCR_NO_TEXT")
        if not result.selected_frames:
            raise ValueError("OCR_NO_SELECTED_IMAGES")
        return result

    def filter_qualified_frames(self, frames: tuple[CapturedFrame, ...]) -> tuple[CapturedFrame, ...]:
        """预留纯黑、截断等质量筛选，目前原样返回全部图片。

        Args:
            frames: 按采集顺序排列的内存图片。

        Returns:
            (
                CapturedFrame(
                    session_id="session",  # 测量周期编号
                    capture_id="capture",  # 采集编号
                    camera_serial="CAM01",  # 相机序列号
                    frame_id="capture-1",  # 唯一图片编号
                    captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                    captured_monotonic=1.0,  # 单调接收时间
                    image_data=b"BM...",  # 内存 BMP 字节
                ),
            )
        """
        return frames

    def recognize_images(self, images: list[bytes]) -> list[dict]:
        """预留整轮模型识别接口，目前明确报告未实现。

        Args:
            images: 按顺序排列的合格图片字节。

        Returns:
            [
                {
                    "blocks": [],  # 单张图片的模型原始文字块，结果与输入等长
                },
            ]
            当前抛出 NotImplementedError，不返回占位成功结果。
        """
        raise NotImplementedError("OCR_MODEL_NOT_IMPLEMENTED")

    def generate_final_text_and_images(self, frame_results: list[dict], frames: tuple[CapturedFrame, ...]) -> OCRResult:
        """预留文字去重、排序和对应图片选择，目前明确报告未实现。

        Args:
            frame_results: 每张图片的 frame_id 和模型原始 blocks。
            frames: 本轮合格图片，保留原始身份和采集顺序。

        Returns:
            OCRResult(
                ordered_lines=("ABC",),  # 去重并排序的完整文字
                selected_frames=(  # 按 frame_id 唯一保存的最终图片
                    CapturedFrame(
                        session_id="session",  # 测量周期编号
                        capture_id="capture",  # 采集编号
                        camera_serial="CAM01",  # 相机序列号
                        frame_id="capture-1",  # 图片编号
                        captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                        captured_monotonic=1.0,  # 单调接收时间
                        image_data=b"BM...",  # BMP 文件字节
                    ),
                ),
                line_frame_ids=(("capture-1",),),  # 与 ordered_lines 逐项对应的来源图片编号
            )
            当前抛出 NotImplementedError，不修改周期或保存图片。
        """
        raise NotImplementedError("OCR_FINAL_SELECTION_NOT_IMPLEMENTED")
