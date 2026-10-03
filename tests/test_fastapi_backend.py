"""验证本机 API 的认证、配置写保护和业务结果，不连接真实设备。"""

from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from src.api.app import create_app
from runtime.hardware_lock import HardwareOwnershipLock


TOKEN = "test-only-ephemeral-token-0000000000000000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def configuration_directory(tmp_path: Path) -> Path:
    """建立所有文件均位于临时目录的最小配置。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        Path(...)  # 包含测试 config.yaml 的目录
    """
    (tmp_path / "config.yaml").write_text(
        "application:\n  database_path: data.sqlite3\n  evidence_directory: evidence\n"
        "camera:\n  mvs_development_directory: sdk\n  camera_gain: 1.0\n"
        "ocr: {}\nfrequency: {}\nmachine: {}\n"
        "io:\n  modbus_serial_port: COM8\n  io_machine_channels: {}\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def api_client(configuration_directory: Path):
    """启动仅初始化临时数据库的 API 测试客户端。

    Args:
        configuration_directory: 测试配置目录。

    Returns:
        TestClient(...)  # 使用真实业务服务的回环客户端
    """
    application = create_app(configuration_directory, token=TOKEN)
    with TestClient(application, base_url="http://127.0.0.1", client=("127.0.0.1", 51000)) as client:
        yield client


def test_health_requires_token_and_trusted_origin(api_client):
    """拒绝无令牌、过期令牌和未授权来源。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 认证成功和失败响应均已验证
    """
    assert api_client.get("/api/v1/health").status_code == 401
    expired = api_client.get("/api/v1/health", headers={"Origin": "http://localhost:1420"})
    assert expired.headers["access-control-allow-origin"] == "http://localhost:1420"
    assert expired.json()["success"] is False
    assert api_client.get("/api/v1/health", headers=AUTH).json()["data"]["ready"] is True
    blocked = api_client.get("/api/v1/health", headers=dict(AUTH, Origin="https://attacker.example"))
    assert blocked.status_code == 403
    assert "access-control-allow-origin" not in blocked.headers


@pytest.mark.parametrize("host", ["attacker.example", "127.0.0.1.attacker.example", "[invalid"])
def test_dns_rebinding_and_malformed_host_are_rejected(api_client, host):
    """拒绝重绑定主机名和损坏的 IPv6 Host。

    Args:
        api_client: 回环 API 客户端。
        host: 恶意或损坏的 Host 请求头。

    Returns:
        None  # 所有 Host 均返回统一 HTTP 403
    """
    response = api_client.get("/api/v1/health", headers=dict(AUTH, Host=host))
    assert response.status_code == 403
    assert response.json()["success"] is False


def test_non_ascii_authorization_cannot_crash_middleware(api_client):
    """将非 ASCII 认证头视为无效令牌而不是服务器异常。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 非 ASCII 输入返回 HTTP 401
    """
    response = api_client.get("/api/v1/health", headers={b"authorization": b"Bearer \xff"})
    assert response.status_code == 401


def test_remote_peer_is_rejected(configuration_directory):
    """即使持有正确令牌也不接受非回环网络连接。

    Args:
        configuration_directory: 测试配置目录。

    Returns:
        None  # 非回环客户端返回 HTTP 403
    """
    application = create_app(configuration_directory, token=TOKEN)
    with TestClient(application, base_url="http://127.0.0.1", client=("192.0.2.4", 1000)) as client:
        assert client.get("/api/v1/health", headers=AUTH).status_code == 403


def test_machine_crud_and_error_envelope(api_client):
    """通过原服务完成机器增改查和软删除。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 业务服务结果与输入校验均保持统一格式
    """
    body = {"machine_name": " 一号机 ", "camera_serial": "CAM1", "frequency_meter_serial": "FREQ1"}
    created = api_client.post("/api/v1/machines", json=body, headers=AUTH)
    assert created.status_code == 200
    machine_id = created.json()["data"]["machine_id"]
    listed = api_client.get("/api/v1/machines", headers=AUTH).json()["data"]["machines"]
    assert listed[0]["machine_name"] == "一号机"
    duplicate = api_client.post("/api/v1/machines", json=body, headers=AUTH)
    assert duplicate.status_code == 409
    assert duplicate.json()["data"]["field"] == "machine_name"
    body["machine_name"] = "新名称"
    assert api_client.put(f"/api/v1/machines/{machine_id}", json=body, headers=AUTH).json()["success"]
    assert api_client.delete(f"/api/v1/machines/{machine_id}", headers=AUTH).json()["success"]
    assert api_client.get("/api/v1/machines", headers=AUTH).json()["data"]["machines"] == []
    invalid = api_client.post("/api/v1/machines", json=dict(body, enabled="true"), headers=AUTH)
    assert invalid.status_code == 422
    assert set(invalid.json()) == {"success", "data", "message"}


@pytest.mark.parametrize("status", ["starting", "running", "stopping"])
def test_machine_and_configuration_writes_blocked_in_active_states(api_client, status):
    """在启动、运行和停止清理阶段从后端拒绝全部配置写入。

    Args:
        api_client: 回环 API 客户端。
        status: 宿主生命周期状态。

    Returns:
        None  # 状态锁不依赖前端是否禁用按钮
    """
    snapshot = api_client.get("/api/v1/configuration", headers=AUTH).json()["data"]
    host = api_client.app.state.runtime_host
    host.status = status
    body = {"machine_name": "机", "camera_serial": "CAM1", "frequency_meter_serial": "FREQ1"}
    assert api_client.post("/api/v1/machines", json=body, headers=AUTH).status_code == 409
    assert api_client.put("/api/v1/machines/1", json=body, headers=AUTH).status_code == 409
    assert api_client.delete("/api/v1/machines/1", headers=AUTH).status_code == 409
    saved = api_client.put("/api/v1/configuration", json={
        "settings": {"camera_gain": 2.0}, "revision": snapshot["revision"],
    }, headers=AUTH)
    assert saved.status_code == 409
    host.status = "stopped"


def test_configuration_revision_rejects_stale_client(api_client):
    """配置读写沿用业务校验并阻止两个窗口覆盖彼此修改。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 有效修改成功，旧版本和只读字段写入均被拒绝
    """
    baseline = api_client.get("/api/v1/configuration", headers=AUTH).json()["data"]
    assert isinstance(baseline["settings"]["database_path"], str)
    changed = api_client.put("/api/v1/configuration", json={
        "settings": {"camera_gain": 2.5}, "revision": baseline["revision"],
    }, headers=AUTH)
    assert changed.json()["success"]
    assert changed.json()["data"]["revision"] != baseline["revision"]
    api_client.get("/api/v1/configuration", headers=AUTH)
    stale = api_client.put("/api/v1/configuration", json={
        "settings": {"camera_gain": 3.5}, "revision": baseline["revision"],
    }, headers=AUTH)
    assert stale.status_code == 409
    assert stale.json()["data"]["field"] == "revision"
    protected = api_client.post("/api/v1/configuration/validate", json={
        "settings": {"database_path": "/tmp/other.sqlite3"},
    }, headers=AUTH)
    assert protected.status_code == 400


def test_cross_process_owner_blocks_configuration_changes(api_client):
    """即使本 API 未监测也不能修改其他实例正在使用的机器配置。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 操作系统设备锁在 HTTP 写入层生效
    """
    with HardwareOwnershipLock():
        response = api_client.post("/api/v1/machines", json={
            "machine_name": "机", "camera_serial": "CAM1", "frequency_meter_serial": "FREQ1",
        }, headers=AUTH)
    assert response.status_code == 409
    assert "其他监测实例" in response.json()["message"]


def test_record_filters_and_missing_details(api_client):
    """验证记录筛选参数、分页上限和缺失详情结果。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 空数据库查询不触发 OCR、相机或 Modbus
    """
    response = api_client.get("/api/v1/records?text_length=20&text_query=abc", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["data"]["records"] == []
    assert api_client.get("/api/v1/records?page_size=201", headers=AUTH).status_code == 422
    assert api_client.get("/api/v1/records?text_length=7", headers=AUTH).status_code == 400
    assert api_client.get("/api/v1/records/missing", headers=AUTH).status_code == 404
    assert api_client.get("/api/v1/abnormal-events/123", headers=AUTH).status_code == 404
    assert api_client.get("/api/v1/records/summary", headers=AUTH).json()["data"]["recognition_count"] == 0


def test_authenticated_shutdown_is_explicit(api_client):
    """只有认证退出命令才设置进程关闭通知。

    Args:
        api_client: 回环 API 客户端。

    Returns:
        None  # 普通查询和未认证退出均不改变进程状态
    """
    assert api_client.post("/api/v1/shutdown").status_code == 401
    assert not api_client.app.state.shutdown_requested.is_set()
    assert api_client.post("/api/v1/shutdown", headers=AUTH).json()["data"]["accepted"]
    assert api_client.app.state.shutdown_requested.is_set()


@pytest.mark.parametrize("suffix", [
    "records?page=1000000000000000000000000",
    "records?end_date=9999-12-31",
    "abnormal-events/999999999999999999999",
])
def test_extreme_query_inputs_fail_validation_without_database_overflow(api_client, suffix):
    """在访问 SQLite 或日期转换前拒绝超界外部输入。

    Args:
        api_client: 回环 API 客户端。
        suffix: 极值查询或主键请求。

    Returns:
        None  # 极值输入返回统一 HTTP 422
    """
    response = api_client.get("/api/v1/" + suffix, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["success"] is False


def test_unexpected_error_keeps_trusted_origin_and_private_cache_headers(configuration_directory):
    """最外层服务器错误响应也允许可信前端读取统一错误信息。

    Args:
        configuration_directory: 测试配置目录。

    Returns:
        None  # HTTP 500 保留准确来源和禁止缓存头
    """
    application = create_app(configuration_directory, token=TOKEN)
    with TestClient(
        application, base_url="http://127.0.0.1", client=("127.0.0.1", 1000), raise_server_exceptions=False,
    ) as client:
        application.state.controller.list_measurement_records = Mock(side_effect=RuntimeError("隔离未知故障"))
        response = client.get("/api/v1/records", headers=dict(AUTH, Origin="http://localhost:1420"))
    assert response.status_code == 500
    assert response.json()["success"] is False
    assert response.headers["access-control-allow-origin"] == "http://localhost:1420"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("yaml_value", "visible_value"),
    [(".nan", "NaN"), (".inf", "Infinity"), ("-.inf", "-Infinity")],
)
def test_invalid_configuration_number_remains_visible_and_repairable(
    configuration_directory,
    yaml_value,
    visible_value,
):
    """非有限 YAML 数字不会使配置页崩溃或被静默转换为设备默认值。

    Args:
        configuration_directory: 测试配置目录。
        yaml_value: YAML 表示的非法数字。
        visible_value: API 使用的可见原始文本。

    Returns:
        None  # 非法值可见、可定位，并能通过正常版本校验修正
    """
    path = configuration_directory / "config.yaml"
    path.write_text(path.read_text().replace("camera_gain: 1.0", f"camera_gain: {yaml_value}"))
    application = create_app(configuration_directory, token=TOKEN)
    with TestClient(application, base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as client:
        response = client.get("/api/v1/configuration", headers=AUTH)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["settings"]["camera_gain"] == visible_value
        assert data["invalid_fields"][0]["field"] == "camera_gain"
        saved = client.put("/api/v1/configuration", headers=AUTH, json={
            "settings": {"camera_gain": 2.5}, "revision": data["revision"],
        })
        assert saved.json()["success"]
        assert saved.json()["data"]["invalid_fields"] == []
