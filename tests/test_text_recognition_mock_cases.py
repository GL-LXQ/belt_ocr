"""使用真实 OCR 文件结构和合成候选验证文字图片终选。"""

import json
from pathlib import Path

import pytest

from models import CapturedFrame
from text_recognition import TextRecognizer


RESULTS_DIRECTORY = Path(__file__).resolve().parents[1] / "statistics" / "results"


def build_mock_ocr_results(
    frame_blocks: list[list[list[tuple[str, float]]]],
) -> tuple[list[dict], tuple[CapturedFrame, ...]]:
    """将多图、多块的候选文字组装成真实 OCR 结果结构。

    Args:
        frame_blocks: 每张图片、每个 block、每行的文字和置信度。

    Returns:
        返回示例：
            (
                [
                    {
                        "frame_id": "capture-1",  # 来源图片编号
                        "image_path": "mock/capture-1.jpg",  # 模拟图片路径
                        "blocks": [  # 图片中的文字块
                            {
                                "bbox": [0, 0, 100, 100],  # 文字块位置
                                "lines": [  # 文字块中的行
                                    {
                                        "text": "003",  # OCR 原文
                                        "bbox": [0, 0, 100, 10],  # 文字行位置
                                        "confidence": 0.9,  # 识别置信度
                                    },
                                ],
                            },
                        ],
                    },
                ],
                (
                    CapturedFrame(
                        session_id="session",  # 测量周期编号
                        capture_id="capture",  # 采集编号
                        camera_serial="camera",  # 相机编号
                        frame_id="capture-1",  # 图片编号
                        captured_at="2026-09-23",  # 采集时间
                        captured_monotonic=1.0,  # 单调采集时间
                        image_data=b"BM",  # 图片字节
                    ),
                ),
            )
    """
    frame_results = []
    frames = []
    for frame_number, blocks in enumerate(frame_blocks, start=1):
        frame_id = f"capture-{frame_number}"
        frames.append(CapturedFrame(
            "session", "capture", "camera", frame_id, "2026-09-23",
            float(frame_number), b"BM",
        ))

        # 为当前图片建立与样本 JSON 相同的 block 和 line 字段。
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

        # 将当前图片的识别结果与来源图片编号配对。
        frame_results.append({
            "frame_id": frame_id,
            "image_path": f"mock/{frame_id}.jpg",
            "blocks": model_blocks,
        })

    return frame_results, tuple(frames)


@pytest.mark.parametrize("file_name", [
    "gray_opencv1.json",
    "gray_opencv2.json",
    "gray_opencv3.json",
    "gray_opencv4.json",
    "gray_opencv5.json",
])
def test_real_ocr_samples_follow_selection_rules(file_name: str) -> None:
    """五份真实 OCR 样本输出固定编号和对应的图片。

    Args:
        file_name: 真实 OCR 样本文件名。

    Returns:
        返回示例：
            None  # 样本最终文字与图片已通过断言验证
    """
    # 读取样本并绑定一张模拟的来源图片。
    result_path = RESULTS_DIRECTORY / file_name
    image_result = json.loads(result_path.read_text(encoding="utf-8"))
    frame_id = result_path.stem
    image_result["frame_id"] = frame_id
    frame = CapturedFrame(
        "session", "capture", "camera", frame_id, "2026-09-23", 1.0, b"BM"
    )

    # 核对样本中的 8 位连号、短数字和首份样本的 20 位文字。
    result = TextRecognizer().generate_final_text_and_images([image_result], (frame,))
    expected_lines = ("2926 215C", "2926 216C", "2926 217C", "003", "14")
    if file_name == "gray_opencv1.json":
        expected_lines = ("2378244 VEGA X 5EPJ1152",) + expected_lines
    assert result.ordered_lines == expected_lines
    assert result.normalized_lines == tuple(
        line.replace(" ", "") for line in expected_lines
    )
    assert result.line_frame_ids == ((frame_id,),) * len(expected_lines)
    assert result.selected_frames == (frame,)


@pytest.mark.parametrize(
    ("lines", "expected_lines", "warning_fragment"),
    [
        pytest.param(
            [("abcdefghijklmnopqrst", 0.8)],
            ("ABCDEFGHIJKLMNOPQRST",),
            None,
            id="twenty_accepts_any_format_at_threshold",
        ),
        pytest.param(
            [("AAAAAAAAAAAAAAAAAAAA", 0.9380401372909546),
             ("bbbbbbbbbbbbbbbbbbbb", 0.9629952907562256)],
            ("BBBBBBBBBBBBBBBBBBBB",), None, id="twenty_chooses_highest_confidence",
        ),
        pytest.param(
            [("AAAAAAAAAAAAAAAAAAAA", 0.9),
             ("bbbbbbbbbbbbbbbbbbbb", 0.9)],
            ("AAAAAAAAAAAAAAAAAAAA",), None, id="twenty_exact_tie_uses_first",
        ),
        pytest.param(
            [("AAAAAAAAAAAAAAAAAAAA", 0.799999)],
            (), "20字符文字没有可靠候选", id="twenty_below_threshold",
        ),
        pytest.param(
            [("00A", 0.99), ("0 03", 0.8)],
            ("0 03",), None, id="three_filters_letters_before_confidence",
        ),
        pytest.param(
            [("123", 0.9380401372909546), ("003", 0.9629952907562256)],
            ("003",), None, id="three_chooses_highest_valid_confidence",
        ),
        pytest.param(
            [("٠٠٣", 0.99), ("003", 0.8)],
            ("003",), None, id="three_filters_unicode_digits",
        ),
        pytest.param(
            [("A12", 0.99)],
            (), "3字符文字没有格式正确的候选", id="three_no_valid_format",
        ),
        pytest.param(
            [("123", 0.799999)],
            (), "3字符文字没有可靠候选", id="three_below_threshold",
        ),
        pytest.param(
            [("A4", 0.99), ("21", 0.8)],
            ("21",), None, id="two_filters_letters_at_threshold",
        ),
        pytest.param(
            [("14", 0.9380401372909546), ("21", 0.9629952907562256)],
            ("21",), None, id="two_chooses_highest_valid_confidence",
        ),
        pytest.param(
            [("٢١", 0.99), ("21", 0.8)],
            ("21",), None, id="two_filters_unicode_digits",
        ),
        pytest.param(
            [("A4", 0.99)],
            (), "2字符文字没有格式正确的候选", id="two_no_valid_format",
        ),
        pytest.param(
            [("21", 0.799999)],
            (), "2字符文字没有可靠候选", id="two_below_threshold",
        ),
        pytest.param(
            [("21", 1.0), ("14", 0.0)],
            ("21",), None, id="confidence_zero_and_one",
        ),
        pytest.param(
            [("0\t03", 0.9)],
            ("0\t03",), None, id="tab_is_removed_only_from_normalized_text",
        ),
        pytest.param(
            [("", 0.99), (" ", 0.99), ("4", 0.99), (":003", 0.99),
             ("1234567890123456789", 0.99),
             ("123456789012345678901", 0.99)],
            (), "20字符文字没有候选", id="unsupported_lengths_are_ignored",
        ),
        pytest.param(
            [("aaaaaaaaaaaaaaaaaaaß", 0.99)],
            (), "20字符文字没有候选", id="uppercase_expands_unicode_length",
        ),
        pytest.param([], (), "20字符文字没有候选", id="empty_lines"),
    ],
)
def test_short_and_twenty_character_mock_cases(
    lines: list[tuple[str, float]],
    expected_lines: tuple[str, ...],
    warning_fragment: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证 20、3、2 位的格式、阈值、同分和空白边界。

    Args:
        lines: 模拟单张图片内的 OCR 行及置信度。
        expected_lines: 预期输出的大写原有空白文字。
        warning_fragment: 需要出现的人工复核日志片段。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 分类文字、来源图片和复核日志已通过断言验证
    """
    # 将当前场景组装为带 bbox 的 OCR 结果。
    frame_results, frames = build_mock_ocr_results([[lines]])

    # 核对文字、来源图片和场景指定的复核日志。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == expected_lines
    assert result.normalized_lines == tuple(
        "".join(line.split()) for line in expected_lines
    )
    assert result.line_frame_ids == ((frames[0].frame_id,),) * len(expected_lines)
    assert result.selected_frames == (frames if expected_lines else ())
    if warning_fragment is not None:
        assert warning_fragment in caplog.text


@pytest.mark.parametrize(
    ("lines", "expected_lines", "warning_fragment"),
    [
        pytest.param(
            [("5EPJ1152", 0.99998), ("LULULIAL", 0.99),
             ("292621CA", 0.98), ("2926 215c", 0.9380401372909546)],
            ("2926 215C",), None, id="eight_requires_seven_digits_and_letter",
        ),
        pytest.param(
            [("5EPJ1152", 0.99), ("292621CA", 0.98)],
            (), "8字符文字没有可靠候选", id="eight_no_valid_format",
        ),
        pytest.param(
            [("2926215C", 0.8), ("2926216C", 0.799999)],
            ("2926215C",), None, id="eight_threshold_inclusive",
        ),
        pytest.param(
            [("2926215C", 0.799999)],
            (), "8字符文字没有可靠候选", id="eight_below_threshold",
        ),
        pytest.param(
            [("1111111A", 0.99), ("3333333A", 0.98),
             ("5555555B", 0.97), ("7777777B", 0.96)],
            ("1111111A", "3333333A", "5555555B"),
            None, id="eight_no_sequence_uses_top_three",
        ),
        pytest.param(
            [("1000000A", 0.99), ("3000000B", 0.98),
             ("5000000C", 0.97), ("7000000D", 0.96),
             ("9000000E", 0.95), ("9000001E", 0.94)],
            ("1000000A", "3000000B", "5000000C"),
            None, id="sixth_candidate_cannot_make_sequence",
        ),
        pytest.param(
            [("1000000A", 0.99), ("3000000B", 0.98),
             ("5000000C", 0.97), ("7000000D", 0.96),
             ("9000000E", 0.95), ("1000001A", 0.95)],
            ("1000000A", "3000000B", "5000000C"),
            None, id="fifth_place_exact_tie_uses_first",
        ),
        pytest.param(
            [("1000000A", 0.99), ("3000000B", 0.98),
             ("5000000C", 0.97), ("7000000D", 0.96),
             ("1000001A", 0.95), ("9000000E", 0.95)],
            ("1000000A", "1000001A"),
            None, id="fifth_place_exact_tie_can_make_sequence",
        ),
        pytest.param(
            [("2926215C", 0.99), ("2926216D", 0.98),
             ("1234567E", 0.97), ("7654321F", 0.96)],
            ("2926215C", "2926216D", "1234567E"),
            None, id="different_suffixes_do_not_make_sequence",
        ),
        pytest.param(
            [("0000099C", 0.91), ("0000100c", 0.90),
             ("9999999A", 0.99)],
            ("0000099C", "0000100C"),
            None, id="sequence_crosses_leading_zero_boundary",
        ),
        pytest.param(
            [("2000002A", 0.88), ("3000001B", 0.98),
             ("2000000A", 0.90), ("3000000B", 0.99),
             ("2000001A", 0.89)],
            ("2000000A", "2000001A", "2000002A"),
            None, id="longest_sequence_wins",
        ),
        pytest.param(
            [("2000000A", 0.99), ("2000001A", 0.81),
             ("3000000B", 0.95), ("3000001B", 0.90)],
            ("3000000B", "3000001B"),
            None, id="equal_length_uses_higher_minimum_confidence",
        ),
        pytest.param(
            [("2000000A", 0.99), ("2000001A", 0.8),
             ("3000000B", 0.98), ("3000001B", 0.8)],
            ("2000000A", "2000001A"),
            None, id="equal_group_scores_use_first_group",
        ),
        pytest.param(
            [("2926217c", 0.91), ("2926215C", 0.99),
             ("2926219C", 0.89), ("2926216C", 0.92),
             ("2926218C", 0.90)],
            ("2926215C", "2926216C", "2926217C", "2926218C", "2926219C"),
            None, id="whole_sequence_outputs_numeric_order",
        ),
        pytest.param(
            [("2926215c", 0.91), ("2926215C", 0.96),
             ("2926216C", 0.95), ("1234567A", 0.99)],
            ("2926215C", "2926216C"),
            None, id="case_insensitive_duplicate_keeps_highest",
        ),
    ],
)
def test_eight_character_mock_cases(
    lines: list[tuple[str, float]],
    expected_lines: tuple[str, ...],
    warning_fragment: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证 8 位编号的格式、去重、Top 5 和连号组选择。

    Args:
        lines: 模拟单张图片内的 OCR 行及置信度。
        expected_lines: 预期输出的大写编号。
        warning_fragment: 需要出现的人工复核日志片段。
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 编号、来源图片和复核日志已通过断言验证
    """
    # 将当前场景组装为带 bbox 的 OCR 结果。
    frame_results, frames = build_mock_ocr_results([[lines]])

    # 核对编号顺序、来源图片和场景指定的复核日志。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == expected_lines
    assert result.normalized_lines == tuple(
        "".join(line.split()) for line in expected_lines
    )
    assert result.line_frame_ids == ((frames[0].frame_id,),) * len(expected_lines)
    assert result.selected_frames == (frames if expected_lines else ())
    if warning_fragment is not None:
        assert warning_fragment in caplog.text


def test_multiple_frames_and_blocks_preserve_evidence_mapping() -> None:
    """多图、多块筛选后每条文字仍对应最终证据图片。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 文字顺序、证据编号与图片去重已通过断言验证
    """
    # 建立包含空 block、重复编号和四类文字的两张图片。
    frame_results, frames = build_mock_ocr_results([
        [
            [],
            [("2926216C", 0.92), ("003", 0.91)],
        ],
        [
            [("2378244 vega x 5epj1152", 0.90)],
            [("2926215c", 0.93), ("21", 0.94),
             ("2926215C", 0.89), (":003", 0.99)],
        ],
    ])

    # 核对按类别输出的文字及首次使用来源图片的顺序。
    result = TextRecognizer().generate_final_text_and_images(
        frame_results, frames
    )
    assert result.ordered_lines == (
        "2378244 VEGA X 5EPJ1152", "2926215C", "2926216C", "003", "21"
    )
    assert result.normalized_lines == (
        "2378244VEGAX5EPJ1152", "2926215C", "2926216C", "003", "21"
    )
    assert result.line_frame_ids == (
        (frames[1].frame_id,),
        (frames[1].frame_id,),
        (frames[1].frame_id,),
        (frames[0].frame_id,),
        (frames[1].frame_id,),
    )
    assert result.selected_frames == (frames[1], frames[0])

    # 图片识别结果顺序改变时仍按 frame_id 找回同一来源图片。
    reversed_result = TextRecognizer().generate_final_text_and_images(
        list(reversed(frame_results)), frames
    )
    assert reversed_result == result


def test_partial_classes_return_only_available_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺少部分类别时只返回合格文字并记录复核日志。

    Args:
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 部分结果及缺失类别日志已通过断言验证
    """
    # 建立只含 8 位和 3 位候选的图片结果。
    frame_results, frames = build_mock_ocr_results([
        [[("2926215c", 0.91), ("003", 0.92)]]
    ])

    # 核对部分类别结果与缺失类别的复核日志。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == ("2926215C", "003")
    assert result.normalized_lines == ("2926215C", "003")
    assert result.line_frame_ids == ((frames[0].frame_id,),) * 2
    assert result.selected_frames == frames
    assert "20字符文字没有候选" in caplog.text
    assert "2字符文字没有候选" in caplog.text


def test_empty_blocks_return_empty_ocr_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """所有 block 为空时结果为空且四类分别记录复核日志。

    Args:
        caplog: pytest 捕获的日志。

    Returns:
        返回示例：
            None  # 空结果及四类日志已通过断言验证
    """
    # 建立空 block 与没有 line 的 block。
    frame_results, frames = build_mock_ocr_results([[[], []]])

    # 核对四类都没有候选时的空结果。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == ()
    assert result.normalized_lines == ()
    assert result.line_frame_ids == ()
    assert result.selected_frames == ()
    for character_length in (20, 3, 2, 8):
        assert f"{character_length}字符文字没有" in caplog.text


def test_duplicate_eight_character_text_uses_one_evidence_frame() -> None:
    """跨图重复编号选择最高分候选的图片作为共同证据。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 去重后的证据图片已通过断言验证
    """
    # 在两张图片中放入大小写不同的重复编号。
    frame_results, frames = build_mock_ocr_results([
        [[("2926215c", 0.91), ("2926216C", 0.95)]],
        [[("2926215C", 0.96), ("1234567A", 0.99)]],
    ])

    # 核对入选的两个编号统一采用第二张图片作为证据。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == ("2926215C", "2926216C")
    assert result.line_frame_ids == (
        (frames[1].frame_id,),
        (frames[1].frame_id,),
    )
    assert result.selected_frames == (frames[1],)


def test_five_eight_character_lines_share_highest_confidence_frame() -> None:
    """五条入选的 8 位文字只保存最高置信度候选的图片。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 五条文字共用的证据图片已通过断言验证
    """
    # 每张图片都包含五条编号，每条编号的最高分分别来自不同图片。
    serial_texts = (
        "2926215C", "2926216C", "2926217C", "2926218C", "2926219C"
    )
    best_confidences = (0.95, 0.92, 0.99, 0.91, 0.93)
    frame_blocks = []
    for best_text_number, best_confidence in enumerate(best_confidences):
        frame_lines = [
            (serial_text, best_confidence if text_number == best_text_number else 0.7)
            for text_number, serial_text in enumerate(serial_texts)
        ]
        frame_blocks.append([frame_lines])
    frame_results, frames = build_mock_ocr_results(frame_blocks)

    # 核对所有编号按升序输出，并共用第三张最高分图片。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == (
        "2926215C", "2926216C", "2926217C", "2926218C", "2926219C"
    )
    assert result.line_frame_ids == ((frames[2].frame_id,),) * 5
    assert result.selected_frames == (frames[2],)


def test_eight_evidence_frame_comes_from_final_winning_group() -> None:
    """8 位证据图片从最终连号组选择，不采用未入选的高分编号。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 连号组证据图片已通过断言验证
    """
    # 建立高分孤立编号和分属两张图片的低分连号。
    frame_results, frames = build_mock_ocr_results([
        [[("9999999A", 0.99)]],
        [[("2926215C", 0.90)]],
        [[("2926216C", 0.92)]],
    ])

    # 核对最终两条文字共用连号组中得分最高的第三张图片。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == ("2926215C", "2926216C")
    assert result.line_frame_ids == ((frames[2].frame_id,),) * 2
    assert result.selected_frames == (frames[2],)


def test_eight_without_sequence_uses_one_top_confidence_frame() -> None:
    """没有连号时前三条 8 位文字共用最高分候选的图片。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 无连号分支的共同证据图片已通过断言验证
    """
    # 将四条互不连续的编号分别放入四张图片。
    frame_results, frames = build_mock_ocr_results([
        [[("1111111A", 0.90)]],
        [[("3333333B", 0.99)]],
        [[("5555555C", 0.95)]],
        [[("7777777D", 0.80)]],
    ])

    # 核对前三条按置信度排列，并共用第二张图片。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == ("3333333B", "5555555C", "1111111A")
    assert result.line_frame_ids == ((frames[1].frame_id,),) * 3
    assert result.selected_frames == (frames[1],)


@pytest.mark.parametrize(
    ("line_text", "expected_lines"),
    [
        pytest.param("1234567890123456789", (), id="twenty_missing_one_character"),
        pytest.param("123456789012345678901", (), id="twenty_extra_character"),
        pytest.param("abcdefghijklmnopqrs#", ("ABCDEFGHIJKLMNOPQRS#",),
                     id="twenty_accepts_special_symbol"),
        pytest.param("292621C", (), id="eight_missing_one_character"),
        pytest.param("29262155C", (), id="eight_extra_character"),
        pytest.param("2926215#", (), id="eight_symbol_replaces_letter"),
        pytest.param("2020 214℃", (), id="eight_celsius_symbol_from_real_sample"),
        pytest.param("0#3", (), id="three_contains_symbol"),
        pytest.param(":003", (), id="three_extra_symbol_changes_length"),
        pytest.param("0#", (), id="two_contains_symbol"),
        pytest.param("4", (), id="two_missing_one_character"),
        pytest.param("03", ("03",), id="three_missing_digit_becomes_two"),
        pytest.param("003", ("003",), id="two_extra_digit_becomes_three"),
    ],
)
def test_wrong_lengths_and_symbols_follow_current_buckets(
    line_text: str,
    expected_lines: tuple[str, ...],
) -> None:
    """逐项验证少字、多字和特殊符号在四类桶中的当前结果。

    Args:
        line_text: 模拟的 OCR 文字。
        expected_lines: 当前规则下预期选中的大写文字。

    Returns:
        返回示例：
            None  # 长度、格式与输出图片已通过断言验证
    """
    # 将单条文字放入与真实样本同结构的 OCR 结果。
    frame_results, frames = build_mock_ocr_results([
        [[(line_text, 0.9380401372909546)]]
    ])

    # 核对文字是否入选，以及对应图片是否保留。
    result = TextRecognizer().generate_final_text_and_images(frame_results, frames)
    assert result.ordered_lines == expected_lines
    assert result.normalized_lines == tuple(
        "".join(line.split()) for line in expected_lines
    )
    assert result.line_frame_ids == ((frames[0].frame_id,),) * len(expected_lines)
    assert result.selected_frames == (frames if expected_lines else ())
