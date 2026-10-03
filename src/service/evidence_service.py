"""按数据库记录授权读取现存证据，不接受外部文件路径。"""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from src.service.measurement_record_service import MeasurementRecordService
from ui.evidence_order import build_evidence_image_sort_key


@dataclass(frozen=True)
class EvidenceFile:
    """保存当前目录枚举得到的文件身份。"""

    image_id: str
    path: Path


class EvidenceAccessError(Exception):
    """保存可展示的证据访问故障及 HTTP 状态。"""

    def __init__(self, message: str, status_code: int = 404) -> None:
        """保存本次证据访问失败。

        Args:
            message: 可展示的中文提示。
            status_code: 对应 HTTP 状态码。

        Returns:
            None  # 错误对象已初始化
        """
        super().__init__(message)
        self.status_code = status_code


class EvidenceService:
    """复用测量服务与共享排序规则，提供有限文件访问。"""

    def __init__(self, records: MeasurementRecordService) -> None:
        """保存测量记录查询服务。

        Args:
            records: 现有测量记录服务。

        Returns:
            None  # 服务已初始化
        """
        self.records = records

    def read_saved_directory(self, session_id: str) -> Path:
        """从真实测量记录取得未经外部请求改写的目录。

        Args:
            session_id: 请求查看的测量周期编号。

        Returns:
            Path("D:/evidence/session")  # 记录保存的绝对路径
        """
        record = self.records.get_record(session_id)["record"]
        if record is None:
            raise EvidenceAccessError("测量记录不存在。")

        # 只使用记录保存的路径，配置迁移后仍可查看旧证据。
        saved_directory = record["evidence_directory"]
        if not saved_directory:
            raise EvidenceAccessError("该记录未保存证据目录。")
        directory = Path(saved_directory)
        if not directory.is_absolute():
            raise EvidenceAccessError("证据目录不是绝对路径，请核对原始记录。", 409)
        return directory

    def resolve_directory(self, session_id: str) -> Path:
        """核对数据库目录存在且不经符号链接跳转。

        Args:
            session_id: 请求查看的测量周期编号。

        Returns:
            Path("D:/evidence/session")  # 已验证的实际证据目录
        """
        directory = self.read_saved_directory(session_id)
        try:
            # 拒绝目录本身及父目录中的链接或 Windows 目录联接。
            for ancestor in (directory, *directory.parents):
                if ancestor.is_symlink() or ancestor.is_junction():
                    raise EvidenceAccessError("证据目录包含链接，无法安全打开。", 403)
            resolved = directory.resolve(strict=True)
            if not resolved.is_dir():
                raise EvidenceAccessError("证据目录不存在或已不是文件夹。")
            return resolved
        except PermissionError as error:
            raise EvidenceAccessError("没有证据目录访问权限。", 403) from error
        except (FileNotFoundError, NotADirectoryError) as error:
            raise EvidenceAccessError("证据目录不存在。") from error
        except OSError as error:
            raise EvidenceAccessError("证据目录读取失败。", 409) from error

    def list_files(self, session_id: str) -> tuple[list[EvidenceFile], str]:
        """仅枚举一条记录目录当前层的 JPG 和 JPEG 文件。

        Args:
            session_id: 请求查看的测量周期编号。

        Returns:
            (
                [EvidenceFile("sha256", Path("D:/evidence/frame.jpg"))],  # 当前证据文件
                "available",  # 目录读取状态
            )
        """
        # 未知周期保持错误，不把无权访问伪装为空目录。
        self.read_saved_directory(session_id)
        try:
            directory = self.resolve_directory(session_id)
            files = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    if Path(entry.name).suffix.lower() not in (".jpg", ".jpeg"):
                        continue
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    image_id = hashlib.sha256(entry.name.encode("utf-8")).hexdigest()
                    files.append(EvidenceFile(image_id, Path(entry.path)))
        except EvidenceAccessError as error:
            states = {404: "missing_directory", 403: "access_denied"}
            return [], states.get(error.status_code, "read_error")
        except PermissionError:
            return [], "access_denied"
        except OSError:
            return [], "read_error"

        # 与保留的历史界面共用数值帧号排序，不复制排序规则。
        files.sort(key=lambda image: build_evidence_image_sort_key(image.path))
        return files, "available" if files else "no_jpg"

    def open_image(self, session_id: str, image_id: str) -> tuple[BinaryIO, str, int]:
        """用枚举得到的图片身份打开当前目录内的常规文件。

        Args:
            session_id: 请求查看的测量周期编号。
            image_id: 列表接口返回的不透明图片编号。

        Returns:
            (
                file_object,  # 调用方负责关闭的二进制文件
                "frame.jpg",  # 原始图片文件名
                1024,  # 文件字节数
            )
        """
        files, state = self.list_files(session_id)
        if state not in ("available", "no_jpg"):
            raise EvidenceAccessError("证据目录不可读取，请刷新后重试。")
        image = next((image for image in files if image.image_id == image_id), None)
        if image is None:
            raise EvidenceAccessError("图片已不存在，请刷新图片列表。")

        # 打开时再次拒绝链接，文件句柄固定本次响应读取的对象。
        image_file = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(image.path, flags)
            image_file = os.fdopen(descriptor, "rb")
            opened = os.fstat(image_file.fileno())
            directory = self.resolve_directory(session_id)
            current = image.path.lstat()
            if image.path.resolve(strict=True).parent != directory or image.path.is_symlink():
                raise EvidenceAccessError("图片路径已改变，请刷新后重试。", 409)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise EvidenceAccessError("图片已改变，请刷新后重试。", 409)
            return image_file, image.path.name, opened.st_size
        except (OSError, EvidenceAccessError) as error:
            if image_file is not None:
                image_file.close()
            if isinstance(error, EvidenceAccessError):
                raise
            status_code = 403 if isinstance(error, PermissionError) else 404
            raise EvidenceAccessError("图片不可读取，请刷新后重试。", status_code) from error
