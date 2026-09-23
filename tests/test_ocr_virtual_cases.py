"""每种场景生成十组虚拟 OCR 数据；已知局限用例仅记录当前行为，不表示业务正确。

输入中的每条候选为（原始文字，置信度）。各场景预期独立声明，不调用筛选函数生成预期。
同分场景保留当前稳定排序行为；不引入新的同分决策规则。这里不包含全角字符。
"""

import random

import pytest

from camera.hikrobot_sdk import CameraFrame
from models import CapturedFrame
from test_code_do_not_delete import generate_final_text_and_images


def build_virtual_cases():
    """生成二十九种场景，每种十组文字、得分或数量不同的虚拟数据。

    Args:
        无外部参数。

    Returns:
        [
            (
                "spaces-0",  # 场景名称和样本编号
                [("0 03", 0.99)],  # 原始文字及行置信度
                ("0 03",),  # 预期输出的原始文字
                (),  # 预期警告片段，空元组表示无警告
                "single",  # 帧与 block 的分布方式
            ),
        ]
    """
    cases = []
    for sample_number in range(10):
        # 变换数字、字母和置信度，生成各场景的十份不同样本。
        correct_text = f"{100 + sample_number:03d}"
        wrong_text = f"{800 + sample_number:03d}"
        short_text = str(20 + sample_number)
        suffix = chr(ord("A") + sample_number)
        serial_number = 2926210 + sample_number * 10
        serial_text = f"{serial_number:07d}{suffix}"
        next_text = f"{serial_number + 1:07d}{suffix}"
        distant_text = f"{serial_number + 100:07d}{suffix}"
        spaced_text = f" {correct_text[0]} {correct_text[1:]} "
        high_score = 0.99 - sample_number * 0.001
        low_rows = [(wrong_text, 0.5)] * 12
        no_sequence = ("没有连号支持",)
        low_confidence = ("需人工复核",)

        # 空格合并与少量高分错误：前三票中正确文字占两票。
        observations = [(spaced_text, 0.98), (correct_text, 0.97), (wrong_text, 0.99)] + low_rows
        cases.append((f"spaces-{sample_number}", observations, (spaced_text,), (), "single"))
        observations = [(correct_text, 0.98), (correct_text, 0.97), (wrong_text, 0.99)] + low_rows
        cases.append((f"high_score_error-{sample_number}", observations, (correct_text,), (), "single"))

        # 低分错误重复不进入前20%，同票时比较入选行的平均分。
        observations = [(correct_text, high_score)] * 3 + [(wrong_text, 0.5)] * (8 + sample_number)
        cases.append((f"low_score_repeats-{sample_number}", observations, (correct_text,), (), "single"))
        observations = [(wrong_text, 0.99), (wrong_text, 0.81), (correct_text, 0.97), (correct_text, 0.96)]
        observations += [(wrong_text, 0.5)] * 16
        cases.append((f"mean_tiebreak-{sample_number}", observations, (correct_text,), (), "single"))

        # 数量跨越五的倍数：取整后的第三票决定获胜文字。
        total_count = 11 + sample_number * 5
        selected_count = 3 + sample_number
        observations = [(wrong_text, 0.99)] + [(correct_text, 0.98)] * (selected_count - 1)
        observations += [(wrong_text, 0.5)] * (total_count - selected_count)
        cases.append((f"round_up-{sample_number}", observations, (correct_text,), (), "single"))

        # 记录按位数分类的局限：漏字进入短桶，标点不删除，字母不会被普通桶排除。
        observations = [(correct_text, high_score), (short_text, 0.98), (":" + correct_text, 1.0)]
        cases.append(
            (
                f"missing_extra_character-{sample_number}",
                observations,
                (correct_text, short_text),
                (),
                "single",
            ),
        )
        letter_text = suffix + str(sample_number)
        cases.append((f"same_length_error-{sample_number}", [(letter_text, high_score)], (letter_text,), (), "single"))

        # 分别覆盖低于阈值、等于阈值和高于阈值，不更改0.8边界。
        observations = [(short_text, 0.7999 - sample_number * 0.001)]
        cases.append((f"below_threshold-{sample_number}", observations, (short_text,), low_confidence, "single"))
        cases.append((f"at_threshold-{sample_number}", [(short_text, 0.8)], (short_text,), (), "single"))
        observations = [(short_text, 0.8001 + sample_number * 0.001)]
        cases.append((f"above_threshold-{sample_number}", observations, (short_text,), (), "single"))

        # 八位格式错误先过滤；孤立高分编号不能压过有连号支持的编号。
        invalid_text = f"A{serial_number:07d}"
        observations = [(invalid_text, 1.0), (str(serial_number) + "8", 1.0), (serial_text, 0.96), (next_text, 0.95)]
        cases.append((f"serial_format-{sample_number}", observations, (serial_text,), (), "single"))
        observations = [(distant_text, 0.99), (serial_text, 0.97), (next_text, 0.96)]
        cases.append((f"isolated_high_score-{sample_number}", observations, (serial_text,), (), "single"))

        # 前导零与进位、逆序输入均按七位数字的整数值判断。
        carry_number = 10 ** (1 + sample_number % 6)
        before_carry = f"{carry_number - 1:07d}{suffix}"
        after_carry = f"{carry_number:07d}{suffix}"
        observations = [(after_carry, high_score), (before_carry, 0.95)]
        cases.append((f"carry_and_leading_zero-{sample_number}", observations, (after_carry,), (), "single"))

        # 字母不同不提供连号支持，相同编号重复占满前五也不构成连号。
        other_suffix = chr(ord(suffix) + 1)
        observations = [(serial_text, high_score), (f"{serial_number + 1:07d}{other_suffix}", 0.9)]
        cases.append((f"suffix_mismatch-{sample_number}", observations, (serial_text,), no_sequence, "single"))
        observations = [(serial_text, high_score)] * 5 + [(next_text, 0.9)]
        cases.append((f"duplicates_fill_top_five-{sample_number}", observations, (serial_text,), no_sequence, "single"))

        # 记录当前完全同分的先到先选行为，两种输入顺序分别断言。
        pair = [correct_text, wrong_text] if sample_number % 2 == 0 else [wrong_text, correct_text]
        observations = [(pair[0], 0.95), (pair[1], 0.95)] + [(correct_text, 0.4)] * 8
        cases.append((f"exact_tie-{sample_number}", observations, (pair[0],), (), "single"))

        # 前20%边界同分时保持输入次序，不自动扩大入选范围。
        boundary_rows = [(correct_text, 0.98), (wrong_text, 0.98)]
        if sample_number % 2:
            boundary_rows.reverse()
        observations = [(correct_text, 0.99), (wrong_text, 0.99)] + boundary_rows + [(wrong_text, 0.3)] * 7
        cases.append((f"percentile_boundary_tie-{sample_number}", observations, (boundary_rows[0][0],), (), "single"))

        # 第五、第六条同分时仅保留先到候选，分别覆盖有连号与无连号两种结果。
        observations = [(serial_text, high_score)]
        observations += [(f"{serial_number + offset * 10:07d}{suffix}", 0.95) for offset in range(1, 4)]
        boundary_candidates = [(next_text, 0.9), (distant_text, 0.9)]
        warnings = ()
        if sample_number % 2:
            boundary_candidates.reverse()
            warnings = no_sequence
        observations += boundary_candidates
        cases.append((f"top_five_boundary_tie-{sample_number}", observations, (serial_text,), warnings, "single"))

        # 重复框分布在不同 block 中，当前会作为独立票累计。
        observations = [(correct_text, 0.99)] + [(wrong_text, 0.98)] * 3 + [(correct_text, 0.4)] * 11
        cases.append((f"duplicate_boxes-{sample_number}", observations, (wrong_text,), (), "blocks"))

        # 两个真实三位内容进入同桶时，当前仅保留一个结果。
        observations = [(correct_text, high_score), (wrong_text, 0.9)]
        cases.append((f"different_real_values-{sample_number}", observations, (correct_text,), (), "distributed"))

        # 记录低分邻居仍能提供连号支持的局限，低分未输出时不会触发复核日志。
        observations = [(serial_text, high_score), (next_text, 0.2 + sample_number * 0.01)]
        cases.append((f"weak_neighbor-{sample_number}", observations, (serial_text,), (), "single"))

        # 两个错误恰好连续时，当前无法从连号判断实际文字是否正确。
        wrong_next = f"{serial_number + 101:07d}{suffix}"
        observations = [(distant_text, high_score), (wrong_next, 0.95)]
        cases.append((f"wrong_but_consecutive-{sample_number}", observations, (distant_text,), (), "single"))

        # 多组连号竞争时选有支持的最高分，而不按组内票数或序列长度排名。
        observations = [(serial_text, 0.97), (next_text, 0.96), (distant_text, high_score), (wrong_next, 0.94)]
        cases.append((f"multiple_sequences-{sample_number}", observations, (distant_text,), (), "distributed"))

        # 第六条才有正确邻居时不扩大前五范围。
        observations = [(serial_text, high_score)]
        observations += [(f"{serial_number + offset * 10:07d}{suffix}", 0.95 - offset * 0.01) for offset in range(1, 5)]
        observations.append((next_text, 0.8))
        cases.append((f"neighbor_at_six-{sample_number}", observations, (serial_text,), no_sequence, "single"))

        # 低分正确文字再多也不能覆盖前20%的高分错误。
        observations = [(wrong_text, high_score)] * 3 + [(correct_text, 0.9)] * 12
        cases.append((f"concentrated_high_errors-{sample_number}", observations, (wrong_text,), (), "single"))

        # 半角字母大小写不自动统一，大小写不同的后缀不构成连号。
        observations = [(serial_text, high_score), (f"{serial_number + 1:07d}{suffix.lower()}", 0.9)]
        cases.append((f"letter_case-{sample_number}", observations, (serial_text,), no_sequence, "single"))

        # 四类散落多帧；三位、两位和型号各只有一条，不要求每帧包含全部类别。
        model_text = f"{2378240 + sample_number} VEGA X 5EPJ1152"
        observations = [(model_text, 0.97), (serial_text, 0.96), (next_text, 0.95)]
        observations += [(correct_text, 0.94), (short_text, 0.93)]
        expected = (model_text, serial_text, correct_text, short_text)
        cases.append((f"mixed_frames-{sample_number}", observations, expected, (), "distributed"))

        # 同一图片输出四类文字，来源图片仅保留一次。
        cases.append((f"shared_image-{sample_number}", observations, expected, (), "single"))

        # 空帧和空 block 与不匹配位数共存，不生成缺失类别结果。
        observations = [("", 0.99), (" " * (sample_number + 1), 0.99), ("ABCDE", 0.99)]
        cases.append((f"empty_and_missing-{sample_number}", observations, (), (), "distributed"))
    return cases


VIRTUAL_CASES = build_virtual_cases()


@pytest.mark.parametrize(
    "case_name, observations, expected_texts, expected_warnings, layout",
    VIRTUAL_CASES,
    ids=[case[0] for case in VIRTUAL_CASES],
)
def test_virtual_ocr_cases(case_name, observations, expected_texts, expected_warnings, layout, caplog):
    """将虚拟数据组装为多帧 OCR 输入，验证文字、日志及来源图片。

    Args:
        case_name: 场景名称和编号，已知局限仍按当前行为断言。
        observations: 原始文字与置信度列表。
        expected_texts: 预期原始文字元组。
        expected_warnings: 预期警告片段列表。
        layout: single 为单帧单块，blocks 为单帧多块，distributed 为多帧多块。
        caplog: pytest 日志捕获对象。

    Returns:
        None  # 输出文字、日志、图片身份和无歧义时的乱序结果全部通过断言
    """
    # 为各帧准备独立图片对象和空 block，虚拟字节仅用于核对图片身份。
    frame_count = 3 if layout == "distributed" else 1
    frames = tuple(
        CapturedFrame(
            session_id=case_name,
            capture_id="virtual-capture",
            camera_serial="virtual-camera",
            frame_id=f"frame-{frame_number}",
            captured_at="2026-09-21T00:00:00+00:00",
            captured_monotonic=float(frame_number),
            camera_frame=CameraFrame(
                "virtual-camera", frame_number, 0, 0, float(frame_number),
                1, 1, 0, 0, f"virtual-image-{frame_number}".encode(),
            ),
        )
        for frame_number in range(frame_count)
    )
    frame_results = [{
        "frame_id": frame.frame_id,
        "blocks": [{
            "lines": [],
        }],
    } for frame in frames]

    # 按布局分配候选行，并为每行补充原始坐标。
    for line_number, (text, confidence) in enumerate(observations):
        frame_result = frame_results[line_number % frame_count]
        if layout != "single":
            frame_result["blocks"].append({
                "lines": [],
            })
        frame_result["blocks"][-1]["lines"].append({
            "text": text,
            "confidence": confidence,
            "bbox": [10, line_number * 30, 210, line_number * 30 + 20],
        })

    # 执行筛选并分别断言文字和日志，局限场景不把错误结果当作真实标注。
    result = generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == expected_texts
    assert len(caplog.records) == len(expected_warnings)
    for expected_warning in expected_warnings:
        assert expected_warning in caplog.text

    # 核对输出文字来自真实输入行，图片对象和来源帧映射完整且没有重复。
    selected_ids = [frame.frame_id for frame in result.selected_frames]
    assert len(selected_ids) == len(set(selected_ids))
    assert set(selected_ids) == {frame_ids[0] for frame_ids in result.line_frame_ids}
    assert len(result.line_frame_ids) == len(expected_texts)
    for text, frame_ids in zip(result.ordered_lines, result.line_frame_ids):
        source = next(frame_result for frame_result in frame_results if frame_result["frame_id"] == frame_ids[0])
        assert any(line["text"] == text for block in source["blocks"] for line in block["lines"])
    for frame in result.selected_frames:
        assert frame is next(original for original in frames if original.frame_id == frame.frame_id)

    # 无同分歧义的多帧场景打乱帧、block 和行，检查完整输出保持一致。
    if case_name.startswith("mixed_frames-"):
        generator = random.Random(int(case_name.rsplit("-", 1)[1]))
        generator.shuffle(frame_results)
        for frame_result in frame_results:
            generator.shuffle(frame_result["blocks"])
            for block in frame_result["blocks"]:
                generator.shuffle(block["lines"])
        assert generate_final_text_and_images(frame_results, frames) == result
