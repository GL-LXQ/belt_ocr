"""按整轮顺序执行原始帧整理、筛选、识别和文字图片终选。"""

import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from models import CapturedFrame, OCRResult
from camera.hikrobot_sdk import CameraFrame


logger = logging.getLogger(__name__)


def _serial_number_of(candidate: dict) -> int:
    """取 8 字符候选前七位数字的整数值。

    Args:
        candidate: 8 字符候选，含去空格后的 normalized_text。

    Returns:
        返回示例：
            2926215  # 前七位数字组成的整数
    """
    return int(candidate["normalized_text"][:7])


class TextRecognizer:
    """提供共享串行处理锁与无业务状态的 OCR 主流程。"""

    def __init__(self) -> None:
        """创建三台机器共用的整轮处理锁。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 处理锁已创建
        """
        # 创建三台机器共用的整轮处理锁。
        self.processing_lock = asyncio.Lock()

    def process_session_frames(
        self,
        session_id: str,
        capture_id: str,
        camera_serial: str,
        frames: tuple[CameraFrame, ...],
    ) -> OCRResult:
        """将整轮原始帧顺序处理为最终文字和对应原始图片。

        Args:
            session_id: 测量周期编号。
            capture_id: 采集编号。
            camera_serial: 相机序列号。
            frames: 本轮全部原始帧，按接收顺序排列。

        Returns:
            返回示例：
                OCRResult(
                    ordered_lines=("ABC",),  # 最终文字顺序
                    normalized_lines=("ABC",),  # 去空白文字顺序
                    selected_frames=(  # 最终选中的内存图片
                        CapturedFrame(
                            session_id="session",  # 测量周期编号
                            capture_id="capture",  # 采集编号
                            camera_serial="CAM01",  # 相机序列号
                            frame_id="capture-1",  # 图片编号
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
                                data=b"\x01\x02",  # 原始图像字节
                            ),
                        ),
                    ),
                    line_frame_ids=(("capture-1",),),  # 每条文字对应的图片编号
                    review_frames=(),  # 正常结果没有待复核图片
                    review_reason=None,  # 正常结果没有复核原因
                )
        """
        # 本轮没有帧时直接失败。
        if not frames:
            raise ValueError(f"session_id={session_id} 本轮没有采集到任何帧。")

        # 逐帧整理相机原始图片及其所属周期。
        captured_frames = []
        for frame in frames:
            # 按单调接收时间换算本帧的 UTC 时间。
            captured_at = datetime.now(timezone.utc) - timedelta(seconds=time.monotonic() - frame.received_monotonic)

            # 登记带周期身份和采集编号的图片。
            captured_frames.append(CapturedFrame(
                session_id=session_id,
                capture_id=capture_id,
                camera_serial=camera_serial,
                frame_id=f"{capture_id}-{frame.frame_number}",
                captured_at=captured_at.isoformat(),
                captured_monotonic=frame.received_monotonic,
                camera_frame=frame,
            ))

        # 固定本轮全部原始帧，并筛选合格图片。
        captured_frames = tuple(captured_frames)
        qualified_frames = self.filter_qualified_frames(captured_frames)

        # 初筛没有合格图片时返回全部原始帧供人工复核。
        if not qualified_frames:
            return OCRResult(
                ordered_lines=(),
                normalized_lines=(),
                selected_frames=(),
                line_frame_ids=(),
                review_frames=captured_frames,
                review_reason="初筛后没有合格图片",
            )

        # 调用模型识别本轮全部合格图片。
        image_results = self.recognize_images(
            [frame.camera_frame for frame in qualified_frames]
        )

        # 模型结果数量不一致时返回全部原始帧供人工复核。
        if len(image_results) != len(qualified_frames):
            return OCRResult(
                ordered_lines=(),
                normalized_lines=(),
                selected_frames=(),
                line_frame_ids=(),
                review_frames=captured_frames,
                review_reason="模型识别结果数量与图片数量不一致",
            )

        # 按输入顺序把模型结果与图片编号配对。
        frame_results = [
            {
                "frame_id": frame.frame_id,
                "blocks": image_result["blocks"],
            }
            for frame, image_result in zip(qualified_frames, image_results)
        ]

        # 取得最终文字、证据图片和文字对应的图片编号。
        (
            ordered_lines,
            normalized_lines,
            selected_frames,
            line_frame_ids,
            review_reason,
        ) = self.generate_final_text_and_images(frame_results, qualified_frames)

        # 没有最终文字时返回全部原始帧供人工复核。
        if not ordered_lines:
            logger.warning("本轮没有最终文字，人工复核 session_id=%s", session_id)
            review_reason = (
                f"{review_reason}；没有最终文字"
                if review_reason else "没有最终文字"
            )
            return OCRResult(
                ordered_lines=(),
                normalized_lines=(),
                selected_frames=(),
                line_frame_ids=(),
                review_frames=captured_frames,
                review_reason=review_reason,
            )

        # 组装并返回本轮最终结果。
        return OCRResult(
            ordered_lines=ordered_lines,
            normalized_lines=normalized_lines,
            selected_frames=selected_frames,
            line_frame_ids=line_frame_ids,
            review_frames=captured_frames if review_reason else (),
            review_reason=review_reason,
        )

    def filter_qualified_frames(self, frames: tuple[CapturedFrame, ...]) -> tuple[CapturedFrame, ...]:
        """预留纯黑、截断等质量筛选，目前原样返回全部图片。

        Args:
            frames: 按采集顺序排列的内存图片。

        Returns:
            返回示例：
                (
                    CapturedFrame(
                        session_id="session",  # 测量周期编号
                        capture_id="capture",  # 采集编号
                        camera_serial="CAM01",  # 相机序列号
                        frame_id="capture-1",  # 唯一图片编号
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
                            data=b"\x01\x02",  # 原始图像字节
                        ),
                    ),
                )
        """
        # 原样返回全部图片。
        return frames

    def recognize_images(self, images: list[CameraFrame]) -> list[dict]:
        """预留整轮模型识别接口，目前明确报告未实现。

        Args:
            images: 按顺序排列的合格相机原始帧。

        Returns:
            返回示例：
                [
                    {
                        "blocks": [],  # 单张图片的模型原始文字块，结果与输入等长
                    },
                ]
            当前抛出 NotImplementedError，不返回占位成功结果。
        """
        # 报告识别模型尚未实现。
        raise NotImplementedError("OCR_MODEL_NOT_IMPLEMENTED")

    def _serial_number_of(candidate: dict) -> int:
        """取得 8 字符编号前七位的数字部分。"""
        return int(candidate["normalized_text"][:7])

    def generate_final_text_and_images(
        self,
        frame_results: list[dict],
        frames: tuple[CapturedFrame, ...],
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[CapturedFrame, ...],
        tuple[tuple[str, ...], ...],
        str | None,
    ]:
        """按字符类别筛选最终文字，并给出每条文字对应的证据图片。

        处理规则：先将文字转成大写；20 字符不做格式过滤，3 和 2 字符要求纯数字，
        8 字符要求前七位数字加一位字母；
        20、3、2 字符各输出本类置信度最高的一条，8 字符先去重再按连续编号规则选出。
        8 字符共用最终入选文字中置信度最高的一张证据图片。
        没有候选、格式全部不符或最高置信度低于阈值时记录人工复核原因。

        Args:
            frame_results: 每张图片的 frame_id 和模型原始 blocks。
            frames: 本轮合格图片，保留原始身份和采集顺序。

        Returns:
            返回示例：
                (
                    ("0 03",),  # 按 20、8、3、2 类别顺序排列的大写文字
                    ("003",),  # 与最终文字逐项对应的去空白文字
                    (  # 最终选中的内存图片，同一图片只保留一次
                        CapturedFrame(
                            session_id="session",  # 测量周期编号
                            capture_id="capture",  # 采集编号
                            camera_serial="CAM01",  # 相机序列号
                            frame_id="capture-1",  # 图片编号
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
                                data=b"\x01\x02",  # 原始图像字节
                            ),
                        ),
                    ),
                    (("capture-1",),),  # 与文字逐项对应的证据图片编号
                    "没有可靠的 20 位文字",  # 各类别合并后的复核原因
                )
        """
        # 低于该置信度的文字不进入最终结果。
        minimum_confidence = 0.8

        # 建立帧编号到图片对象的索引。
        frames_by_id = {frame.frame_id: frame for frame in frames}

        # 收集所有文字，按去空白后的字符数量分入 20、8、3、2 四类。
        candidates_by_length: defaultdict[int, list[dict]] = defaultdict(list)
        for frame_result in frame_results:
            for block in frame_result["blocks"]:
                for line in block["lines"]:
                    # 统一文字中的字母大小写。
                    text = line["text"].upper()
                    normalized_text = re.sub(r"\s+", "", text)
                    character_length = len(normalized_text)
                    if character_length not in (20, 8, 3, 2):
                        continue

                    # 候选保留大写文字、去空白文字、置信度和来源图片。
                    candidates_by_length[character_length].append({
                        "text": text,
                        "normalized_text": normalized_text,
                        "confidence": line["confidence"],
                        "frame_id": frame_result["frame_id"],
                    })

        selected_candidates: list[dict] = []
        review_reasons: list[str] = []

        # 3 字符和 2 字符只接受 0 到 9。
        text_patterns = {
            3: r"[0-9]{3}",
            2: r"[0-9]{2}",
        }

        # 20、3、2 字符文字各自输出一条。
        for character_length in (20, 3, 2):
            candidates = candidates_by_length.get(character_length, [])

            # 当前类别一个候选都没有。
            if not candidates:
                logger.warning("%s字符文字没有候选，人工复核", character_length)
                review_reasons.append(f"没有可靠的 {character_length} 位文字")
                continue

            # 20 字符不做格式过滤，3、2 字符按数字格式过滤。
            pattern = text_patterns.get(character_length)
            if pattern is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if re.fullmatch(pattern, candidate["normalized_text"])
                ]

                # 有该位数的文字，但格式全部不符合要求。
                if not candidates:
                    logger.warning("%s字符文字没有格式正确的候选，人工复核", character_length)
                    review_reasons.append(f"没有格式正确的 {character_length} 位文字")
                    continue

            # 取本类置信度最高的一条。
            best_candidate = max(candidates, key=lambda candidate: candidate["confidence"])

            # 最高置信度仍低于阈值。
            if best_candidate["confidence"] < minimum_confidence:
                logger.warning("%s字符文字没有可靠候选，人工复核", character_length)
                review_reasons.append(f"{character_length} 位文字最高置信度不足")
                continue

            best_candidate["evidence_frame_id"] = best_candidate["frame_id"]
            selected_candidates.append(best_candidate)

        # 8 字符编号要求前七位数字加一位字母。
        eight_candidates = [
            candidate
            for candidate in candidates_by_length.get(8, [])
            if re.fullmatch(r"[0-9]{7}[A-Za-z]", candidate["normalized_text"])
        ]

        # 相同编号保留置信度最高的一次，并删除低于阈值的候选。
        reliable_eight_candidates = self._select_reliable_candidates(eight_candidates, minimum_confidence)

        # 没有可靠候选时人工复核。
        if not reliable_eight_candidates:
            logger.warning("8字符文字没有可靠候选，人工复核")
            review_reasons.append("没有可靠的 8 位文字")
        else:
            # 有可靠候选时按连续编号规则选出最终编号。
            eight_winners = self._select_eight_character_winners(reliable_eight_candidates)

            # 选最终编号中置信度最高的图片作为 8 字符类别的共同证据。
            best_eight_candidate = max(
                eight_winners, key=lambda candidate: candidate["confidence"]
            )
            evidence_frame_id = best_eight_candidate["frame_id"]
            for candidate in eight_winners:
                candidate["evidence_frame_id"] = evidence_frame_id
            selected_candidates.extend(eight_winners)

        # 统一按 20、8、3、2 的类别顺序排列。
        category_order = {
            20: 0,
            8: 1,
            3: 2,
            2: 3,
        }
        selected_candidates.sort(key=lambda candidate: category_order[len(candidate["normalized_text"])])

        # 最终文字保留 OCR 原有空白，字母统一大写。
        ordered_lines = tuple(candidate["text"] for candidate in selected_candidates)

        # 同时保存去掉所有空白后的文字。
        normalized_lines = tuple(candidate["normalized_text"] for candidate in selected_candidates)

        # 每条最终文字记录自己采用的证据图片。
        line_frame_ids = tuple(
            (candidate["evidence_frame_id"],) for candidate in selected_candidates
        )

        # 同一张证据图片只保留一次。
        selected_frame_ids = dict.fromkeys(
            candidate["evidence_frame_id"] for candidate in selected_candidates
        )
        selected_frames = tuple(frames_by_id[frame_id] for frame_id in selected_frame_ids)

        # 合并本轮需要人工复核的原因。
        review_reason = "；".join(review_reasons) or None

        # 返回最终文字、证据图片和复核原因。
        return (
            ordered_lines,
            normalized_lines,
            selected_frames,
            line_frame_ids,
            review_reason,
        )

    def _select_reliable_candidates(self, candidates: list[dict], minimum_confidence: float) -> list[dict]:
        """对相同的 8 字符文字去重，并删除低于阈值的候选。

        Args:
            candidates: 格式正确的 8 字符候选。
            minimum_confidence: 保留的最低置信度。

        Returns:
            返回示例：
                [{
                    "text": "2926215C",  # 保留空白的大写文字
                    "normalized_text": "2926215C",  # 去掉空白后的文字
                    "confidence": 0.95,  # 该行的识别置信度
                    "frame_id": "capture-1",  # 来源图片编号
                }]
        """
        # 相同文字只保留置信度最高的一次，同分保留先出现的候选。
        best_candidate_by_text: dict[str, dict] = {}
        for candidate in candidates:
            normalized_text = candidate["normalized_text"]
            current_candidate = best_candidate_by_text.get(normalized_text)
            if current_candidate is None or candidate["confidence"] > current_candidate["confidence"]:
                best_candidate_by_text[normalized_text] = candidate

        # 删除低于置信度阈值的候选。
        return [
            candidate
            for candidate in best_candidate_by_text.values()
            if candidate["confidence"] >= minimum_confidence
        ]

    def _select_eight_character_winners(self, candidates: list[dict]) -> list[dict]:
        """从 8 字符可靠候选中选出最终编号。

        优先选择最长连续编号组，没有连号时选择置信度最高的前三条。

        Args:
            candidates: 已去重并删除低置信度候选的 8 字符候选。

        Returns:
            返回示例：
                [{
                    "text": "2926215C",  # 保留空白的大写文字
                    "normalized_text": "2926215C",  # 去掉空白后的文字
                    "confidence": 0.95,  # 该行的识别置信度
                    "frame_id": "capture-1",  # 来源图片编号
                }]
        """
        # 按置信度从高到低排列，同分保留先出现的候选。
        ordered_candidates = sorted(candidates, key=lambda candidate: candidate["confidence"], reverse=True)

        # 只用置信度最高的前五条寻找连号。
        top_candidates = ordered_candidates[:5]

        # 按最后一位字母分组。
        candidates_by_suffix: defaultdict[str, list[dict]] = defaultdict(list)
        for candidate in top_candidates:
            suffix = candidate["normalized_text"][-1]
            candidates_by_suffix[suffix].append(candidate)

        # 收集所有长度至少为 2 的连续编号组。
        consecutive_groups: list[list[dict]] = []
        for suffix_candidates in candidates_by_suffix.values():
            ordered_suffix_candidates = sorted(suffix_candidates, key=_serial_number_of)
            current_group: list[dict] = []
            for candidate in ordered_suffix_candidates:
                # 当前编号与上一编号相差 1 时并入当前组。
                if current_group and _serial_number_of(candidate) == _serial_number_of(current_group[-1]) + 1:
                    current_group.append(candidate)
                    continue

                # 断开时收走已有连续组，两个及以上编号才计为一组。
                if len(current_group) >= 2:
                    consecutive_groups.append(current_group)

                # 从当前编号重新开始一组。
                current_group = [candidate]

            # 保存该后缀组末尾的连续段。
            if len(current_group) >= 2:
                consecutive_groups.append(current_group)

        # 完全没有连号时使用置信度最高的前三条。
        if not consecutive_groups:
            return top_candidates[:3]

        # 连号数量最多的组优先，数量相同时组内最低置信度更高者优先。
        winning_group = max(
            consecutive_groups,
            key=lambda group: (len(group), min(candidate["confidence"] for candidate in group)),
        )

        # 最终连续编号按数字从小到大输出。
        winning_group.sort(key=_serial_number_of)
        return winning_group
