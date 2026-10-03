"""验证证据授权、分页、延迟读取及安全文件边界。"""

import hashlib
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from src.api.evidence_routes import create_evidence_router
from src.service.evidence_service import EvidenceAccessError, EvidenceService


@pytest.fixture
def evidence_fixture(tmp_path: Path):
    """建立只引用临时证据的接口客户端。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        (
            client,  # 测试 HTTP 客户端
            service,  # 仅返回临时记录的服务替身
            directory,  # 测试图片目录
        )
    """
    directory = tmp_path / "evidence"
    directory.mkdir()
    service = Mock()
    service.get_record.side_effect = lambda session_id: {
        "record": {"evidence_directory": str(directory)} if session_id == "session-one" else None,
    }
    application = FastAPI()
    application.include_router(create_evidence_router(service), prefix="/api/v1")
    with TestClient(application) as client:
        yield client, service, directory


def save_test_jpeg(path: Path, size: tuple[int, int] = (100, 50)) -> None:
    """保存可解码的隔离测试图片。

    Args:
        path: 临时文件路径。
        size: 图片宽高。

    Returns:
        None  # 测试图片已写入临时目录
    """
    Image.new("RGB", size, (100, 160, 120)).save(path, "JPEG")


def test_evidence_listing_is_paginated_and_uses_existing_numeric_order(evidence_fixture) -> None:
    """验证现存图片数、分页与旧版一致的帧顺序。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 先显示第二帧再显示第十帧，非图片不进入列表
    """
    client, _, directory = evidence_fixture
    capture_id = "11111111111141118111111111111111"
    for frame_number in (10, 2, 1):
        save_test_jpeg(directory / f"{capture_id}-{frame_number}.jpg")
    (directory / "ignored.txt").write_text("不作为证据图片")
    (directory / "nested").mkdir()
    response = client.get("/api/v1/records/session-one/evidence?page=2&page_size=1")
    payload = response.json()["data"]
    assert response.status_code == 200
    assert (payload["count"], payload["page"], payload["total_pages"]) == (3, 2, 3)
    assert payload["images"][0]["filename"] == f"{capture_id}-2.jpg"
    assert "evidence_directory" not in payload


def test_listing_does_not_decode_files_and_preserves_nonstandard_names(evidence_fixture) -> None:
    """验证枚举不触发解码且文件名不会进入 URL 路径。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 损坏文件保留身份，特殊字符只在可展示文件名中出现
    """
    client, _, directory = evidence_fixture
    filename = "第 1 张 #问号?.JPEG"
    (directory / filename).write_bytes(b"broken jpeg")
    payload = client.get("/api/v1/records/session-one/evidence").json()["data"]
    image = payload["images"][0]
    assert payload["state"] == "available"
    assert image["filename"] == filename
    assert image["image_id"] == hashlib.sha256(filename.encode()).hexdigest()
    assert image["url"].endswith(image["image_id"])
    assert "?" not in image["url"]
    assert "token" not in image["thumbnail_url"]


def test_images_are_scoped_to_real_records_and_never_accept_filenames(evidence_fixture) -> None:
    """验证任意图片路径和不存在的测量编号不能读取文件。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 错误记录及直接文件名请求均返回 404
    """
    client, _, directory = evidence_fixture
    save_test_jpeg(directory / "frame.jpg")
    image_id = hashlib.sha256(b"frame.jpg").hexdigest()
    assert client.get(f"/api/v1/records/unknown/images/{image_id}").status_code == 404
    assert client.get("/api/v1/records/session-one/images/frame.jpg").status_code == 404
    assert client.get("/api/v1/records/unknown/evidence").status_code == 404
    assert client.get("/api/v1/records/unknown/evidence-directory").status_code == 404


def test_image_stream_and_thumbnail_preserve_bytes_and_limit_dimensions(evidence_fixture) -> None:
    """验证原图不改写且缩略图按比例受限。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 原图内容完整，缩略图最大边长为 640
    """
    client, _, directory = evidence_fixture
    image_path = directory / "wide.jpg"
    save_test_jpeg(image_path, (2000, 1000))
    image = client.get("/api/v1/records/session-one/evidence").json()["data"]["images"][0]
    original = client.get(image["url"])
    thumbnail = client.get(image["thumbnail_url"])
    assert original.content == image_path.read_bytes()
    assert original.headers["cache-control"] == "private, no-store"
    assert original.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(thumbnail.content)) as decoded:
        assert decoded.size == (640, 320)


def test_corrupt_image_returns_actionable_error_without_removing_file(evidence_fixture) -> None:
    """验证损坏图片有可读错误且原文件不删除。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 缩略图失败为 422，原证据仍保留
    """
    client, _, directory = evidence_fixture
    path = directory / "broken.jpg"
    path.write_bytes(b"broken")
    image = client.get("/api/v1/records/session-one/evidence").json()["data"]["images"][0]
    response = client.get(image["thumbnail_url"])
    assert response.status_code == 422
    assert response.json()["success"] is False
    assert path.read_bytes() == b"broken"


def test_missing_directory_has_unknown_count_and_empty_directory_has_zero(evidence_fixture) -> None:
    """验证不可读取与确实无图片的状态区别。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 空目录数量为零，丢失目录数量未知
    """
    client, _, directory = evidence_fixture
    empty = client.get("/api/v1/records/session-one/evidence").json()["data"]
    assert (empty["state"], empty["count"]) == ("no_jpg", 0)
    directory.rmdir()
    missing = client.get("/api/v1/records/session-one/evidence").json()["data"]
    assert (missing["state"], missing["count"]) == ("missing_directory", None)
    assert client.get("/api/v1/records/session-one/evidence-directory").status_code == 404


def test_symlink_image_and_nested_files_are_never_exposed(evidence_fixture, tmp_path: Path) -> None:
    """验证链接和嵌套文件不会越过当前证据目录。

    Args:
        evidence_fixture: 隔离接口和目录。
        tmp_path: 临时目录。

    Returns:
        None  # 指向目录外的链接不会出现在图片列表
    """
    client, _, directory = evidence_fixture
    outside = tmp_path / "outside.jpg"
    save_test_jpeg(outside)
    try:
        (directory / "escape.jpg").symlink_to(outside)
    except OSError:
        pytest.skip("当前平台未允许创建测试链接")
    (directory / "nested").mkdir()
    save_test_jpeg(directory / "nested" / "nested.jpg")
    payload = client.get("/api/v1/records/session-one/evidence").json()["data"]
    assert payload["images"] == []
    assert client.get(
        "/api/v1/records/session-one/images/" + hashlib.sha256(b"escape.jpg").hexdigest()
    ).status_code == 404


def test_symlink_directory_cannot_be_opened_by_native_shell(evidence_fixture, tmp_path: Path) -> None:
    """验证原目录被链接替换后不能请求系统打开。

    Args:
        evidence_fixture: 隔离接口和目录。
        tmp_path: 临时目录。

    Returns:
        None  # 文件夹解析明确拒绝链接，列表显示不可访问
    """
    client, _, directory = evidence_fixture
    outside = tmp_path / "outside"
    outside.mkdir()
    directory.rmdir()
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台未允许创建测试链接")
    assert client.get("/api/v1/records/session-one/evidence-directory").status_code == 403
    assert client.get("/api/v1/records/session-one/evidence").json()["data"]["state"] == "access_denied"


def test_file_disappearing_after_listing_returns_not_found(evidence_fixture) -> None:
    """验证列表发出后文件删除能正常报错。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 已失效图片身份返回 404
    """
    client, _, directory = evidence_fixture
    path = directory / "frame.jpg"
    save_test_jpeg(path)
    image = client.get("/api/v1/records/session-one/evidence").json()["data"]["images"][0]
    path.unlink()
    assert client.get(image["url"]).status_code == 404


def test_directory_resolver_uses_saved_location_instead_of_current_config(evidence_fixture) -> None:
    """验证文件夹操作只返回数据库保存的真实目录。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 解析结果与当前配置目录无关
    """
    client, _, directory = evidence_fixture
    response = client.get("/api/v1/records/session-one/evidence-directory")
    assert response.json()["data"]["directory"] == str(directory.resolve())


def test_relative_database_path_is_rejected(evidence_fixture) -> None:
    """验证旧记录中的相对路径不会随着启动目录改变授权范围。

    Args:
        evidence_fixture: 隔离接口和目录。

    Returns:
        None  # 相对路径报告原始记录问题
    """
    _, records, _ = evidence_fixture
    records.get_record.side_effect = None
    records.get_record.return_value = {"record": {"evidence_directory": "../../external"}}
    with pytest.raises(EvidenceAccessError, match="不是绝对路径"):
        EvidenceService(records).resolve_directory("session-one")


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101", "page=word"])
def test_evidence_pagination_validates_external_boundaries(evidence_fixture, query: str) -> None:
    """验证分页外部参数具有明确上限。

    Args:
        evidence_fixture: 隔离接口和目录。
        query: 非法分页查询字符串。

    Returns:
        None  # 非法参数返回 422
    """
    client, _, _ = evidence_fixture
    assert client.get(f"/api/v1/records/session-one/evidence?{query}").status_code == 422
