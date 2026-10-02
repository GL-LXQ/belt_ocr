"""处理一轮 OCR，包括帧整理、帧筛选、文字识别和最终结果生成。"""

import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import MeasurementFrame, OCRResult
from camera.hikrobot_sdk import CameraFrame, convert_mono8_frame_to_array
from ocr.config import load_config as load_ocr_config
from ocr.engine import BeltOCREngine


logger = logging.getLogger(__name__)


class OCRProcessingError(Exception):
    """表示本轮 OCR 处理过程中发生了可识别的处理错误。"""


class OCRResourceWaitTimeoutError(TimeoutError):
    """表示等待 OCR 使用权超时。"""


def _serial_number_of(candidate: dict) -> int:
    """返回 8 字符候选前 7 位数字对应的整数。

    Args:
        candidate: 包含 recognized_text 的 8 字符候选。

    Returns:
        返回示例：
            2926215  # 前七位数字组成的整数
    """
    return int(candidate["recognized_text"][:7])


class TextRecognizer:
    """管理共享 OCR Engine 的使用，并提供帧筛选、识别和结果整理流程。"""

    def __init__(self) -> None:
        """初始化系统共享的 OCR 访问控制和 OCR Engine。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # OCR 访问控制和 Engine 引用已初始化
        """
        # 创建共享 OCR 访问锁。
        self._processing_lock = asyncio.Lock()

        # 初始化共享 OCR Engine，启动前暂时为空。
        self.ocr_engine: BeltOCREngine | None = None

    @asynccontextmanager
    async def acquire_ocr_access(
        self,
        wait_timeout_seconds: float,
    ) -> AsyncIterator[None]:
        """在指定时间内等待 OCR 使用权，并在退出上下文时自动释放。

        Args:
            wait_timeout_seconds: 等待 OCR 使用权的最长时间，单位秒。

        Returns:
            返回示例：
                None  # 进入上下文后独占 OCR Engine 的使用权
        """
        # 在指定时间内等待 OCR 使用权。
        try:
            await asyncio.wait_for(
                self._processing_lock.acquire(),
                timeout=wait_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise OCRResourceWaitTimeoutError("等待共享 OCR 处理资源超时") from error

        # 退出上下文时自动释放 OCR 使用权。
        try:
            yield
        finally:
            self._processing_lock.release()

    def initialize(self) -> None:
        """加载 OCR 配置并初始化系统共享的 OCR Engine。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # OCR Engine 已初始化，已有实例时直接复用
        """
        # OCR Engine 已初始化时直接复用现有实例。
        if self.ocr_engine is not None:
            return

        # 加载 OCR 配置并创建共享 OCR Engine。
        config_path = Path(__file__).resolve().parent / "ocr" / "config.yaml"
        self.ocr_engine = BeltOCREngine(load_ocr_config(config_path))

    def prepare_frames_for_ocr(
        self,
        session_id: str,
        capture_id: str,
        camera_serial: str,
        frames: tuple[CameraFrame, ...],
    ) -> tuple[tuple[MeasurementFrame, ...], tuple[MeasurementFrame, ...]]:
        """为本轮相机帧补充测量信息，并筛选需要进入 OCR 的帧。

        Args:
            session_id: 当前测量的 Session ID。
            capture_id: 本轮采集 ID。
            camera_serial: 相机序列号。
            frames: 本轮相机原始帧，按接收顺序排列。

        Returns:
            返回示例：
                measurement_frame = MeasurementFrame(
                    session_id="session",  # Session ID
                    capture_id="capture",  # 采集编号
                    camera_serial="CAM01",  # 相机序列号
                    frame_id="capture-1",  # 帧 ID
                    captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                    captured_monotonic=1.0,  # 单调接收时间
                    camera_frame=CameraFrame(
                        camera_serial="CAM01",  # 相机序列号
                        frame_number=1,  # SDK 帧编号
                        device_timestamp=100,  # 设备时间戳
                        host_timestamp=200,  # 主机时间戳
                        received_monotonic=1.0,  # 接收单调时间
                        width=2,  # 图像宽度
                        height=1,  # 图像高度
                        pixel_type=17301505,  # 像素格式编号
                        lost_packet_count=0,  # 丢包数
                        image_bytes=b"\x01\x02",  # 原始图像字节
                    ),
                )
                (
                    (measurement_frame,),  # 本轮整理后的全部测量帧
                    (measurement_frame,),  # 按原顺序筛选出的 OCR 帧
                )
        """
        # 本轮没有采集到图片时直接报错。
        if not frames:
            raise ValueError(f"session_id={session_id} 本轮没有采集到任何帧。")

        # 逐帧补充 Session、采集 ID 和时间信息。
        measurement_frames = []
        for frame in frames:
            # 根据单调接收时间换算这一帧的 UTC 接收时间。
            captured_at = datetime.now(timezone.utc) - timedelta(
                seconds=time.monotonic() - frame.received_monotonic
            )

            # 生成带有 Session 和采集信息的 MeasurementFrame。
            measurement_frames.append(MeasurementFrame(
                session_id=session_id,
                capture_id=capture_id,
                camera_serial=camera_serial,
                frame_id=f"{capture_id}-{frame.frame_number}",
                captured_at=captured_at.isoformat(),
                captured_monotonic=frame.received_monotonic,
                camera_frame=frame,
            ))

        # 将整理后的帧转为元组，并筛选需要进入 OCR 的帧。
        measurement_frames = tuple(measurement_frames)
        qualified_frames = self.select_qualified_frames(measurement_frames)
        return measurement_frames, qualified_frames

    def create_no_qualified_frames_result(
        self, measurement_frames: tuple[MeasurementFrame, ...]
    ) -> OCRResult:
        """没有合格帧时，生成需要人工复核的 OCR 结果。

        Args:
            measurement_frames: 本轮整理后的全部测量帧。

        Returns:
            返回示例：
                measurement_frame = MeasurementFrame(
                    session_id="session",  # Session ID
                    capture_id="capture",  # 采集编号
                    camera_serial="CAM01",  # 相机序列号
                    frame_id="capture-1",  # 帧 ID
                    captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                    captured_monotonic=1.0,  # 单调接收时间
                    camera_frame=CameraFrame(
                        camera_serial="CAM01",  # 相机序列号
                        frame_number=1,  # SDK 帧编号
                        device_timestamp=100,  # 设备时间戳
                        host_timestamp=200,  # 主机时间戳
                        received_monotonic=1.0,  # 接收单调时间
                        width=2,  # 图像宽度
                        height=1,  # 图像高度
                        pixel_type=17301505,  # 像素格式编号
                        lost_packet_count=0,  # 丢包数
                        image_bytes=b"\x01\x02",  # 原始图像字节
                    ),
                )
                OCRResult(
                    recognized_lines=(),  # 正式识别文字
                    selected_frames=(),  # 选中的证据帧
                    line_frame_ids=(),  # 文字对应的证据帧 ID
                    review_frames=(measurement_frame,),  # 待人工复核的测量帧
                    review_reason="初筛后没有合格图片",  # 复核原因
                )
        """
        # 将本轮全部测量帧留给人工复核。
        return OCRResult(
            recognized_lines=(),
            selected_frames=(),
            line_frame_ids=(),
            review_frames=measurement_frames,
            review_reason="初筛后没有合格图片",
        )

    def recognize_qualified_frames(
        self,
        session_id: str,
        measurement_frames: tuple[MeasurementFrame, ...],
        qualified_frames: tuple[MeasurementFrame, ...],
    ) -> OCRResult:
        """识别合格帧，并生成最终文字和证据图片。

        Args:
            session_id: 当前测量的 Session ID。
            measurement_frames: 本轮整理后的全部测量帧。
            qualified_frames: 本轮筛选后需要进入 OCR 的帧。

        Returns:
            返回示例：
                selected_frame = MeasurementFrame(
                    session_id="session",  # Session ID
                    capture_id="capture",  # 采集编号
                    camera_serial="CAM01",  # 相机序列号
                    frame_id="capture-1",  # 帧 ID
                    captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                    captured_monotonic=1.0,  # 单调接收时间
                    camera_frame=CameraFrame(
                        camera_serial="CAM01",  # 相机序列号
                        frame_number=1,  # SDK 帧编号
                        device_timestamp=100,  # 设备时间戳
                        host_timestamp=200,  # 主机时间戳
                        received_monotonic=1.0,  # 接收单调时间
                        width=2,  # 图像宽度
                        height=1,  # 图像高度
                        pixel_type=17301505,  # 像素格式编号
                        lost_packet_count=0,  # 丢包数
                        image_bytes=b"\x01\x02",  # 原始图像字节
                    ),
                )
                OCRResult(
                    recognized_lines=("ABC",),  # 正式识别文字
                    selected_frames=(selected_frame,),  # 选中的证据帧
                    line_frame_ids=(("capture-1",),),  # 文字对应的证据帧 ID
                    review_frames=(),  # 正常结果没有待复核图片
                    review_reason=None,  # 正常结果没有复核原因
                )
        """

        # 调用 OCR Engine 识别本轮全部合格帧。
        image_results = self.recognize_images(
            [frame.camera_frame for frame in qualified_frames]
        )

        # 检查 OCR 返回结果的类型和数量。
        if not isinstance(image_results, list):
            raise OCRProcessingError("模型识别结果不是图片结果列表")
        if len(image_results) != len(qualified_frames):
            raise OCRProcessingError("模型识别结果数量与图片数量不一致")

        # 检查每张图片的 OCR 结果结构是否符合后续处理要求。
        for image_result in image_results:
            if not isinstance(image_result, dict) or not isinstance(
                image_result.get("blocks"), list
            ):
                raise OCRProcessingError("模型单图结果缺少 blocks 列表")
            for block in image_result["blocks"]:
                if not isinstance(block, dict) or not isinstance(
                    block.get("lines"), list
                ):
                    raise OCRProcessingError("模型文字块缺少 lines 列表")
                for line in block["lines"]:
                    if not isinstance(line, dict) or not isinstance(
                        line.get("text"), str
                    ):
                        raise OCRProcessingError("模型文字行缺少 text 字符串")

        # 按输入顺序将 OCR 结果与对应的 frame_id 组合。
        frame_results = [
            {
                "frame_id": frame.frame_id,
                "blocks": image_result["blocks"],
            }
            for frame, image_result in zip(qualified_frames, image_results)
        ]

        # 根据全部 OCR 结果选出最终文字、证据图片和对应的 frame_id。
        (
            recognized_lines,
            selected_frames,
            line_frame_ids,
            review_reason,
        ) = self.generate_final_text_and_images(frame_results, qualified_frames)

        # 没有选出最终文字时，将本轮全部测量帧留给人工复核。
        if not recognized_lines:
            logger.warning("本轮没有最终文字，人工复核 session_id=%s", session_id)
            review_reason = (
                f"{review_reason}；没有最终文字"
                if review_reason else "没有最终文字"
            )
            return OCRResult(
                recognized_lines=(),
                selected_frames=(),
                line_frame_ids=(),
                review_frames=measurement_frames,
                review_reason=review_reason,
            )

        # 生成并返回本轮 OCRResult。
        return OCRResult(
            recognized_lines=recognized_lines,
            selected_frames=selected_frames,
            line_frame_ids=line_frame_ids,
            review_frames=measurement_frames if review_reason else (),
            review_reason=review_reason,
        )

    def select_qualified_frames(
        self, frames: tuple[MeasurementFrame, ...]
    ) -> tuple[MeasurementFrame, ...]:
        """筛选需要进入 OCR 的测量帧；当前还未实现过滤，暂时返回全部帧。

        Args:
            frames: 按采集顺序排列的 MeasurementFrame。

        Returns:
            返回示例：
                (
                    MeasurementFrame(
                        session_id="session",  # Session ID
                        capture_id="capture",  # 采集编号
                        camera_serial="CAM01",  # 相机序列号
                        frame_id="capture-1",  # 帧 ID
                        captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                        captured_monotonic=1.0,  # 单调接收时间
                        camera_frame=CameraFrame(
                            camera_serial="CAM01",  # 相机序列号
                            frame_number=1,  # SDK 帧编号
                            device_timestamp=100,  # 设备时间戳
                            host_timestamp=200,  # 主机时间戳
                            received_monotonic=1.0,  # 接收单调时间
                            width=2,  # 图像宽度
                            height=1,  # 图像高度
                            pixel_type=17301505,  # 像素格式编号
                            lost_packet_count=0,  # 丢包数
                            image_bytes=b"\x01\x02",  # 原始图像字节
                        ),
                    ),
                )
        """
        # 当前未实现帧过滤，直接返回全部帧。
        return frames

    def recognize_images(self, camera_frames: list[CameraFrame]) -> list[dict]:
        """逐张将 CameraFrame 转为 NumPy 图像，并调用共享 OCR Engine 识别。

        Args:
            camera_frames: 按顺序排列、需要识别的 CameraFrame。

        Returns:
            返回示例：
                [
                    {
                        "image_path": None,  # 内存图片没有路径
                        "blocks": [  # 当前图片的文字块
                            {
                                "bbox": [1, 2, 10, 12],  # 文字块坐标
                                "lines": [  # 文字块中的文字行
                                    {
                                        "text": "123",  # 文字内容
                                        "bbox": [1, 2, 10, 12],  # 文字行坐标
                                        "confidence": 0.95,  # 识别置信度
                                    }
                                ],
                            }
                        ],
                    },
                ]
        """
        # 按输入顺序逐张转换并识别相机帧。
        image_results = []
        for camera_frame in camera_frames:
            try:
                image_numpy = convert_mono8_frame_to_array(camera_frame)
            except ValueError as error:
                raise OCRProcessingError(str(error)) from error

            # 确保直接调用 recognize_images() 时也复用同一个 OCR Engine。
            self.initialize()

            # 保存当前图片的 OCR 结果。
            image_results.append(self.ocr_engine.process_image(image_numpy))

        return image_results

    def _serial_number_of(candidate: dict) -> int:
        """返回 8 字符编号前 7 位数字对应的整数。

        Args:
            candidate: 包含 recognized_text 的 8 字符候选。

        Returns:
            返回示例：
                2926215  # 前七位数字组成的整数
        """
        return int(candidate["recognized_text"][:7])

    def generate_final_text_and_images(
        self,
        frame_results: list[dict],
        frames: tuple[MeasurementFrame, ...],
    ) -> tuple[
        tuple[str, ...],
        tuple[MeasurementFrame, ...],
        tuple[tuple[str, ...], ...],
        str | None,
    ]:
        """按 20、8、3、2 字符类别选出最终文字，并确定对应的证据图片。

        处理规则：先将文字统一转成大写，并去掉所有空白。
        20 字符不限制格式；3、2 字符必须全部是数字；8 字符必须是前 7 位数字加 1 位字母。
        20、3、2 字符各选置信度最高的一条；8 字符先去重并过滤低置信度候选，
        再按连续编号规则选择。
        8 字符最终使用其中置信度最高候选所在的图片作为共同证据。
        某类没有候选、格式不符合要求或置信度不足时，记录人工复核原因。

        Args:
            frame_results: 每张图片的 frame_id 和 OCR blocks。
            frames: 本轮经过筛选的 MeasurementFrame，保持原有采集顺序。

        Returns:
            返回示例：
                (
                    ("003",),  # 正式识别文字，按 20、8、3、2 类别排列
                    (  # 最终证据帧，同一帧只保留一次
                        MeasurementFrame(
                            session_id="session",  # Session ID
                            capture_id="capture",  # 采集编号
                            camera_serial="CAM01",  # 相机序列号
                            frame_id="capture-1",  # 帧 ID
                            captured_at="2026-09-19T00:00:00+00:00",  # UTC 接收时间
                            captured_monotonic=1.0,  # 单调接收时间
                            camera_frame=CameraFrame(
                                camera_serial="CAM01",  # 相机序列号
                                frame_number=1,  # SDK 帧编号
                                device_timestamp=100,  # 设备时间戳
                                host_timestamp=200,  # 主机时间戳
                                received_monotonic=1.0,  # 接收单调时间
                                width=2,  # 图像宽度
                                height=1,  # 图像高度
                                pixel_type=17301505,  # 像素格式编号
                                lost_packet_count=0,  # 丢包数
                                image_bytes=b"\x01\x02",  # 原始图像字节
                            ),
                        ),
                    ),
                    (("capture-1",),),  # 与文字逐项对应的证据帧 ID
                    "没有可靠的 20 位文字",  # 合并后的人工复核原因
                )
        """
        # 低于该置信度的文字不进入最终结果。
        minimum_confidence = 0.8

        # 按 frame_id 建立 MeasurementFrame 索引。
        frames_by_id = {frame.frame_id: frame for frame in frames}

        # 收集 OCR 文字，并按去空白后的字符数分为 20、8、3、2 四类。
        candidates_by_length: defaultdict[int, list[dict]] = defaultdict(list)
        for frame_result in frame_results:
            for block in frame_result["blocks"]:
                for line in block["lines"]:
                    # 将 OCR 文字转为大写并删除所有空白。
                    recognized_text = re.sub(r"\s+", "", line["text"].upper())
                    character_length = len(recognized_text)
                    if character_length not in (20, 8, 3, 2):
                        continue

                    # 保存正式识别候选、置信度和来源 frame_id。
                    candidates_by_length[character_length].append({
                        "recognized_text": recognized_text,
                        "confidence": line.get("confidence"),
                        "frame_id": frame_result["frame_id"],
                    })

        selected_candidates: list[dict] = []
        review_reasons: list[str] = []

        # 3 字符和 2 字符候选必须全部是数字。
        text_patterns = {
            3: r"[0-9]{3}",
            2: r"[0-9]{2}",
        }

        # 20、3、2 字符类别各选出一条最终文字。
        for character_length in (20, 3, 2):
            candidates = candidates_by_length.get(character_length, [])

            # 当前类别没有任何候选。
            if not candidates:
                logger.warning("%s字符文字没有候选，人工复核", character_length)
                review_reasons.append(f"没有可靠的 {character_length} 位文字")
                continue

            # 20 字符候选不限制格式；3、2 字符候选只保留纯数字。
            pattern = text_patterns.get(character_length)
            if pattern is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if re.fullmatch(pattern, candidate["recognized_text"])
                ]

                # 当前类别有候选，但没有一个符合格式要求。
                if not candidates:
                    logger.warning("%s字符文字没有格式正确的候选，人工复核", character_length)
                    review_reasons.append(f"没有格式正确的 {character_length} 位文字")
                    continue

            # 检查当前类别所有候选的置信度是否有效。
            for candidate in candidates:
                confidence = candidate["confidence"]
                confidence_is_usable = (
                    not isinstance(confidence, bool)
                    and isinstance(confidence, (int, float))
                    and math.isfinite(confidence)
                )
                if not confidence_is_usable:
                    raise OCRProcessingError(f"{character_length} 位文字置信度不是有限数值")

            # 选出当前类别置信度最高的候选。
            best_candidate = max(
                candidates,
                key=lambda candidate: candidate["confidence"],
            )

            # 最高置信度仍低于最低要求。
            if best_candidate["confidence"] < minimum_confidence:
                logger.warning("%s字符文字没有可靠候选，人工复核", character_length)
                review_reasons.append(f"{character_length} 位文字最高置信度不足")
                continue

            best_candidate["evidence_frame_id"] = best_candidate["frame_id"]
            selected_candidates.append(best_candidate)

        # 8 字符候选必须是前 7 位数字加 1 位字母。
        eight_candidates = [
            candidate
            for candidate in candidates_by_length.get(8, [])
            if re.fullmatch(r"[0-9]{7}[A-Za-z]", candidate["recognized_text"])
        ]

        # 检查 8 字符候选的置信度是否有效。
        for candidate in eight_candidates:
            confidence = candidate["confidence"]
            confidence_is_usable = (
                not isinstance(confidence, bool)
                and isinstance(confidence, (int, float))
                and math.isfinite(confidence)
            )
            if not confidence_is_usable:
                raise OCRProcessingError("8 位文字置信度不是有限数值")

        # 相同 8 字符文字只保留置信度最高的一条，并过滤低置信度候选。
        reliable_eight_candidates = self._select_reliable_candidates(
            eight_candidates,
            minimum_confidence,
        )

        # 没有可靠的 8 字符候选时，记录人工复核原因。
        if not reliable_eight_candidates:
            logger.warning("8字符文字没有可靠候选，人工复核")
            review_reasons.append("没有可靠的 8 位文字")
        else:
            # 有可靠候选时，按连续编号规则选出最终 8 字符文字。
            eight_winners = self._select_eight_character_winners(
                reliable_eight_candidates
            )

            # 从最终 8 字符文字中选出置信度最高的一条，并将它的来源图片作为这一类别的共同证据。
            best_eight_candidate = max(
                eight_winners, key=lambda candidate: candidate["confidence"]
            )
            evidence_frame_id = best_eight_candidate["frame_id"]
            for candidate in eight_winners:
                candidate["evidence_frame_id"] = evidence_frame_id
            selected_candidates.extend(eight_winners)

        # 将最终结果按 20、8、3、2 字符类别排序。
        category_order = {
            20: 0,
            8: 1,
            3: 2,
            2: 3,
        }
        selected_candidates.sort(
            key=lambda candidate: category_order[len(candidate["recognized_text"])]
        )

        # 按已排序候选生成正式识别文字。
        recognized_lines = tuple(
            candidate["recognized_text"] for candidate in selected_candidates
        )

        # 为每条最终文字记录对应的证据 frame_id。
        line_frame_ids = tuple(
            (candidate["evidence_frame_id"],) for candidate in selected_candidates
        )

        # 生成证据图片列表时，同一个 frame_id 只保留一次。
        selected_frame_ids = dict.fromkeys(
            candidate["evidence_frame_id"] for candidate in selected_candidates
        )
        selected_frames = tuple(
            frames_by_id[frame_id] for frame_id in selected_frame_ids
        )

        # 合并本轮需要人工复核的原因。
        review_reason = "；".join(review_reasons) or None

        # 返回正式识别文字、证据图片、文字对应的 frame_id 和复核原因。
        return (
            recognized_lines,
            selected_frames,
            line_frame_ids,
            review_reason,
        )

    def _select_reliable_candidates(
        self,
        candidates: list[dict],
        minimum_confidence: float,
    ) -> list[dict]:
        """相同 8 字符文字只保留置信度最高的一条，并过滤低置信度候选。

        Args:
            candidates: 格式正确的 8 字符候选。
            minimum_confidence: 保留的最低置信度。

        Returns:
            返回示例：
                [{
                    "recognized_text": "2926215C",  # 大写且无空白的正式识别文字
                    "confidence": 0.95,  # 该行的识别置信度
                    "frame_id": "capture-1",  # 来源图片编号
                }]
        """
        # 相同文字只保留置信度最高的一条；置信度相同时保留先出现的候选。
        best_candidate_by_text: dict[str, dict] = {}
        for candidate in candidates:
            recognized_text = candidate["recognized_text"]
            current_candidate = best_candidate_by_text.get(recognized_text)
            if (
                current_candidate is None
                or candidate["confidence"] > current_candidate["confidence"]
            ):
                best_candidate_by_text[recognized_text] = candidate

        # 过滤低于最低置信度的候选。
        return [
            candidate
            for candidate in best_candidate_by_text.values()
            if candidate["confidence"] >= minimum_confidence
        ]

    def _select_eight_character_winners(self, candidates: list[dict]) -> list[dict]:
        """从可靠的 8 字符候选中选出最终结果。

        优先选择最长的连续编号组；没有连续编号时，选择置信度最高的前三条。

        Args:
            candidates: 已去重并过滤低置信度候选的 8 字符候选。

        Returns:
            返回示例：
                [{
                    "recognized_text": "2926215C",  # 大写且无空白的正式识别文字
                    "confidence": 0.95,  # 该行的识别置信度
                    "frame_id": "capture-1",  # 来源图片编号
                }]
        """
        # 按置信度从高到低排列，同分保留先出现的候选。
        ordered_candidates = sorted(
            candidates,
            key=lambda candidate: candidate["confidence"],
            reverse=True,
        )

        # 只在置信度最高的前 5 条候选中寻找连续编号。
        top_candidates = ordered_candidates[:5]

        # 按编号最后一位字母分组。
        candidates_by_suffix: defaultdict[str, list[dict]] = defaultdict(list)
        for candidate in top_candidates:
            suffix = candidate["recognized_text"][-1]
            candidates_by_suffix[suffix].append(candidate)

        # 收集所有长度至少为 2 的连续编号组。
        consecutive_groups: list[list[dict]] = []
        for suffix_candidates in candidates_by_suffix.values():
            ordered_suffix_candidates = sorted(suffix_candidates, key=_serial_number_of)
            current_group: list[dict] = []
            for candidate in ordered_suffix_candidates:
                # 当前编号与上一编号相差 1 时并入当前组。
                if (
                    current_group
                    and _serial_number_of(candidate)
                    == _serial_number_of(current_group[-1]) + 1
                ):
                    current_group.append(candidate)
                    continue

                # 连续关系中断时，保存当前连续组；至少包含 2 个编号才算有效。
                if len(current_group) >= 2:
                    consecutive_groups.append(current_group)

                # 从当前编号开始新的连续组。
                current_group = [candidate]

            # 保存当前字母分组末尾的连续编号组。
            if len(current_group) >= 2:
                consecutive_groups.append(current_group)

        # 没有找到连续编号时，返回置信度最高的前三条。
        if not consecutive_groups:
            return top_candidates[:3]

        # 优先选择包含编号最多的连续组；数量相同时，选择组内最低置信度更高的一组。
        winning_group = max(
            consecutive_groups,
            key=lambda group: (
                len(group),
                min(candidate["confidence"] for candidate in group),
            ),
        )

        # 最终连续编号按前 7 位数字从小到大排列。
        winning_group.sort(key=_serial_number_of)
        return winning_group
