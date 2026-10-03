"""定义 HTTP 请求和可序列化的统一结果模型。"""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool


ResponseData = TypeVar("ResponseData")


class ApiResult(BaseModel, Generic[ResponseData]):
    """固定成功标志、JSON 数据和用户可读提示。"""

    success: bool
    data: ResponseData | None = None
    message: str = ""


class MachineInput(BaseModel):
    """限制机器配置写入字段和基础输入长度。"""

    model_config = ConfigDict(extra="forbid")
    machine_name: str = Field(min_length=1, max_length=200)
    camera_serial: str = Field(min_length=1, max_length=200)
    frequency_meter_serial: str = Field(min_length=1, max_length=200)
    enabled: StrictBool = True
    remark: str | None = Field(default=None, max_length=2000)


class ReviewInput(BaseModel):
    """保存本次人工复核的可选修改文字。"""

    model_config = ConfigDict(extra="forbid")
    edited_text: str | None = Field(default=None, max_length=20000)


class ConfigurationInput(BaseModel):
    """传递业务服务允许修改的配置字段。"""

    model_config = ConfigDict(extra="forbid")
    settings: dict[str, JsonValue]


class ConfigurationSaveInput(ConfigurationInput):
    """要求配置保存携带读取版本，避免多个窗口互相覆盖。"""

    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeMachine(BaseModel):
    """定义实时机器卡片和现场占用状态。"""

    id: str
    machine_name: str
    camera_serial: str
    frequency_meter_serial: str
    enabled: bool
    remark: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    camera_state: str
    camera_error: str
    status: Literal["online", "offline", "fault"]
    warning: str
    active_session_id: str | None
    waiting_cycle_reset: bool
    inflight_count: int = Field(ge=0)


class RuntimeSession(BaseModel):
    """定义按 Session ID 隔离的周期阶段、结果和频率。"""

    machine_id: str
    session_id: str
    state: Literal["RUNNING", "SAVING_RESULT", "COMMITTED", "FAILED"]
    cycle_closed: bool
    stages: dict[
        Literal["session_start", "image_capture", "frequency_collection", "character_recognition", "evidence_storage"],
        Literal["running", "success", "failed"],
    ]
    recognized_lines: list[str]
    final_frequency_hz: float | None
    start_time: str | None
    finish_time: str | None
    errors: list[str]


class RuntimeSnapshot(BaseModel):
    """定义 HTTP 和每次 SSE 事件共用的完整恢复快照。"""

    status: Literal["stopped", "starting", "running", "stopping", "failed"]
    running: bool
    failure: str
    started_at: str | None
    sequence: int = Field(ge=0)
    machines: list[RuntimeMachine]
    sessions: list[RuntimeSession]
