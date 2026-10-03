"""通过 FastAPI 复用现有业务服务并托管独立监测生命周期。"""

import asyncio
from contextlib import asynccontextmanager, closing
from dataclasses import fields
from datetime import date
from functools import partial
import json
import logging
import math
from pathlib import Path
import secrets
import sqlite3
from typing import Annotated, Literal

from pydantic import JsonValue

from fastapi import FastAPI, Path as ApiPath, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, StreamingResponse

from config_util import AppConfig, read_configuration_settings
from repo.abnormal_event_repo import AbnormalEventRepo
from repo.machine_repo import MachineRepo
from repo.measurement_record_repo import MeasurementRecordRepo
from runtime.runtime_host import RuntimeHost
from src.controller.controller import AppController
from src.controller.result import Result
from src.service.abnormal_event_service import AbnormalEventService
from src.service.machine_service import MachineService
from src.service.measurement_record_service import MeasurementRecordService
from src.api.evidence_routes import create_evidence_router
from src.api.models import (
    ApiResult, ConfigurationInput, ConfigurationSaveInput, MachineInput, ReviewInput, RuntimeSnapshot,
)
from src.api.security import DEFAULT_ORIGINS, LocalSessionMiddleware


logger = logging.getLogger(__name__)


def make_response(result: Result, failure_status: int = 400) -> JSONResponse:
    """把业务结果严格转换为 JSON 响应。

    Args:
        result: 业务控制器返回的纯数据结果。
        failure_status: 业务失败时使用的 HTTP 状态码。

    Returns:
        JSONResponse(...)  # 包含 success、data 和 message 的统一响应
    """
    content = ApiResult[JsonValue].model_validate(jsonable_encoder(result)).model_dump(mode="json")
    return JSONResponse(content, status_code=200 if result.success else failure_status)


def make_runtime_response(result: Result, failure_status: int = 400) -> JSONResponse:
    """校验完整快照结构后构造运行命令响应。

    Args:
        result: 包含完整运行快照的结果。
        failure_status: 命令拒绝时使用的 HTTP 状态码。

    Returns:
        JSONResponse(...)  # data 通过 RuntimeSnapshot 的机器和周期字段校验
    """
    content = ApiResult[RuntimeSnapshot].model_validate(jsonable_encoder(result)).model_dump(mode="json")
    return JSONResponse(content, status_code=200 if result.success else failure_status)


def make_configuration_response(result: Result, failure_status: int = 400) -> JSONResponse:
    """将可修正的非法数值保留为可见文本，并过滤敏感配置字段。

    Args:
        result: 配置读取或保存结果。
        failure_status: 业务拒绝时使用的 HTTP 状态码。

    Returns:
        JSONResponse(...)  # settings 保留可修正值，invalid_fields 列出原始错误字段
    """
    if not result.success:
        return make_response(result, failure_status)
    known_fields = {field.name for field in fields(AppConfig)}
    invalid_fields = []

    def preserve_invalid_number(value, path):
        """递归转换 JSON 无法表达的非有限数值。

        Args:
            value: 配置字段或嵌套值。
            path: 可定位的配置字段路径。

        Returns:
            object  # 正常值保持不变，非有限数值转换为可见原始文本
        """
        if isinstance(value, float) and not math.isfinite(value):
            raw_value = "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
            invalid_fields.append({
                "field": path,
                "raw_value": raw_value,
                "message": "配置包含非有限数值，请输入有效数字。",
            })
            return raw_value
        if isinstance(value, dict):
            return {key: preserve_invalid_number(item, f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [preserve_invalid_number(item, f"{path}.{index}") for index, item in enumerate(value)]
        return value

    result.data["settings"] = {
        key: preserve_invalid_number(value, key) for key, value in result.data["settings"].items()
        if key in known_fields and not any(
            secret in key.lower() for secret in ("password", "token", "secret", "credential", "api_key", "private_key")
        )
    }
    result.data["invalid_fields"] = invalid_fields
    return make_response(result, failure_status)


def create_controller(configuration_directory: Path) -> AppController:
    """从配置建立业务仓储，仅初始化数据库结构而不连接硬件。

    Args:
        configuration_directory: 包含 config.yaml 的目录。

    Returns:
        AppController(...)  # 已绑定真实仓储的无界面业务适配器
    """
    settings = read_configuration_settings(configuration_directory)
    configuration = AppConfig(**settings)
    database_path = configuration.database_path
    recovery_path = configuration.recovery_path

    # 页面查询只初始化业务表，不取得运行库或设备所有权。
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)
    recovery_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(recovery_path)) as connection, connection:
        AbnormalEventRepo.create_table(connection)

    # 业务规则继续由原有服务和仓储执行。
    return AppController(
        MachineService(MachineRepo(database_path)),
        MeasurementRecordService(MeasurementRecordRepo(database_path)),
        AbnormalEventService(AbnormalEventRepo(recovery_path)),
        configuration_directory,
    )


def create_app(
    configuration_directory: Path,
    token: str | None = None,
    ocr_config_path: Path | None = None,
    controller: AppController | None = None,
    runtime_host: RuntimeHost | None = None,
    allowed_origins: frozenset[str] = DEFAULT_ORIGINS,
) -> FastAPI:
    """组装仅支持本机临时会话的 HTTP 和 SSE 应用。

    Args:
        configuration_directory: 公共配置目录。
        token: 宿主通过内存传入的临时令牌，省略时生成新令牌。
        ocr_config_path: 独立 OCR 配置路径。
        controller: 可选的业务适配器，供隔离测试注入。
        runtime_host: 可选的运行宿主，供隔离测试注入。
        allowed_origins: 允许访问的准确浏览器来源。

    Returns:
        FastAPI(...)  # 生命周期、认证、HTTP 命令和 SSE 已登记
    """
    session_token = token or secrets.token_urlsafe(32)
    if len(session_token) < 32:
        raise ValueError("会话令牌至少需要 32 个字符。")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """建立服务依赖，并在 API 生命周期结束前等待现场清理。

        Args:
            application: 当前 FastAPI 应用。

        Returns:
            None  # 上下文结束后监测资源已释放
        """
        adapter = controller or await asyncio.to_thread(create_controller, configuration_directory)
        host = runtime_host or RuntimeHost(
            configuration_directory,
            adapter.machine_service.list_machines,
            ocr_config_path,
        )
        adapter.is_monitoring_active = lambda: host.is_active
        application.state.controller = adapter
        application.state.runtime_host = host
        application.state.shutdown_requested = asyncio.Event()
        await host.refresh_machines()
        try:
            yield
        finally:
            await host.shutdown()

    application = FastAPI(title="BeltVision 本机接口", version="1.0.0", lifespan=lifespan)
    application.state.session_token = session_token
    application.add_middleware(LocalSessionMiddleware, token=session_token, allowed_origins=allowed_origins)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        """将请求参数错误转换为统一结果，不回显原始输入。

        Args:
            request: 当前请求。
            error: 请求模型校验错误。

        Returns:
            JSONResponse(...)  # HTTP 422，包含可定位的字段说明
        """
        errors = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]} for item in error.errors()]
        return make_response(Result.error("请求参数无效。", {"errors": errors}), 422)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        """统一路由和证据访问错误格式。

        Args:
            request: 当前请求。
            error: HTTP 层错误。

        Returns:
            JSONResponse(...)  # 原始 HTTP 状态码和统一错误结果
        """
        return make_response(Result.error(str(error.detail)), error.status_code)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        """记录未知故障并返回不包含堆栈的业务提示。

        Args:
            request: 当前请求。
            error: 未处理异常。

        Returns:
            JSONResponse(...)  # HTTP 500，原始堆栈仅写入本机日志
        """
        logger.error("API 请求处理失败", exc_info=(type(error), error, error.__traceback__))
        response = make_response(Result.error("服务处理失败，请查看本机日志后重试。"), 500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        origin = request.headers.get("origin")
        if origin in allowed_origins:
            response.headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
        return response

    @application.get("/api/v1/health", response_model=ApiResult[JsonValue])
    async def health(request: Request):
        """返回可用于桌面握手校验的版本与就绪状态。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 包含 ready=True 和 protocol=1
        """
        return make_response(Result.ok({"ready": True, "protocol": 1}))

    @application.get("/api/v1/state", response_model=ApiResult[RuntimeSnapshot])
    async def state(request: Request):
        """读取当前完整运行状态。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 为完整机器和周期快照
        """
        return make_runtime_response(Result.ok(request.app.state.runtime_host.snapshot()))

    @application.get("/api/v1/events")
    async def events(request: Request):
        """建立有界 SSE 订阅，连接和重连均从完整快照开始。

        Args:
            request: 当前已认证请求，Last-Event-ID 可由客户端发送。

        Returns:
            StreamingResponse(...)  # 每条 snapshot 事件均含完整 ApiResult JSON
        """
        host = request.app.state.runtime_host
        try:
            queue = host.subscribe()
        except RuntimeError as error:
            return make_response(Result.error(str(error)), 503)

        async def stream_snapshots():
            """发送完整状态和保活注释，结束时释放当前订阅。

            Args:
                无外部参数。

            Returns:
                bytes  # 包含 id、event 和 data 的 SSE 帧或保活注释
            """
            try:
                while not await request.is_disconnected():
                    try:
                        snapshot = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    if snapshot is None:
                        break
                    content = ApiResult[RuntimeSnapshot](success=True, data=snapshot).model_dump(mode="json")
                    payload = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {snapshot['sequence']}\nevent: snapshot\ndata: {payload}\n\n".encode()
            finally:
                host.unsubscribe(queue)
        return StreamingResponse(
            stream_snapshots(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @application.post("/api/v1/monitoring/start", response_model=ApiResult[RuntimeSnapshot])
    async def start_monitoring(request: Request):
        """请求唯一宿主启动监测。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 为启动后的完整快照
        """
        return make_runtime_response(await request.app.state.runtime_host.start(), 409)

    @application.post("/api/v1/monitoring/stop", response_model=ApiResult[RuntimeSnapshot])
    async def stop_monitoring(request: Request):
        """请求监测停止并保持清理期间的写保护。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 为停止请求后的完整快照
        """
        return make_runtime_response(await request.app.state.runtime_host.stop())

    @application.post("/api/v1/shutdown", response_model=ApiResult[JsonValue])
    async def shutdown(request: Request):
        """通知桌面后端入口在完整清理后退出。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.accepted=True，退出完成由进程结束确认
        """
        request.app.state.shutdown_requested.set()
        return make_response(Result.ok({"accepted": True}))

    @application.get("/api/v1/machines", response_model=ApiResult[JsonValue])
    async def list_machines(request: Request):
        """列出所有未删除的机器配置。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.machines 为机器配置列表
        """
        return make_response(await asyncio.to_thread(request.app.state.controller.list_machines))

    @application.post("/api/v1/machines", response_model=ApiResult[JsonValue])
    async def create_machine(body: MachineInput, request: Request):
        """在监测完全停止时新增机器。

        Args:
            body: 机器配置字段。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.machine_id 为新机器编号
        """
        operation = partial(request.app.state.controller.create_machine, **body.model_dump())
        return make_response(await request.app.state.runtime_host.run_mutation(operation), 409)

    @application.put("/api/v1/machines/{machine_id}", response_model=ApiResult[JsonValue])
    async def update_machine(
        machine_id: Annotated[int, ApiPath(ge=1, le=2**63 - 1)],
        body: MachineInput,
        request: Request,
    ):
        """在监测完全停止时修改机器。

        Args:
            machine_id: 待修改机器主键。
            body: 完整机器配置字段。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.machine_id 为已修改机器编号
        """
        operation = partial(request.app.state.controller.update_machine, machine_id, **body.model_dump())
        return make_response(await request.app.state.runtime_host.run_mutation(operation), 409)

    @application.delete("/api/v1/machines/{machine_id}", response_model=ApiResult[JsonValue])
    async def delete_machine(machine_id: Annotated[int, ApiPath(ge=1, le=2**63 - 1)], request: Request):
        """在监测完全停止时软删除机器。

        Args:
            machine_id: 待删除机器主键。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # 成功时 data=None
        """
        operation = partial(request.app.state.controller.delete_machine, machine_id)
        return make_response(await request.app.state.runtime_host.run_mutation(operation), 409)

    @application.get("/api/v1/records", response_model=ApiResult[JsonValue])
    async def list_records(
        request: Request,
        review_status: Literal["normal", "pending", "reviewed"] | None = None,
        machine_id: str | None = None,
        page: Annotated[int, Query(ge=1, le=1_000_000_000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 20,
        start_date: date | None = None,
        end_date: date | None = None,
        text_query: Annotated[str | None, Query(max_length=500)] = None,
        text_match_mode: Literal["contains", "exact"] = "contains",
        text_length: int | None = None,
    ):
        """按原有业务规则分页筛选历史和图片管理记录。

        Args:
            request: 当前已认证请求。
            review_status: 复核状态。
            machine_id: 机器编号。
            page: 页码。
            page_size: 每页条数，上限 200。
            start_date: 本地开始日期。
            end_date: 本地结束日期。
            text_query: 文字查询内容。
            text_match_mode: 包含或精确匹配。
            text_length: 完整行的位数限制。

        Returns:
            ApiResult(...)  # data 包含 records、page、page_size、total 和 total_pages
        """
        if end_date == date.max:
            return make_response(Result.error("结束日期不能晚于 9999-12-30。"), 422)
        operation = partial(
            request.app.state.controller.list_measurement_records,
            review_status, machine_id, page, page_size, start_date, end_date,
            text_query=text_query, text_match_mode=text_match_mode, text_length=text_length,
        )
        return make_response(await asyncio.to_thread(operation))

    @application.get("/api/v1/records/machines", response_model=ApiResult[JsonValue])
    async def record_machines(request: Request):
        """读取历史记录中的机器选项。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.machines 为历史机器列表
        """
        return make_response(await asyncio.to_thread(request.app.state.controller.list_record_machines))

    @application.get("/api/v1/records/summary", response_model=ApiResult[JsonValue])
    async def record_summary(request: Request):
        """读取今天已入库记录和待复核数量。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 包含 recognition_count 和 pending_review_count
        """
        return make_response(await asyncio.to_thread(request.app.state.controller.get_today_measurement_summary))

    @application.get("/api/v1/records/{session_id}", response_model=ApiResult[JsonValue])
    async def record_detail(session_id: str, request: Request):
        """读取指定测量周期的详情。

        Args:
            session_id: 测量周期编号。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.record 为业务记录详情
        """
        result = await asyncio.to_thread(request.app.state.controller.get_measurement_record, session_id)
        if result.success and result.data["record"] is None:
            return make_response(Result.error("未找到测量记录。"), 404)
        return make_response(result)

    @application.post("/api/v1/records/{session_id}/review", response_model=ApiResult[JsonValue])
    async def review_record(session_id: str, body: ReviewInput, request: Request):
        """使用原有审计和幂等规则完成人工复核。

        Args:
            session_id: 待复核周期编号。
            body: 可选人工修改文字。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # 重复复核返回 HTTP 409 并保留原业务提示
        """
        result = await asyncio.to_thread(
            request.app.state.controller.complete_measurement_review, session_id, body.edited_text,
        )
        return make_response(result, 409)

    @application.get("/api/v1/abnormal-events", response_model=ApiResult[JsonValue])
    async def abnormal_events(
        request: Request,
        machine_id: str | None = None,
        session_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        page: Annotated[int, Query(ge=1, le=1_000_000_000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    ):
        """按机器、周期和日期筛选异常事件。

        Args:
            request: 当前已认证请求。
            machine_id: 可选机器编号。
            session_id: 可选完整周期编号。
            start_date: 本地开始日期。
            end_date: 本地结束日期。
            page: 当前页码。
            page_size: 每页条数，上限 200。

        Returns:
            ApiResult(...)  # data.events 为异常事件列表
        """
        if end_date == date.max:
            return make_response(Result.error("结束日期不能晚于 9999-12-30。"), 422)
        result = await asyncio.to_thread(
            request.app.state.controller.list_abnormal_events, machine_id, session_id, start_date, end_date,
            page=page, page_size=page_size,
        )
        return make_response(result)

    @application.get("/api/v1/abnormal-events/machines", response_model=ApiResult[JsonValue])
    async def abnormal_machines(request: Request):
        """读取异常事件关联的机器编号。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.machine_ids 为机器编号列表
        """
        return make_response(await asyncio.to_thread(request.app.state.controller.list_abnormal_event_machine_ids))

    @application.get("/api/v1/abnormal-events/{event_id}", response_model=ApiResult[JsonValue])
    async def abnormal_detail(event_id: Annotated[int, ApiPath(ge=1, le=2**63 - 1)], request: Request):
        """读取异常事件详情。

        Args:
            event_id: 异常事件主键。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data.event 为原始异常详情
        """
        result = await asyncio.to_thread(request.app.state.controller.get_abnormal_event, event_id)
        if result.success and result.data["event"] is None:
            return make_response(Result.error("未找到异常事件。"), 404)
        return make_response(result)

    @application.get("/api/v1/configuration", response_model=ApiResult[JsonValue])
    async def configuration(request: Request):
        """读取允许展示的配置字段和内容版本。

        Args:
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 包含 settings 和 revision
        """
        result = await asyncio.to_thread(request.app.state.controller.read_configuration)
        return make_configuration_response(result)

    @application.post("/api/v1/configuration/validate", response_model=ApiResult[JsonValue])
    async def validate_configuration(body: ConfigurationInput, request: Request):
        """复用现有配置校验，返回字段定位错误。

        Args:
            body: 待校验的可编辑配置字段。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # 失败时 data.field 指向待修正字段
        """
        result = await asyncio.to_thread(request.app.state.controller.validate_configuration, body.settings)
        return make_response(result)

    @application.put("/api/v1/configuration", response_model=ApiResult[JsonValue])
    async def save_configuration(body: ConfigurationSaveInput, request: Request):
        """在监测完全停止且版本未变化时原子保存配置。

        Args:
            body: 配置草稿和读取时的版本摘要。
            request: 当前已认证请求。

        Returns:
            ApiResult(...)  # data 包含保存后的 settings 和 revision
        """
        operation = partial(request.app.state.controller.save_configuration, body.settings, body.revision)
        result = await request.app.state.runtime_host.run_mutation(operation)
        return make_configuration_response(result, 409)

    # 证据路由按请求取得当前服务，复用统一认证与异常处理。
    application.include_router(create_evidence_router(), prefix="/api/v1")
    return application
