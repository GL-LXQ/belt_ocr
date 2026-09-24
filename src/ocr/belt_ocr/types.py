from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class OCRLine:
    text: str
    bbox: List[int]
    confidence: float
    detection_confidence: Optional[float] = None


@dataclass
class OCRBlock:
    bbox: List[int]
    lines: List[OCRLine]
