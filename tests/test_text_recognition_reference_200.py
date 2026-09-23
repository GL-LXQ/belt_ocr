"""将 OCR 终选结果与预先记录的 200 条正确数据逐项比对。"""

import json
from collections import Counter
from pathlib import Path

import pytest

from camera.hikrobot_sdk import CameraFrame
from models import CapturedFrame
from text_recognition import TextRecognizer


REFERENCE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "ocr_selection_reference_200.json"
)
REFERENCE_CASES = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["cases"]


def test_reference_contains_fifty_cases_per_category() -> None:
    """检查固定基准数据包含四类各 50 条且编号不重复。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 四类数量与案例编号已通过断言验证
    """
    # 统计固定基准中的类别数量和案例编号。
    category_counts = Counter(case["category"] for case in REFERENCE_CASES)
    case_ids = [case["case_id"] for case in REFERENCE_CASES]

    # 核对每类 50 条且所有案例编号唯一。
    assert category_counts == {20: 50, 8: 50, 3: 50, 2: 50}
    assert len(case_ids) == len(set(case_ids)) == 200


@pytest.mark.parametrize(
    "reference_case",
    REFERENCE_CASES,
    ids=[case["case_id"] for case in REFERENCE_CASES],
)
def test_final_selection_matches_recorded_correct_data(
    reference_case: dict,
) -> None:
    """逐项比对文字、归一化文字、逐条证据和最终图片。

    Args:
        reference_case: 已记录输入和正确输出的一条固定基准案例。

    Returns:
        返回示例：
            None  # 本案例的四项输出已与固定基准比对
    """
    # 将固定 OCR 输入中的每个 frame_id 绑定到独立的内存图片。
    frame_results = reference_case["frames"]
    frames = tuple(
        CapturedFrame(
            session_id="session",
            capture_id="capture",
            camera_serial="camera",
            frame_id=frame_result["frame_id"],
            captured_at="2026-09-23",
            captured_monotonic=float(frame_number),
            camera_frame=CameraFrame(
                "camera", frame_number, 0, 0, float(frame_number), 1, 1, 0, 0,
                b"BM" + frame_result["frame_id"].encode("ascii"),
            ),
        )
        for frame_number, frame_result in enumerate(frame_results, start=1)
    )
    frames_by_id = {frame.frame_id: frame for frame in frames}
    expected = reference_case["expected"]

    # 调用终选函数并比对预先记录的四项正确输出。
    result = TextRecognizer().generate_final_text_and_images(
        frame_results, frames
    )
    assert result.ordered_lines == tuple(expected["ordered_lines"])
    assert result.normalized_lines == tuple(expected["normalized_lines"])
    assert result.line_frame_ids == tuple(
        tuple(frame_ids) for frame_ids in expected["line_frame_ids"]
    )
    assert result.selected_frames == tuple(
        frames_by_id[frame_id] for frame_id in expected["selected_frame_ids"]
    )
