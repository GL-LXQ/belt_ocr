"""生成独立记录的四类 OCR 终选测试基准数据。"""

import json
import re
from pathlib import Path


REFERENCE_PATH = Path(__file__).with_name("ocr_selection_reference_200.json")


def record_reference_case(
    cases: list[dict],
    category: int,
    scenario: str,
    variant: int,
    frame_blocks: list[list[list[tuple[str, float]]]],
    expected_lines: list[str],
    evidence_frame_numbers: list[int],
) -> None:
    """将输入、人工指定的入选文字和证据图片写成固定基准案例。

    Args:
        cases: 当前已收集的基准案例。
        category: 本案例主要验证的字符类别。
        scenario: 本案例验证的边界场景名称。
        variant: 同场景的样本序号。
        frame_blocks: 每张图片、每个 block、每行的文字和置信度。
        expected_lines: 预先指定的入选文字，按最终业务顺序排列。
        evidence_frame_numbers: 每条入选文字使用的图片序号，从一开始。

    Returns:
        返回示例：
            None  # 基准案例已追加到 cases
    """
    case_id = f"{category}-{scenario}-{variant:02d}"
    frames = []

    # 将候选文字写成真实样本使用的图片、block 和 line 结构。
    for frame_number, blocks in enumerate(frame_blocks, start=1):
        frame_id = f"{case_id}-frame-{frame_number}"
        model_blocks = []
        for block_lines in blocks:
            model_lines = []
            for line_number, (line_text, confidence) in enumerate(block_lines):
                model_lines.append({
                    "text": line_text,
                    "bbox": [0, line_number * 10, 100, line_number * 10 + 10],
                    "confidence": confidence,
                })
            model_blocks.append({"bbox": [0, 0, 100, 100], "lines": model_lines})
        frames.append({
            "frame_id": frame_id,
            "image_path": f"mock/{frame_id}.jpg",
            "blocks": model_blocks,
        })

    # 记录预先指定的文字和图片对应关系。
    ordered_lines = [line.upper() for line in expected_lines]
    normalized_lines = [re.sub(r"\s+", "", line) for line in ordered_lines]
    evidence_frame_ids = [
        frames[frame_number - 1]["frame_id"]
        for frame_number in evidence_frame_numbers
    ]
    cases.append({
        "case_id": case_id,
        "category": category,
        "scenario": scenario,
        "frames": frames,
        "expected": {
            "ordered_lines": ordered_lines,
            "normalized_lines": normalized_lines,
            "line_frame_ids": [[frame_id] for frame_id in evidence_frame_ids],
            "selected_frame_ids": list(dict.fromkeys(evidence_frame_ids)),
        },
    })


def build_twenty_character_cases(cases: list[dict]) -> None:
    """记录 50 个 20 位文字的长度、格式、阈值和跨图案例。

    Args:
        cases: 当前已收集的基准案例。

    Returns:
        返回示例：
            None  # 50 个 20 位基准案例已追加
    """
    for variant in range(5):
        primary_text = f"{2378240 + variant:07d} vega x 5epj1152"
        alternate_text = f"{2378250 + variant:07d} vega x 5epj1152"
        lower_confidence = 0.9380401372909546 + variant * 0.000001
        higher_confidence = 0.9629952907562256 + variant * 0.000001
        if variant == 4:
            higher_confidence = 1.0

        # 单条合格、两条选高分和同分先到先选。
        record_reference_case(
            cases, 20, "at_threshold", variant,
            [[[(primary_text, 0.8)]]], [primary_text], [1],
        )
        record_reference_case(
            cases, 20, "highest_confidence", variant,
            [[[(primary_text, lower_confidence),
               (alternate_text, higher_confidence)]]],
            [alternate_text], [1],
        )
        record_reference_case(
            cases, 20, "exact_tie", variant,
            [[[(primary_text, 0.9)]], [[(alternate_text, 0.9)]]],
            [primary_text], [1],
        )

        # 低于阈值、少一位和多一位均不产生 20 位结果。
        record_reference_case(
            cases, 20, "below_threshold", variant,
            [[[(primary_text, 0.0 if variant == 0 else 0.799999)]]],
            [], [],
        )
        record_reference_case(
            cases, 20, "missing_character", variant,
            [[[(primary_text[:-1], 0.99)]]], [], [],
        )
        record_reference_case(
            cases, 20, "extra_character", variant,
            [[[(primary_text + "Z", 0.99)]]], [], [],
        )

        # 20 位不做格式过滤，Unicode 大写扩长后按实际长度分桶。
        symbol_characters = ("#", "℃", ":", "/", "-")
        symbol_text = primary_text[:-1] + symbol_characters[variant]
        record_reference_case(
            cases, 20, "special_symbol", variant,
            [[[(symbol_text, 0.9)]]], [symbol_text], [1],
        )
        record_reference_case(
            cases, 20, "unicode_case_expansion", variant,
            [[[('a' * 19 + 'ß', 0.99)]]], [], [],
        )

        # 跨 block 的空白文字和跨图的高分文字分别保留实际证据图片。
        spaced_text = primary_text.replace(" ", "\t", 1)
        record_reference_case(
            cases, 20, "multiple_blocks", variant,
            [[[], [("4", 0.99)], [(spaced_text, 0.9)]]],
            [spaced_text], [1],
        )
        record_reference_case(
            cases, 20, "highest_frame", variant,
            [[[(primary_text, 0.9)]], [[(alternate_text, 0.95)]]],
            [alternate_text], [2],
        )


def build_three_character_cases(cases: list[dict]) -> None:
    """记录 50 个 3 位文字的数字格式、阈值和错位案例。

    Args:
        cases: 当前已收集的基准案例。

    Returns:
        返回示例：
            None  # 50 个 3 位基准案例已追加
    """
    for variant in range(5):
        primary_text = f"{variant:03d}"
        alternate_text = f"{variant + 100:03d}"
        lower_confidence = 0.9380401372909546 + variant * 0.000001
        higher_confidence = 0.9629952907562256 + variant * 0.000001
        if variant == 4:
            higher_confidence = 1.0

        # 单条合格、两条选高分、格式过滤后选有效数字。
        record_reference_case(
            cases, 3, "at_threshold", variant,
            [[[(primary_text, 0.8)]]], [primary_text], [1],
        )
        record_reference_case(
            cases, 3, "highest_confidence", variant,
            [[[(primary_text, lower_confidence),
               (alternate_text, higher_confidence)]]],
            [alternate_text], [1],
        )
        invalid_texts = ("A12", "00A", "0#3", "０１２", "12℃")
        record_reference_case(
            cases, 3, "letter_filtered", variant,
            [[[(invalid_texts[variant], 0.99), (primary_text, 0.8)]]],
            [primary_text], [1],
        )

        # 低于阈值、添加符号、非 ASCII 数字分别验证过滤与回退。
        record_reference_case(
            cases, 3, "below_threshold", variant,
            [[[(primary_text, 0.0 if variant == 0 else 0.799999)]]],
            [], [],
        )
        record_reference_case(
            cases, 3, "extra_symbol", variant,
            [[[(':' + primary_text, 0.99)]]], [], [],
        )
        record_reference_case(
            cases, 3, "unicode_digit_filtered", variant,
            [[[("٠٠٣", 0.99), (primary_text, 0.8)]]],
            [primary_text], [1],
        )

        # 空白归一化、跨图取高分和同分先到先选。
        spaced_text = f"{primary_text[0]} {primary_text[1:]}"
        record_reference_case(
            cases, 3, "embedded_space", variant,
            [[[(spaced_text, 0.9)]]], [spaced_text], [1],
        )
        record_reference_case(
            cases, 3, "highest_frame", variant,
            [[[(primary_text, 0.9)]], [[(alternate_text, 0.95)]]],
            [alternate_text], [2],
        )
        record_reference_case(
            cases, 3, "exact_tie", variant,
            [[[(primary_text, 0.9)]], [[(alternate_text, 0.9)]]],
            [primary_text], [1],
        )

        # 少一位的纯数字按 2 位类别记录。
        shortened_text = primary_text[1:]
        record_reference_case(
            cases, 3, "reclassified_as_two", variant,
            [[[(shortened_text, 0.9)]]], [shortened_text], [1],
        )


def build_two_character_cases(cases: list[dict]) -> None:
    """记录 50 个 2 位文字的数字格式、阈值和错位案例。

    Args:
        cases: 当前已收集的基准案例。

    Returns:
        返回示例：
            None  # 50 个 2 位基准案例已追加
    """
    for variant in range(5):
        primary_text = f"{variant + 10:02d}"
        alternate_text = f"{variant + 20:02d}"
        lower_confidence = 0.9380401372909546 + variant * 0.000001
        higher_confidence = 0.9629952907562256 + variant * 0.000001
        if variant == 4:
            higher_confidence = 1.0

        # 单条合格、两条选高分、格式过滤后选有效数字。
        record_reference_case(
            cases, 2, "at_threshold", variant,
            [[[(primary_text, 0.8)]]], [primary_text], [1],
        )
        record_reference_case(
            cases, 2, "highest_confidence", variant,
            [[[(primary_text, lower_confidence),
               (alternate_text, higher_confidence)]]],
            [alternate_text], [1],
        )
        invalid_texts = ("A4", "4A", "0#", "２１", "2℃")
        record_reference_case(
            cases, 2, "letter_filtered", variant,
            [[[(invalid_texts[variant], 0.99), (primary_text, 0.8)]]],
            [primary_text], [1],
        )

        # 低于阈值、少一位、非 ASCII 数字分别验证筛选。
        record_reference_case(
            cases, 2, "below_threshold", variant,
            [[[(primary_text, 0.0 if variant == 0 else 0.799999)]]],
            [], [],
        )
        record_reference_case(
            cases, 2, "missing_character", variant,
            [[[(primary_text[1:], 0.99)]]], [], [],
        )
        record_reference_case(
            cases, 2, "unicode_digit_filtered", variant,
            [[[("٢١", 0.99), (primary_text, 0.8)]]],
            [primary_text], [1],
        )

        # 空白归一化、跨图取高分和同分先到先选。
        spaced_text = f"{primary_text[0]} {primary_text[1]}"
        record_reference_case(
            cases, 2, "embedded_space", variant,
            [[[(spaced_text, 0.9)]]], [spaced_text], [1],
        )
        record_reference_case(
            cases, 2, "highest_frame", variant,
            [[[(primary_text, 0.9)]], [[(alternate_text, 0.95)]]],
            [alternate_text], [2],
        )
        record_reference_case(
            cases, 2, "exact_tie", variant,
            [[[(primary_text, 0.9)]], [[(alternate_text, 0.9)]]],
            [primary_text], [1],
        )

        # 多一位的纯数字按 3 位类别记录。
        extended_text = f"0{primary_text}"
        record_reference_case(
            cases, 2, "reclassified_as_three", variant,
            [[[(extended_text, 0.9)]]], [extended_text], [1],
        )


def build_eight_character_cases(cases: list[dict]) -> None:
    """记录 50 个 8 位文字的格式、去重、Top 5、连号和证据案例。

    Args:
        cases: 当前已收集的基准案例。

    Returns:
        返回示例：
            None  # 50 个 8 位基准案例已追加
    """
    for variant in range(5):
        serial_start = 2926210 + variant * 10
        serial_texts = [
            f"{serial_start + offset:07d}c" for offset in range(5)
        ]
        lower_confidence = 0.9380401372909546 + variant * 0.000001
        higher_confidence = 0.9629952907562256 + variant * 0.000001
        if variant == 4:
            higher_confidence = 1.0

        # 单条合格、格式错误高分、同文跨图去重。
        record_reference_case(
            cases, 8, "at_threshold", variant,
            [[[(serial_texts[0], 0.8)]]], [serial_texts[0]], [1],
        )
        invalid_texts = (
            ("5EPJ1152", "2020 214℃"),
            ("292621C", "29262155C"),
            ("2926215#", "292621CA"),
            ("LULULIAL", "2926215℃"),
            ("12345678", "123456A7"),
        )
        record_reference_case(
            cases, 8, "format_filtered", variant,
            [[[(invalid_texts[variant][0], 0.99998),
               (invalid_texts[variant][1], 0.99),
               (serial_texts[0], lower_confidence)]]],
            [serial_texts[0]], [1],
        )
        record_reference_case(
            cases, 8, "duplicate_highest_frame", variant,
            [[[(serial_texts[0], 0.9)]],
             [[(serial_texts[0].upper(), higher_confidence)]]],
            [serial_texts[0]], [2],
        )

        # 低于阈值和无连号前三条分别验证空结果与共同证据图片。
        record_reference_case(
            cases, 8, "below_threshold", variant,
            [[[(serial_texts[0], 0.0 if variant == 0 else 0.799999),
               ("5EPJ1152", 0.99)]]],
            [], [],
        )
        isolated_texts = [
            f"{serial_start + offset * 2:07d}{chr(65 + offset)}"
            for offset in range(4)
        ]
        record_reference_case(
            cases, 8, "no_sequence_top_three", variant,
            [[[(isolated_texts[0], 0.90)]],
             [[(isolated_texts[1], 0.99)]],
             [[(isolated_texts[2], 0.95)]],
             [[(isolated_texts[3], 0.85)]]],
            [isolated_texts[1], isolated_texts[2], isolated_texts[0]],
            [2, 2, 2],
        )

        # 大小写连号和第六条在 Top 5 外的同分边界。
        first_text = "0000099c" if variant == 0 else serial_texts[0]
        second_text = "0000100C" if variant == 0 else serial_texts[1]
        record_reference_case(
            cases, 8, "mixed_case_sequence", variant,
            [[[(first_text, 0.90)]], [[(second_text, 0.95)]]],
            [first_text, second_text], [2, 2],
        )
        boundary_texts = [
            f"{1000000 + variant + offset * 2000000:07d}{chr(65 + offset)}"
            for offset in range(5)
        ]
        sixth_text = f"{1000001 + variant:07d}A"
        boundary_lines = [
            (boundary_texts[offset], 0.99 - offset * 0.01)
            for offset in range(5)
        ]
        boundary_lines.append((sixth_text, 0.95))
        record_reference_case(
            cases, 8, "top_five_exact_tie", variant,
            [[boundary_lines]], boundary_texts[:3], [1, 1, 1],
        )

        # 长连号组优先，等长时比较组内最低置信度。
        group_b = [
            f"{serial_start + offset:07d}B" for offset in range(2)
        ]
        record_reference_case(
            cases, 8, "longest_group", variant,
            [[[(group_b[0], 0.99), (group_b[1], 0.98)]],
             [[(serial_texts[0], 0.91), (serial_texts[1], 0.90),
               (serial_texts[2], 0.89)]]],
            serial_texts[:3], [2, 2, 2],
        )
        if variant == 4:
            record_reference_case(
                cases, 8, "higher_group_minimum", variant,
                [[[(serial_texts[0], 0.99), (serial_texts[1], 0.8)]],
                 [[(group_b[0], 0.98), (group_b[1], 0.8)]]],
                serial_texts[:2], [1, 1],
            )
        else:
            record_reference_case(
                cases, 8, "higher_group_minimum", variant,
                [[[(serial_texts[0], 0.99), (serial_texts[1], 0.81)]],
                 [[(group_b[0], 0.95), (group_b[1], 0.90)]]],
                group_b, [2, 2],
            )

        # 五张图片都含完整编号，五条最终文字共用最高分图片。
        full_group_frames = []
        best_confidences = [0.95, 0.92, 0.99, 0.91, 0.93]
        for best_text_number, best_confidence in enumerate(best_confidences):
            frame_lines = [
                (serial_text, best_confidence if text_number == best_text_number
                 else 0.7)
                for text_number, serial_text in enumerate(serial_texts)
            ]
            full_group_frames.append([frame_lines])
        record_reference_case(
            cases, 8, "five_lines_one_evidence", variant,
            full_group_frames, serial_texts, [3] * 5,
        )


def build_ocr_selection_reference() -> None:
    """按四类文字生成固定输入与预期结果文件。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 固定基准文件已写入 tests/fixtures
    """
    # 按类别逐组记录预期结果。
    cases = []
    build_twenty_character_cases(cases)
    build_eight_character_cases(cases)
    build_three_character_cases(cases)
    build_two_character_cases(cases)

    # 写入固定基准文件，测试只读取该文件。
    reference_data = {"schema_version": 1, "cases": cases}
    REFERENCE_PATH.write_text(
        json.dumps(reference_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build_ocr_selection_reference()
