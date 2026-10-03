"""提供授权证据列表、延迟图片读取及桌面文件夹解析接口。"""

from io import BytesIO
from threading import BoundedSemaphore
from typing import Literal
from urllib.parse import quote
import warnings

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from starlette.background import BackgroundTask

from src.service.evidence_service import EvidenceAccessError, EvidenceService
from src.service.measurement_record_service import MeasurementRecordService, MeasurementRecordServiceError


THUMBNAIL_SLOTS = BoundedSemaphore(2)


class EvidenceImageData(BaseModel):
    """说明一张无需立即加载的证据图片。"""

    image_id: str
    filename: str
    url: str
    thumbnail_url: str


class EvidencePageData(BaseModel):
    """说明一个证据目录的当前分页结果。"""

    images: list[EvidenceImageData]
    state: Literal["available", "no_jpg", "missing_directory", "access_denied", "read_error"]
    count: int | None
    page: int
    page_size: int
    total_pages: int


class EvidencePageResponse(BaseModel):
    """统一封装证据列表响应。"""

    success: bool = True
    data: EvidencePageData
    message: str = ""


class EvidenceDirectoryData(BaseModel):
    """说明记录授权且当前可访问的目录。"""

    directory: str


class EvidenceDirectoryResponse(BaseModel):
    """统一封装桌面文件夹解析响应。"""

    success: bool = True
    data: EvidenceDirectoryData
    message: str = ""


def create_evidence_router(service: MeasurementRecordService | None = None) -> APIRouter:
    """为既有测量服务创建证据路由，认证由主路由统一执行。

    Args:
        service: 测试使用的测量服务，生产请求从应用生命周期取得。

    Returns:
        APIRouter()  # 包含三个证据端点的路由
    """
    router = APIRouter()

    def resolve_service(request: Request) -> EvidenceService:
        """从应用生命周期或测试注入取得证据服务。

        Args:
            request: 当前 HTTP 请求。

        Returns:
            EvidenceService(...)  # 复用本次应用的测量服务
        """
        records = service if service is not None else request.app.state.controller.measurement_record_service
        return EvidenceService(records)

    @router.get("/records/{session_id}/evidence", response_model=EvidencePageResponse)
    def read_evidence_page(
        session_id: str,
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(24, ge=1, le=100),
    ):
        """枚举当前目录并只返回当前页文件身份。

        Args:
            session_id: 测量周期编号。
            request: 当前 HTTP 请求。
            page: 从一开始的页码。
            page_size: 当前页最大图片数。

        Returns:
            EvidencePageResponse(...)  # 图片身份、状态、数量与分页字段
        """
        try:
            files, state = resolve_service(request).list_files(session_id)
        except (EvidenceAccessError, MeasurementRecordServiceError) as error:
            return evidence_error_response(error)

        # 目录异常时不伪造图片数量，成功枚举后才显示现存数量。
        count = len(files) if state in ("available", "no_jpg") else None
        total_pages = max(1, (len(files) + page_size - 1) // page_size)
        images = []
        for image in files[(page - 1) * page_size:page * page_size]:
            url = f"/api/v1/records/{quote(session_id, safe='')}/images/{image.image_id}"
            images.append(EvidenceImageData(
                image_id=image.image_id,
                filename=image.path.name,
                url=url,
                thumbnail_url=f"{url}?thumbnail=true",
            ))
        return EvidencePageResponse(data=EvidencePageData(
            images=images,
            state=state,
            count=count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        ))

    @router.get("/records/{session_id}/evidence-directory", response_model=EvidenceDirectoryResponse)
    def read_evidence_directory(session_id: str, request: Request):
        """为桌面壳解析真实记录的证据文件夹。

        Args:
            session_id: 测量周期编号。
            request: 当前 HTTP 请求。

        Returns:
            EvidenceDirectoryResponse(data=EvidenceDirectoryData(directory="D:/evidence"))  # 已授权目录
        """
        try:
            directory = resolve_service(request).resolve_directory(session_id)
            return EvidenceDirectoryResponse(data=EvidenceDirectoryData(directory=str(directory)))
        except (EvidenceAccessError, MeasurementRecordServiceError) as error:
            return evidence_error_response(error)

    @router.get("/records/{session_id}/images/{image_id}")
    def read_evidence_image(session_id: str, image_id: str, request: Request, thumbnail: bool = False):
        """按不透明图片身份读取缩略图或流式原图。

        Args:
            session_id: 测量周期编号。
            image_id: 列表接口提供的图片编号。
            request: 当前 HTTP 请求。
            thumbnail: 是否生成有限尺寸的缩略图。

        Returns:
            Response(...)  # JPEG 字节或统一中文错误响应
        """
        try:
            image_file, filename, size = resolve_service(request).open_image(session_id, image_id)
        except (EvidenceAccessError, MeasurementRecordServiceError) as error:
            return evidence_error_response(error)

        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        if thumbnail:
            try:
                # 保持 Pillow 解压上限，缩略图最大边长为 640 像素。
                with image_file, THUMBNAIL_SLOTS, warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(image_file) as source:
                        if source.format != "JPEG":
                            raise UnidentifiedImageError("证据不是 JPEG 图片。")
                        source.thumbnail((640, 640))
                        buffer = BytesIO()
                        source.convert("RGB").save(buffer, format="JPEG", quality=82)
                return Response(buffer.getvalue(), media_type="image/jpeg", headers=headers)
            except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                return evidence_error_response(EvidenceAccessError("图片无法解码，原始文件仍保留。", 422))

        # 原图分块读取，不在服务端缓存完整图片。
        def iterate_image():
            """逐块交付已固定句柄的图片并释放文件。

            Args:
                无。

            Returns:
                bytes  # 每块最多 65536 字节的 JPEG 内容
            """
            try:
                while chunk := image_file.read(65536):
                    yield chunk
            finally:
                image_file.close()

        headers["Content-Length"] = str(size)
        headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(filename, safe='')}"
        return StreamingResponse(
            iterate_image(),
            media_type="image/jpeg",
            headers=headers,
            background=BackgroundTask(image_file.close),
        )

    return router


def evidence_error_response(error: Exception) -> JSONResponse:
    """按现有接口语义封装证据故障。

    Args:
        error: 文件访问或测量服务故障。

    Returns:
        JSONResponse(...)  # success=False、data=None、message=中文提示
    """
    status_code = error.status_code if isinstance(error, EvidenceAccessError) else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "message": str(error),
        },
    )
