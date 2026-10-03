"""图片管理的测量展示数据和有限后台读取。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable
import os

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader

from src.controller.controller import AppController


REVIEW_CAPTIONS = {
    "normal": "正常",
    "pending": "待复核",
    "reviewed": "已复核",
}
EVIDENCE_MESSAGES = {
    "loading": ("正在读取图片", "仅读取当前测量保存的证据目录。"),
    "missing_directory": ("证据目录不存在", "暂时无法读取图片列表，测量信息仍保留。"),
    "no_jpg": ("目录中没有 JPG", "当前目录没有可展示的 JPG / JPEG 图片。"),
    "access_denied": ("没有目录读取权限", "暂时无法读取图片列表，请检查目录访问权限。"),
    "read_error": ("证据目录读取失败", "请检查目录是否可访问，测量信息仍保留。"),
    "corrupt": ("图片无法解码", "文件仍保留在本次图片列表中，可继续查看其他图片。"),
    "image_missing": ("图片已不存在", "目录读取后文件发生变化，可刷新图片列表。"),
    "image_denied": ("没有图片读取权限", "文件仍保留在本次图片列表中，请检查文件访问权限。"),
}


@dataclass(frozen=True)
class EvidenceImage:
    """保存当前测量中一张现存 JPG 的路径，不缓存原图。"""

    path: Path

    @property
    def filename(self) -> str:
        """返回目录中保存的实际文件名。

        Args:
            无。

        Returns:
            "frame-001.JPG"  # 未改写的证据文件名
        """
        return self.path.name


@dataclass(frozen=True)
class EvidenceMeasurement:
    """保存一条真实测量的展示字段与当前目录读取状态。"""

    session_id: str
    machine_id: str
    machine_name: str
    finished_at: datetime
    review_status: str
    recognized_lines: tuple[str, ...]
    reviewed_lines: tuple[str, ...] | None
    frequency: float | None
    evidence_directory: str
    images: tuple[EvidenceImage, ...] = ()
    evidence_state: str = "loading"
    image_count: int | None = None

    @property
    def effective_lines(self) -> tuple[str, ...]:
        """优先返回非 NULL 的人工文字，否则返回正式 OCR 文字。

        Args:
            无。

        Returns:
            ("ABC00001", "A01")  # 本次测量的当前有效文字
        """
        return self.reviewed_lines if self.reviewed_lines is not None else self.recognized_lines

    @property
    def image_count_caption(self) -> str:
        """显示当前读取到的文件数，不推断证据是否完整。

        Args:
            无。

        Returns:
            "现存 3 张"  # 当前目录列出的 JPG / JPEG 文件数
            "数量未知"  # 尚未读取或目录无法读取
        """
        return "数量未知" if self.image_count is None else f"现存 {self.image_count} 张"


@dataclass(frozen=True)
class EvidenceListing:
    """保存单个证据目录的文件列表及读取状态。"""

    images: tuple[EvidenceImage, ...]
    state: str


@dataclass(frozen=True)
class EvidenceDecodedImage:
    """保存后台解码的当前图片或缩略图及失败状态。"""

    image: QImage
    state: str


@dataclass(frozen=True)
class EvidenceCardImage:
    """保存卡片所需的数量和一张缩略图，不保留整组原图。"""

    count: int | None
    thumbnail: QImage
    filename: str
    state: str


@dataclass(frozen=True)
class EvidenceReadFailure:
    """将后台读取故障传回主线程展示。"""

    message: str


class EvidenceReadSignals(QObject):
    """把一次有限读取的结果送回主线程。"""

    completed = Signal(int, object, object)


class EvidenceReadTask(QRunnable):
    """执行图片页面的一次读取，取消后不再发送结果。"""

    def __init__(
        self,
        generation: int,
        identity: object,
        read: Callable[[], object],
        cancelled: Event,
        signals: EvidenceReadSignals,
    ) -> None:
        """保存本次读取和页面代次，不创建常驻工作线程。

        Args:
            generation: 发起读取时的页面或查看器代次。
            identity: 当前记录或图片的位置标识。
            read: 只执行一次的数据库或文件读取。
            cancelled: 本代次失效时置位的取消标志。
            signals: 在线程池存活期间保留的结果信号。

        Returns:
            None  # 有限读取任务已准备，尚未开始运行
        """
        super().__init__()
        self.generation = generation
        self.identity = identity
        self.read = read
        self.cancelled = cancelled
        self.signals = signals

    def run(self) -> None:
        """执行一次读取，并忽略取消后的结果。

        Args:
            无。

        Returns:
            None  # 当前结果或错误已发送，取消的读取直接结束
        """
        if self.cancelled.is_set():
            return
        try:
            result = self.read()
        except Exception as error:
            result = EvidenceReadFailure(str(error))

        # 不让离页或新请求之前的结果进入界面。
        if not self.cancelled.is_set():
            self.signals.completed.emit(self.generation, self.identity, result)


def build_evidence_measurement(record: dict, listing: EvidenceListing | None = None) -> EvidenceMeasurement:
    """将现有测量服务返回的记录整理为图片展示数据。

    Args:
        record: 已解码文字字段的测量列表或详情记录。
        listing: 仅在打开组内查看器时传入的当前目录列表。

    Returns:
        EvidenceMeasurement(...)  # 含原样证据目录和本地完成时间的测量
    """
    status = "normal"
    if record["needs_review"]:
        status = "pending" if record["reviewed_at"] is None else "reviewed"

    # 只使用记录保存的目录，不读取当前采集配置。
    image_count = None
    if listing is not None and listing.state in ("available", "no_jpg"):
        image_count = len(listing.images)
    return EvidenceMeasurement(
        session_id=record["session_id"],
        machine_id=record["machine_id"],
        machine_name=record["machine_name"],
        finished_at=datetime.fromisoformat(record["finish_time"]).astimezone(),
        review_status=status,
        recognized_lines=record["recognized_lines"],
        reviewed_lines=record["reviewed_lines"],
        frequency=record["final_frequency_hz"],
        evidence_directory=record["evidence_directory"],
        images=listing.images if listing is not None else (),
        evidence_state=listing.state if listing is not None else "loading",
        image_count=image_count,
    )


def list_evidence_images(evidence_directory: str, cancelled: Event) -> EvidenceListing:
    """只枚举指定记录目录当前层中的现存 JPG 和 JPEG 文件。

    Args:
        evidence_directory: 测量记录保存的原始目录字符串。
        cancelled: 离页或切换记录后的取消标志。

    Returns:
        EvidenceListing((), "missing_directory")  # 指定目录不存在
        EvidenceListing((EvidenceImage(...),), "available")  # 当前目录文件列表
    """
    if cancelled.is_set():
        return EvidenceListing((), "loading")
    if not evidence_directory:
        return EvidenceListing((), "missing_directory")
    images = []
    try:
        # 读取单个目录，不递归查找其他测量或配置中的根目录。
        with os.scandir(evidence_directory) as entries:
            for entry in entries:
                if cancelled.is_set():
                    return EvidenceListing((), "loading")
                if Path(entry.name).suffix.lower() in (".jpg", ".jpeg") and entry.is_file():
                    images.append(EvidenceImage(Path(entry.path)))
    except (FileNotFoundError, NotADirectoryError):
        return EvidenceListing((), "missing_directory")
    except PermissionError:
        return EvidenceListing((), "access_denied")
    except OSError:
        return EvidenceListing((), "read_error")

    # 排序只影响组内显示，不推断文件数量或采集完整性。
    images.sort(key=lambda image: (image.filename.casefold(), image.filename))
    return EvidenceListing(tuple(images), "available" if images else "no_jpg")


def decode_evidence_image(path: Path, thumbnail_size: QSize | None = None) -> EvidenceDecodedImage:
    """在工作线程中读取 QImage，缩略图优先按目标大小解码。

    Args:
        path: 当前选中的实际图片路径。
        thumbnail_size: 缩略图边界；None 表示只读取当前一张原图。

    Returns:
        EvidenceDecodedImage(QImage(...), "available")  # 已解码图片
        EvidenceDecodedImage(QImage(), "corrupt")  # 文件存在但无法解码
    """
    try:
        # 单独打开文件以区分权限拒绝、文件消失和格式损坏。
        with path.open("rb"):
            pass
    except FileNotFoundError:
        return EvidenceDecodedImage(QImage(), "image_missing")
    except PermissionError:
        return EvidenceDecodedImage(QImage(), "image_denied")
    except OSError:
        return EvidenceDecodedImage(QImage(), "corrupt")

    # 使用 QImageReader 的缩小解码避免为卡片保留整张原图。
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if thumbnail_size is not None and reader.size().isValid():
        reader.setScaledSize(reader.size().scaled(thumbnail_size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return EvidenceDecodedImage(image, "corrupt" if image.isNull() else "available")


def read_card_evidence(evidence_directory: str, cancelled: Event) -> EvidenceCardImage:
    """读取当前卡片的目录数量和第一张可解码的缩略图。

    Args:
        evidence_directory: 当前卡片记录保存的证据目录。
        cancelled: 页面代次取消标志，逐文件检查。

    Returns:
        EvidenceCardImage(3, QImage(...), "002.jpg", "available")  # 当前数量和首张可用缩略图
        EvidenceCardImage(None, QImage(), "", "access_denied")  # 目录无法读取
    """
    listing = list_evidence_images(evidence_directory, cancelled)
    if listing.state != "available":
        count = 0 if listing.state == "no_jpg" else None
        return EvidenceCardImage(count, QImage(), "", listing.state)

    # 每次只解码一张缩略图，遇到坏图继续查找当前组的下一张。
    first_failure = None
    for image in listing.images:
        if cancelled.is_set():
            return EvidenceCardImage(None, QImage(), "", "loading")
        decoded = decode_evidence_image(image.path, QSize(640, 240))
        if not decoded.image.isNull():
            return EvidenceCardImage(len(listing.images), decoded.image, image.filename, "available")
        if first_failure is None:
            first_failure = (image.filename, decoded.state)

    # 全组不可解码时仍保留当前文件数和第一张失败文件名。
    filename, state = first_failure
    return EvidenceCardImage(len(listing.images), QImage(), filename, state)


def read_measurement_evidence(
    controller: AppController,
    session_id: str,
    cancelled: Event,
) -> EvidenceMeasurement | None:
    """重新读取点击的测量详情，再枚举该详情保存的证据目录。

    Args:
        controller: 现有测量 Controller。
        session_id: 当前点击的测量编号。
        cancelled: 当前查看器请求的取消标志。

    Returns:
        EvidenceMeasurement(...)  # 最新测量详情及单个目录的文件列表
        None  # 测量已不存在或读取已取消
    """
    if cancelled.is_set():
        return None
    result = controller.get_measurement_record(session_id)
    if not result.success:
        raise RuntimeError(result.message)
    record = result.data["record"]
    if record is None or cancelled.is_set():
        return None

    # 详情和目录列表只服务于当前选中的测量。
    listing = list_evidence_images(record["evidence_directory"], cancelled)
    return build_evidence_measurement(record, listing)
