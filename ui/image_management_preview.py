"""仅在图片管理演示入口中使用的内存记录和绘制占位图。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap


REVIEW_CAPTIONS = {
    "normal": "正常",
    "pending": "待复核",
    "reviewed": "已复核",
}
EVIDENCE_MESSAGES = {
    "missing_directory": ("证据目录不存在", "暂时无法读取图片列表，测量信息仍保留。"),
    "no_jpg": ("目录中没有 JPG", "当前目录没有可展示的 JPG / JPEG 图片。"),
    "access_denied": ("没有目录读取权限", "暂时无法读取图片列表，请检查目录访问权限。"),
    "corrupt": ("图片无法解码", "文件仍保留在本次图片列表中，可继续查看其他图片。"),
}


@dataclass(frozen=True)
class PreviewEvidenceImage:
    """保存一张虚构图片的展示参数，不引用磁盘文件。"""

    filename: str
    number: int
    size: tuple[int, int] = (1200, 420)
    readable: bool = True


@dataclass(frozen=True)
class PreviewMeasurement:
    """保存一次虚构测量及其分组图片，仅供 UI 预览。"""

    session_id: str
    machine_id: str
    machine_name: str
    finished_at: datetime
    review_status: str
    recognized_lines: tuple[str, ...]
    reviewed_lines: tuple[str, ...] | None
    frequency: float | None
    images: tuple[PreviewEvidenceImage, ...]
    evidence_state: str = "available"

    @property
    def effective_lines(self) -> tuple[str, ...]:
        """优先返回人工最终文字，未改写时返回 OCR 文字。

        Args:
            无。

        Returns:
            ("DEMO0001", "A01")  # 本次演示测量的有效文字行
        """
        return self.reviewed_lines if self.reviewed_lines is not None else self.recognized_lines

    @property
    def image_count_caption(self) -> str:
        """生成当前图片数量或无法读取时的提示。

        Args:
            无。

        Returns:
            "现存 3 张 · 演示"  # 虚构目录中列出的图片数，不表示证据完整
            "数量未知 · 演示"  # 目录缺失或访问被拒绝
        """
        if self.evidence_state in ("missing_directory", "access_denied"):
            return "数量未知 · 演示"
        return f"现存 {len(self.images)} 张 · 演示"


def build_preview_measurements() -> tuple[PreviewMeasurement, ...]:
    """创建与业务记录完全隔离的虚构测量和异常展示场景。

    Args:
        无。

    Returns:
        (PreviewMeasurement(...),)  # 按虚构完成时间倒序排列的内存演示记录
    """
    records = []
    states = ("available", "available", "available", "corrupt", "missing_directory", "no_jpg", "access_denied")
    for record_index in range(1, 15):
        # 为每次演示测量分配明确的虚构编号和机器名称。
        machine_number = (record_index - 1) % 3 + 1
        session_id = f"DEMO-{record_index:04d}"
        recognized_lines = (f"DEMO20260927{record_index:08d}", f"D{record_index:07d}", "A01", "01")
        review_status = ("normal", "pending", "reviewed")[(record_index - 1) % 3]
        evidence_state = states[record_index - 1] if record_index <= len(states) else "available"

        # 用内存参数描述图片，不创建目录或 JPG 文件。
        image_count = (3, 5, 2)[(record_index - 1) % 3]
        images = tuple(
            PreviewEvidenceImage(
                filename=f"demo-{record_index:04d}-{image_number:02d}.jpg",
                number=image_number,
                size=(540, 900) if image_number == 2 else (1200, 420),
                readable=not (evidence_state == "corrupt" and image_number == 1),
            )
            for image_number in range(1, image_count + 1)
        )
        if evidence_state in ("missing_directory", "no_jpg", "access_denied"):
            images = ()

        # 覆盖未改写确认、人工改写、空文字和无频率的布局。
        reviewed_lines = None
        if review_status == "reviewed" and record_index != 6:
            reviewed_lines = (f"DEMO20260927{record_index:08d}", "EDIT0001", "B02", "02")
        if record_index == 8:
            recognized_lines = ()
        records.append(PreviewMeasurement(
            session_id=session_id,
            machine_id=f"DEMO-M{machine_number}",
            machine_name=f"演示 {machine_number} 号皮带机",
            finished_at=datetime(2026, 9, 27, 15, 30) - timedelta(hours=record_index - 1),
            review_status=review_status,
            recognized_lines=recognized_lines,
            reviewed_lines=reviewed_lines,
            frequency=None if record_index == 2 else 50.0 + (record_index % 3) * 0.5,
            images=images,
            evidence_state=evidence_state,
        ))
    return tuple(records)


def draw_preview_image(image: PreviewEvidenceImage) -> QPixmap:
    """在内存中绘制带 DEMO 标识的证据占位图。

    Args:
        image: 虚构图片的尺寸、编号及可读状态。

    Returns:
        QPixmap()  # 可读图片返回绘制结果，损坏场景返回空图
    """
    if not image.readable:
        return QPixmap()

    # 绘制低对比背景和简化的皮带区域。
    width, height = image.size
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#E7ECF2"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#536276"))
    painter.drawRoundedRect(QRect(36, height // 4, width - 72, height // 2), 14, 14)

    # 在占位图中央明确显示演示标识。
    font = QFont("Sans Serif")
    font.setPixelSize(max(28, min(width // 18, 54)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, f"DEMO  {image.number:02d}")

    # 在图片边缘说明这不是采集照片。
    font.setPixelSize(max(18, min(width // 32, 28)))
    font.setBold(False)
    painter.setFont(font)
    painter.setPen(QColor("#667085"))
    caption_rect = QRect(20, height - 72, width - 40, 52)
    painter.drawText(caption_rect, Qt.AlignmentFlag.AlignCenter, "UI PREVIEW · 非采集图片")
    painter.end()
    return pixmap
