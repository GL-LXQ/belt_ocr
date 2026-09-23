import json
import logging
import re
from collections import defaultdict
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from camera.hikrobot_sdk import CameraFrame
from models import CapturedFrame, OCRResult


logger = logging.getLogger(__name__)


def generate_final_text_and_images(frame_results: list[dict], frames: tuple[CapturedFrame, ...]) -> OCRResult:
    """按去空格后的字符数量筛选最终文字并返回来源图片。

    Args:
        frame_results: 每张图片的 frame_id 和模型原始 blocks。
        frames: 本轮合格图片，保留原始身份和采集顺序。

    Returns:
        OCRResult(
            ordered_lines=("003",),  # 按类别排列的最终原始文字
            selected_frames=(  # 按 frame_id 唯一保存的最终图片
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
                        device_timestamp=0,  # 设备时间戳
                        host_timestamp=0,  # 主机时间戳
                        received_monotonic=1.0,  # 接收单调时间
                        width=1,  # 图像宽度
                        height=1,  # 图像高度
                        pixel_type=0,  # 像素格式编号
                        lost_packet_count=0,  # 丢包数
                        data=b"BM",  # 测试图像字节
                    ),
                ),
            ),
            line_frame_ids=(("capture-1",),),  # 与 ordered_lines 逐项对应的来源图片编号
        )
        返回筛选出的文字、来源图片和文字帧对应关系；块2无连号时记录日志后使用最高置信度结果。
    """
    # 建立帧编号索引，方便根据最终文字收集来源图片。
    frames_by_id = {frame.frame_id: frame for frame in frames}
    candidates_by_length: defaultdict[int, list[dict]] = defaultdict(list)

    # 收集所有 line，并按去空格后的字符数量分桶。
    for frame_result in frame_results:
        for block in frame_result["blocks"]:
            for line in block["lines"]:
                text = line["text"]
                normalized_text = re.sub(r"\s+", "", text)
                if len(normalized_text) in (20, 8, 3, 2):
                    candidates_by_length[len(normalized_text)].append({
                        "text": text,
                        "normalized_text": normalized_text,
                        "confidence": line["confidence"],
                        "frame_id": frame_result["frame_id"],
                    })

    selected_candidates: list[dict] = []

    # 对内容应重复的三类文字，取置信度前20%后按重复次数投票。
    for character_length in (20, 3, 2):
        candidates = sorted(
            candidates_by_length.get(character_length, []),
            key=lambda candidate: candidate["confidence"],
            reverse=True,
        )
        if not candidates:
            continue
        # 向上取整保留前20%，按去空格后的完整文字累计票数。
        selected_count = max(1, (len(candidates) + 4) // 5)
        grouped_candidates: defaultdict[str, list[dict]] = defaultdict(list)
        for candidate in candidates[:selected_count]:
            grouped_candidates[candidate["normalized_text"]].append(candidate)
        # 先比较票数，同票比较平均置信度，再保留获胜文字中得分最高的原文。
        winning_text = max(
            grouped_candidates,
            key=lambda text: (
                len(grouped_candidates[text]),
                sum(item["confidence"] for item in grouped_candidates[text]) / len(grouped_candidates[text]),
            ),
        )
        winner = max(grouped_candidates[winning_text], key=lambda item: item["confidence"])
        selected_candidates.append(winner)

    # 对八字符块先取最高置信度前五条，再从中选择有相邻编号支持的结果。
    eight_character_candidates = [
        candidate
        for candidate in candidates_by_length.get(8, [])
        if re.fullmatch(r"[0-9]{7}[A-Za-z]", candidate["normalized_text"])
    ]
    top_eight_candidates = sorted(
        eight_character_candidates,
        key=lambda candidate: candidate["confidence"],
        reverse=True,
    )[:5]
    if top_eight_candidates:
        # 保留重复行的原始得分，按末尾字母和数字共同寻找相邻编号。
        serial_keys = {
            (int(candidate["normalized_text"][:7]), candidate["normalized_text"][-1])
            for candidate in top_eight_candidates
        }
        supported_candidates = []
        for candidate in top_eight_candidates:
            serial_number = int(candidate["normalized_text"][:7])
            suffix = candidate["normalized_text"][-1]
            if (serial_number - 1, suffix) in serial_keys or (serial_number + 1, suffix) in serial_keys:
                supported_candidates.append(candidate)
        if not supported_candidates:
            logger.warning("块2前五条候选没有连号支持，使用最高置信度结果：%s", top_eight_candidates[0]["text"])
            supported_candidates = top_eight_candidates
        selected_candidates.append(max(supported_candidates, key=lambda item: item["confidence"]))

    # 按四类的展示顺序整理结果，低置信度文字只记录日志。
    category_order = {
        20: 0,
        8: 1,
        3: 2,
        2: 3,
    }
    selected_candidates.sort(key=lambda candidate: category_order[len(candidate["normalized_text"])])
    for candidate in selected_candidates:
        if candidate["confidence"] < 0.8:
            logger.warning(
                "最终文字需人工复核 text=%s confidence=%.4f frame_id=%s",
                candidate["text"],
                candidate["confidence"],
                candidate["frame_id"],
            )

    # 整理原始文字和每条文字的来源帧编号。
    ordered_lines = tuple(candidate["text"] for candidate in selected_candidates)
    line_frame_ids = tuple((candidate["frame_id"],) for candidate in selected_candidates)

    # 按首次出现顺序收集图片，同一图片只保留一次。
    selected_frame_ids = dict.fromkeys(candidate["frame_id"] for candidate in selected_candidates)
    selected_frames = tuple(frames_by_id[frame_id] for frame_id in selected_frame_ids)
    return OCRResult(
        ordered_lines=ordered_lines,
        selected_frames=selected_frames,
        line_frame_ids=line_frame_ids,
    )


@pytest.mark.parametrize(
    "observations, expected_text, expected_warning",
    [
        ([("003", 0.98), ("0 03", 0.97), ("008", 0.99)] + [("008", 0.7)] * 8, "003", None),
        ([("003", 0.98), ("008", 0.99)] + [("003", 0.7)] * 4, "008", None),
        ([("A4", 0.9)], "A4", None),
        ([("14", 0.79)], "14", "需人工复核"),
        ([("14", 0.8)], "14", None),
        ([("2926 215C", 0.99), ("2926215C", 0.9), ("2926216C", 0.95)], "2926 215C", None),
        ([("2926215C", 0.99), ("2926216D", 0.95)], "2926215C", "没有连号支持"),
        ([("2926999C", 0.98), ("2927000C", 0.99)], "2927000C", None),
        ([("ABCDEFGH", 1.0), ("2926215C", 0.9)], "2926215C", "没有连号支持"),
        ([("2926215C", 0.99)] * 5 + [("2926216C", 0.98)], "2926215C", "没有连号支持"),
    ],
)
def test_candidate_selection(observations, expected_text, expected_warning, caplog):
    """验证字符分桶、投票、连号和日志边界。

    Args:
        observations: 文字与置信度组成的候选列表。
        expected_text: 预期保留的原始文字。
        expected_warning: 预期日志片段，None 表示没有警告。
        caplog: pytest 日志捕获对象。

    Returns:
        None  # 文字、图片映射和日志断言通过
    """
    # 创建测试帧，并将候选行装入同一个原始 block。
    frame = CapturedFrame(
        "session", "capture", "camera", "frame", "2026-09-21", 1.0,
        CameraFrame("camera", 1, 0, 0, 1.0, 1, 1, 0, 0, b"BM"),
    )
    frame_results = [{
        "frame_id": frame.frame_id,
        "blocks": [{
            "lines": [{
                "text": text,
                "confidence": confidence,
            } for text, confidence in observations],
        }],
    }]

    # 调用独立筛选函数，检查输出原文和来源图片。
    result = generate_final_text_and_images(frame_results, (frame,))
    assert result.ordered_lines == (expected_text,)
    assert result.selected_frames == (frame,)
    assert result.line_frame_ids == (("frame",),)

    # 核对低置信度和缺少连号支持的日志。
    if expected_warning is None:
        assert not caplog.records
    else:
        assert expected_warning in caplog.text


def test_empty_candidates():
    """验证没有候选时返回空结果且不补造缺失类别。

    Args:
        无外部参数。

    Returns:
        None  # 空结果断言通过
    """
    result = generate_final_text_and_images([], ())
    assert result == OCRResult((), (), ())


def test_statistics_samples():
    """读取本地五张图片及 OCR JSON，验证样本筛选和来源图片对应关系。

    Args:
        无外部参数。

    Returns:
        None  # 样本输出和 BMP 来源图片断言通过，并打印筛选文字
    """
    # 按样本文件名配对 JSON 和 JPG，读取每帧原始识别结果。
    statistics_directory = Path(__file__).resolve().parents[1] / "statistics"
    frame_results = []
    frames = []
    for result_path in sorted((statistics_directory / "results").glob("*.json")):
        content = json.loads(result_path.read_text(encoding="utf-8"))
        frame_results.append({
            "frame_id": result_path.stem,
            "blocks": content["blocks"],
        })

        # 将 JPG 转为内存 BMP，供测试核对图片内容。
        image = QImage(str(statistics_directory / "imgs" / f"{result_path.stem}.jpg"))
        buffer = QBuffer()
        assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "BMP")
        image_data = bytes(buffer.data())
        buffer.close()
        frames.append(CapturedFrame(
            session_id="sample-session",
            capture_id="sample-capture",
            camera_serial="sample-camera",
            frame_id=result_path.stem,
            captured_at="2026-09-21T00:00:00+00:00",
            captured_monotonic=float(len(frames)),
            camera_frame=CameraFrame(
                "sample-camera", len(frames), 0, 0, float(len(frames)),
                image.width(), image.height(), 0, 0, image_data,
            ),
        ))

    # 检查样本输出的四类文字、来源关系和图片去重。
    assert len(frames) == 5
    result = generate_final_text_and_images(frame_results, tuple(frames))
    print("样本最终文字：", result.ordered_lines)
    print("文字来源帧：", result.line_frame_ids)
    assert [len("".join(text.split())) for text in result.ordered_lines] == [20, 8, 3, 2]
    selected_ids = [frame.frame_id for frame in result.selected_frames]
    assert len(selected_ids) == len(set(selected_ids))
    assert set(selected_ids) == {frame_ids[0] for frame_ids in result.line_frame_ids}
    assert all(
        frame.camera_frame.data.startswith(b"BM") for frame in result.selected_frames
    )
    for text, frame_ids in zip(result.ordered_lines, result.line_frame_ids):
        source = next(item for item in frame_results if item["frame_id"] == frame_ids[0])
        assert any(text == line["text"] for block in source["blocks"] for line in block["lines"])
