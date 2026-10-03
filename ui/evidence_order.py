"""为证据预览和图片查看提供一致的帧显示顺序。"""

from pathlib import Path
import re


EVIDENCE_FRAME_NAME = re.compile(r"([0-9a-f]{32})-(0|[1-9][0-9]*)\.jpg")


def build_evidence_image_sort_key(path: Path) -> tuple[str, int, str, str]:
    """按采集编号分组并按数值帧号排序，其他名称按词法排序。

    Args:
        path: 当前证据图片路径，只读取文件名，不访问磁盘。

    Returns:
        返回示例：
            (
                "0123456789ab4def8123456789abcdef",  # 采集编号或折叠大小写的完整文件名
                2,  # 标准文件的数值帧号，其他文件为 -1
                "0123456789ab4def8123456789abcdef-2.jpg",  # 折叠大小写的完整文件名
                "0123456789ab4def8123456789abcdef-2.jpg",  # 原始文件名，用于稳定处理同名键
            )
    """
    filename = path.name
    folded_filename = filename.casefold()
    frame_match = EVIDENCE_FRAME_NAME.fullmatch(filename)

    # 标准文件使用采集编号和帧号，其他文件保留词法顺序。
    return (
        frame_match.group(1) if frame_match else folded_filename,
        int(frame_match.group(2)) if frame_match else -1,
        folded_filename,
        filename,
    )
