"""验证标准证据帧的数值顺序与非标准名称的稳定回退。"""

from pathlib import Path

import pytest

from ui.evidence_order import build_evidence_image_sort_key


FIRST_CAPTURE_ID = "11111111111141118111111111111111"
SECOND_CAPTURE_ID = "22222222222242228222222222222222"


def test_standard_evidence_names_follow_numeric_frame_order() -> None:
    """验证标准文件按数值帧号排列，十号帧不会排在二号帧之前。

    Args:
        无。

    Returns:
        None  # 标准文件依次按 0、1、2、10、100 帧排列
    """
    paths = [Path(f"{FIRST_CAPTURE_ID}-{frame_number}.jpg") for frame_number in (10, 2, 100, 1, 0)]
    expected = [Path(f"{FIRST_CAPTURE_ID}-{frame_number}.jpg") for frame_number in (0, 1, 2, 10, 100)]
    assert sorted(paths, key=build_evidence_image_sort_key) == expected


@pytest.mark.parametrize(
    "filename",
    (
        "frame-2.jpg",
        "123-2.jpg",
        "g" * 32 + "-2.jpg",
        FIRST_CAPTURE_ID[:-1] + "-2.jpg",
        "ABCDEF0123454ABC8DEF0123456789AB-2.jpg",
        FIRST_CAPTURE_ID + "-02.jpg",
        FIRST_CAPTURE_ID + "--2.jpg",
        FIRST_CAPTURE_ID + "-２.jpg",
        FIRST_CAPTURE_ID + "-2.JPG",
        FIRST_CAPTURE_ID + "-2.jpeg",
        FIRST_CAPTURE_ID + "-2.jpg.partial",
    ),
)
def test_nonstandard_evidence_names_keep_lexical_key(filename: str) -> None:
    """验证未按实际生成格式命名的文件不被当作 SDK 帧号。

    Args:
        filename: 非标准证据文件名。

    Returns:
        None  # 使用完整文件名词法键并保留原始大小写作为稳定次序
    """
    assert build_evidence_image_sort_key(Path(filename)) == (
        filename.casefold(),
        -1,
        filename.casefold(),
        filename,
    )


def test_mixed_capture_groups_and_legacy_names_have_deterministic_order() -> None:
    """验证多个采集编号分别排帧号，并与旧名称使用可比较的稳定键。

    Args:
        无。

    Returns:
        None  # 输入遍历顺序不影响采集分组、数值帧顺序或词法回退
    """
    filenames = (
        f"{SECOND_CAPTURE_ID}-1.jpg",
        "frame-2.jpg",
        f"{FIRST_CAPTURE_ID}-10.jpg",
        "a.jpg",
        f"{SECOND_CAPTURE_ID}-10.jpg",
        f"{FIRST_CAPTURE_ID}-2.jpg",
        "A.jpg",
        f"{SECOND_CAPTURE_ID}-2.jpg",
        "frame-10.jpg",
        f"{FIRST_CAPTURE_ID}-1.jpg",
    )
    expected = [
        f"{FIRST_CAPTURE_ID}-1.jpg",
        f"{FIRST_CAPTURE_ID}-2.jpg",
        f"{FIRST_CAPTURE_ID}-10.jpg",
        f"{SECOND_CAPTURE_ID}-1.jpg",
        f"{SECOND_CAPTURE_ID}-2.jpg",
        f"{SECOND_CAPTURE_ID}-10.jpg",
        "A.jpg",
        "a.jpg",
        "frame-10.jpg",
        "frame-2.jpg",
    ]

    # 正序和反序输入都使用同一组文件名期望值。
    for input_names in (filenames, tuple(reversed(filenames))):
        paths = [Path(filename) for filename in input_names]
        assert [path.name for path in sorted(paths, key=build_evidence_image_sort_key)] == expected
