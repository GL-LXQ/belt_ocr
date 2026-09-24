from __future__ import annotations

from typing import Iterable, List

from .types import OCRBlock, OCRLine


def _width(b):
    return max(1, b[2] - b[0])


def _height(b):
    return max(1, b[3] - b[1])


def _x_overlap_ratio(a, b) -> float:
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    return overlap / max(1, min(_width(a), _width(b)))


def _center_x(b) -> float:
    return (b[0] + b[2]) / 2.0


def _vertical_gap(a, b) -> int:
    if a[3] < b[1]:
        return b[1] - a[3]
    if b[3] < a[1]:
        return a[1] - b[3]
    return 0


def _same_block(
    a: OCRLine,
    b: OCRLine,
    horizontal_overlap_ratio: float,
    center_x_tolerance_ratio: float,
    max_vertical_gap_ratio: float,
) -> bool:
    ba, bb = a.bbox, b.bbox
    h_ok = _x_overlap_ratio(ba, bb) >= horizontal_overlap_ratio
    center_tol = max(_width(ba), _width(bb)) * center_x_tolerance_ratio
    c_ok = abs(_center_x(ba) - _center_x(bb)) <= center_tol
    if not (h_ok or c_ok):
        return False

    gap = _vertical_gap(ba, bb)
    height_ref = max(_height(ba), _height(bb))
    return gap <= height_ref * max_vertical_gap_ratio


def group_lines_into_blocks(
    lines: Iterable[OCRLine],
    horizontal_overlap_ratio: float = 0.30,
    center_x_tolerance_ratio: float = 0.45,
    max_vertical_gap_ratio: float = 1.8,
    min_lines_per_block: int = 1,
) -> List[OCRBlock]:
    items = list(lines)
    n = len(items)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if _same_block(
                items[i], items[j], horizontal_overlap_ratio,
                center_x_tolerance_ratio, max_vertical_gap_ratio
            ):
                union(i, j)

    groups: dict[int, list[OCRLine]] = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)

    blocks: list[OCRBlock] = []
    for group in groups.values():
        if len(group) < min_lines_per_block:
            continue
        group.sort(key=lambda x: ((x.bbox[1] + x.bbox[3]) / 2.0, x.bbox[0]))
        x1 = min(x.bbox[0] for x in group)
        y1 = min(x.bbox[1] for x in group)
        x2 = max(x.bbox[2] for x in group)
        y2 = max(x.bbox[3] for x in group)
        blocks.append(OCRBlock(bbox=[x1, y1, x2, y2], lines=group))

    blocks.sort(key=lambda b: (b.bbox[0], b.bbox[1]))
    return blocks
