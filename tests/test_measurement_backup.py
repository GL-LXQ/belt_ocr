"""只用临时业务库和图片验证显式备份与校验。"""

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from repo.machine_repo import MachineRepo
from repo.measurement_record_repo import MeasurementRecordRepo
from scripts import backup_measurements
from scripts.backup_measurements import create_backup, describe_file, main, restore_backup, verify_backup


@pytest.fixture
def measurement_source(tmp_path: Path) -> tuple[Path, Path]:
    """准备含机器、人工复核、跨月时间和六张证据的临时库。

    Args:
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            (
                Path("/tmp/source/measurements.sqlite3"),  # 临时业务库
                Path("/tmp/source/evidence"),  # 临时证据根目录
            )
    """
    # 建立现有业务表，证据目录归属开始日期。
    source_root = tmp_path / "source"
    source_root.mkdir()
    database_path = source_root / "measurements.sqlite3"
    evidence_root = source_root / "evidence"
    session_directory = evidence_root / "20260930" / "1" / "session-1"
    session_directory.mkdir(parents=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)
        connection.execute(
            "INSERT INTO machine (id, machine_name, camera_serial, frequency_meter_serial) "
            "VALUES (1, '皮带机一', 'camera-1', 'frequency-1')"
        )
        connection.execute(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, recognized_lines, final_frequency_hz, "
            "evidence_directory, reviewed_at, reviewed_lines) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1", "1", "2026-09-30T23:59:59+00:00", "2026-10-01T00:00:02+00:00",
                '["2926215C", "003"]', 50.0, str(session_directory),
                "2026-10-01T00:05:02+00:00", '["2926216C", "003"]',
            ),
        )

    # 使用小型二进制内容检查字节复制，不进行图片或视觉测试。
    for image_number in range(6):
        (session_directory / f"{image_number}.jpg").write_bytes(b"\xff\xd8" + bytes([image_number]) + b"\xff\xd9")
    (session_directory / "ignored.txt").write_text("不会备份任意附带文件", encoding="utf-8")
    return database_path, evidence_root


def test_backup_preserves_source_and_restores_search_elsewhere(measurement_source, tmp_path: Path) -> None:
    """验证完整快照、全部图片及在新位置恢复后的文字查询。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 原数据未变，更换位置后仍能查询并找到六张证据
    """
    database_path, evidence_root = measurement_source
    source_bytes = {path: path.read_bytes() for path in database_path.parent.rglob("*") if path.is_file()}
    destination = tmp_path / "backup"
    assert create_backup(database_path, evidence_root, destination) == destination
    assert verify_backup(destination) == {
        "measurement_count": 1,
        "machine_count": 1,
        "image_count": 6,
    }
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes
    assert not destination.with_name("backup.partial").exists()
    assert not list(destination.rglob("ignored.txt"))

    # 使用恢复命令在新目录复制记录并重映射证据路径。
    restored = tmp_path / "restored"
    backup_bytes = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    assert restore_backup(destination, restored) == restored
    assert verify_backup(restored)["image_count"] == 6
    manifest = json.loads((restored / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["created_at_utc"].endswith("+00:00")
    assert manifest["records"][0]["source_directory"].endswith("20260930/1/session-1")
    restored_database = restored / "measurements.sqlite3"
    with closing(sqlite3.connect(restored_database)) as connection:
        reviewed_lines = connection.execute("SELECT reviewed_lines FROM measurement_records").fetchone()[0]
        assert reviewed_lines == '["2926216C", "003"]'

    # 沿现有 Repo 查询人工有效文字，并从详情找到恢复后的全部图片。
    repo = MeasurementRecordRepo(restored_database)
    records = repo.list_records(text_query="2926216C", text_match_mode="exact", text_length=8)
    assert len(records) == 1
    assert records[0]["machine_name"] == "皮带机一"
    assert records[0]["final_frequency_hz"] == 50.0
    assert not repo.list_records(text_query="2926215C", text_match_mode="exact")
    record = repo.get_record("session-1")
    assert Path(record["evidence_directory"]).is_relative_to(restored)
    assert len(list(Path(record["evidence_directory"]).glob("*.jpg"))) == 6
    assert verify_backup(destination)["image_count"] == 6
    assert {path: path.read_bytes() for path in backup_bytes} == backup_bytes
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes


def test_backup_snapshot_excludes_uncommitted_transaction(measurement_source, tmp_path: Path) -> None:
    """验证 WAL 中未提交的复核修改不会进入快照。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 快照保存已提交版本，源事务仍可自行回滚
    """
    database_path, evidence_root = measurement_source
    with closing(sqlite3.connect(database_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE measurement_records SET reviewed_lines='[\"UNCOMMITTED\"]'")
        destination = create_backup(database_path, evidence_root, tmp_path / "concurrent")
        with closing(sqlite3.connect(destination / "measurements.sqlite3")) as snapshot:
            assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            reviewed_lines = snapshot.execute("SELECT reviewed_lines FROM measurement_records").fetchone()[0]
            assert reviewed_lines == '["2926216C", "003"]'
        writer.rollback()


def test_backup_lock_timeout_leaves_only_partial(measurement_source, tmp_path: Path) -> None:
    """验证源库独占锁不会让备份无限等待或发布不完整结果。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 在等待上限附近失败，仅保留暂存目录
    """
    database_path, evidence_root = measurement_source
    destination = tmp_path / "locked"
    with closing(sqlite3.connect(database_path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="快照超时"):
            create_backup(database_path, evidence_root, destination, timeout_seconds=0.12)
        assert time.monotonic() - started < 2
        writer.rollback()
    assert not destination.exists()
    assert destination.with_name("locked.partial").exists()
    assert verify_backup(create_backup(database_path, evidence_root, tmp_path / "retry"))["measurement_count"] == 1


@pytest.mark.parametrize(
    "malformation", ["missing_directory", "empty_directory", "relative", "outside", "root", "link"]
)
def test_backup_refuses_missing_or_unsafe_evidence(measurement_source, tmp_path: Path, malformation: str) -> None:
    """验证缺失和越界证据不会被标记为成功备份。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        malformation: 要模拟的证据路径异常。

    Returns:
        返回示例：
            None  # 备份失败，源库保留，正式目标不存在
    """
    database_path, evidence_root = measurement_source
    session_directory = evidence_root / "20260930" / "1" / "session-1"
    invalid_path = session_directory
    if malformation == "missing_directory":
        invalid_path = evidence_root / "missing"
    elif malformation == "empty_directory":
        invalid_path = evidence_root / "empty"
        invalid_path.mkdir()
    elif malformation == "relative":
        invalid_path = Path("evidence/relative")
    elif malformation == "outside":
        invalid_path = tmp_path / "outside"
        invalid_path.mkdir()
        (invalid_path / "private.jpg").write_bytes(b"not authorized evidence")
    elif malformation == "root":
        invalid_path = evidence_root
    else:
        secret = tmp_path / "private.jpg"
        secret.write_bytes(b"outside")
        try:
            (session_directory / "link.jpg").symlink_to(secret)
        except OSError:
            pytest.skip("当前平台不允许创建符号链接")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("UPDATE measurement_records SET evidence_directory=?", (str(invalid_path),))
    source_bytes = database_path.read_bytes()
    destination = tmp_path / "invalid"
    with pytest.raises((ValueError, OSError)):
        create_backup(database_path, evidence_root, destination)
    assert not destination.exists()
    assert destination.with_name("invalid.partial").exists()
    assert database_path.read_bytes() == source_bytes


@pytest.mark.parametrize("existing", ["destination", "partial"])
def test_backup_never_overwrites_existing_target(measurement_source, tmp_path: Path, existing: str) -> None:
    """验证现有目标和失败暂存内容都不会被覆盖。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        existing: 模拟正式备份或暂存目录已存在。

    Returns:
        返回示例：
            None  # 已有内容原样保留
    """
    database_path, evidence_root = measurement_source
    destination = tmp_path / "existing"
    preserved = destination if existing == "destination" else destination.with_name("existing.partial")
    preserved.mkdir()
    sentinel = preserved / "keep.txt"
    sentinel.write_text("保留", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_backup(database_path, evidence_root, destination)
    assert sentinel.read_text(encoding="utf-8") == "保留"
    assert list(preserved.iterdir()) == [sentinel]


@pytest.mark.parametrize("damage", ["changed", "missing", "outside_manifest"])
def test_verify_detects_damage(measurement_source, tmp_path: Path, damage: str) -> None:
    """验证文件损坏、缺失和清单越界路径都会使校验失败。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        damage: 对备份副本施加的损坏类型。

    Returns:
        返回示例：
            None  # 错误备份未通过校验
    """
    database_path, evidence_root = measurement_source
    destination = create_backup(database_path, evidence_root, tmp_path / "damaged")
    image_path = next(destination.rglob("*.jpg"))
    if damage == "changed":
        image_path.write_bytes(b"damaged")
    elif damage == "missing":
        image_path.unlink()
    else:
        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../source/measurements.sqlite3"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        verify_backup(destination)


def test_cli_uses_existing_config_path_rules_without_hardware(measurement_source, tmp_path: Path, capsys) -> None:
    """验证命令行按配置目录解析路径且无需有效相机配置。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        capsys: pytest 标准输出捕获器。

    Returns:
        返回示例：
            None  # 创建和独立校验成功，已存在目标返回失败
    """
    configuration = tmp_path / "config"
    configuration.mkdir()
    (configuration / "config.yaml").write_text(
        "application:\n  database_path: ../source/measurements.sqlite3\n  evidence_directory: ../source/evidence\n"
        "camera:\n  mvs_development_directory: unused\n  camera_strobe_enabled: true\n",
        encoding="utf-8",
    )
    destination = tmp_path / "cli-backup"
    assert main(["--config-dir", str(configuration), "--destination", str(destination)]) == 0
    assert main(["--verify", str(destination)]) == 0
    assert "备份校验通过" in capsys.readouterr().out
    assert main(["--config-dir", str(configuration), "--destination", str(destination)]) == 1
    assert "备份目标已存在" in capsys.readouterr().err


def test_empty_measurement_table_still_preserves_machine(measurement_source, tmp_path: Path) -> None:
    """验证尚无测量记录时仍能备份机器配置。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 空测量库校验成功且保留机器表
    """
    database_path, evidence_root = measurement_source
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("DELETE FROM measurement_records")
    destination = create_backup(database_path, evidence_root, tmp_path / "empty")
    assert verify_backup(destination) == {
        "measurement_count": 0,
        "machine_count": 1,
        "image_count": 0,
    }
    assert verify_backup(restore_backup(destination, tmp_path / "restored-empty")) == verify_backup(destination)


def test_invalid_database_is_not_published(measurement_source, tmp_path: Path) -> None:
    """验证损坏的源库不会产生成功备份。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 无效文件保持原样且目标未发布
    """
    database_path, evidence_root = measurement_source
    database_path.write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(database_path, evidence_root, tmp_path / "corrupt")
    assert database_path.read_bytes() == b"not a sqlite database"
    assert not (tmp_path / "corrupt").exists()


def test_cli_explicit_paths_need_no_configuration(measurement_source, tmp_path: Path) -> None:
    """验证显式源路径不会要求配置文件或硬件依赖。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 显式源路径完成备份，源库不存在时不创建空库
    """
    database_path, evidence_root = measurement_source
    assert main([
        "--database", str(database_path), "--evidence-root", str(evidence_root),
        "--config-dir", str(tmp_path / "absent-config"), "--destination", str(tmp_path / "explicit"),
    ]) == 0
    absent_database = tmp_path / "absent.sqlite3"
    assert main([
        "--database", str(absent_database), "--evidence-root", str(evidence_root),
        "--destination", str(tmp_path / "failed"),
    ]) == 1
    assert not absent_database.exists()


def test_restore_preserves_metadata_after_original_paths_disappear(measurement_source, tmp_path: Path) -> None:
    """验证原位置不可用时恢复全部字段，仅改写证据路径。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 机器、复核、频率明细、额外表和全部图片完整保留
    """
    # 增加停用机器、待复核记录、频率明细与额外业务元数据。
    database_path, evidence_root = measurement_source
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("UPDATE machine SET enabled=0, is_deleted=1, remark='原始机器备注'")
        connection.execute(
            "UPDATE measurement_records SET needs_review=1, review_reason='跨月人工复核', "
            "measurement_frequencies='[{\"frequency_hz\":49.8,\"source\":\"meter-1\"}]'"
        )
        connection.execute(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, recognized_lines, final_frequency_hz, "
            "evidence_directory, needs_review, review_reason) "
            "SELECT 'session-2', machine_id, start_time, finish_time, '[]', NULL, evidence_directory, 1, '待确认' "
            "FROM measurement_records WHERE session_id='session-1'"
        )
        connection.execute("CREATE TABLE custom_metadata (name TEXT, value TEXT)")
        connection.execute("INSERT INTO custom_metadata VALUES ('operator', '操作员甲')")

        # 保存完整表内容与结构供恢复后逐字段比较。
        schema = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        original_rows = {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name, in tables}
        columns = connection.execute("PRAGMA table_info(measurement_records)").fetchall()
        evidence_column = next(index for index, column in enumerate(columns) if column[1] == "evidence_directory")

    # 移走原数据和备份，恢复不依赖它们曾经所在的位置或生产配置。
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    source_bytes = {path.relative_to(database_path.parent): path.read_bytes()
                    for path in database_path.parent.rglob("*") if path.is_file()}
    backup_bytes = {path.relative_to(backup): path.read_bytes() for path in backup.rglob("*") if path.is_file()}
    offline_source = database_path.parent.rename(tmp_path / "offline-source")
    relocated_backup = backup.rename(tmp_path / "relocated-backup")
    restored = tmp_path / "restored"
    assert main(["--restore", str(relocated_backup), str(restored), "--config-dir", str(tmp_path / "absent")]) == 0

    # 比较全库结构和数据，只允许证据目录变化。
    with closing(sqlite3.connect(restored / "measurements.sqlite3")) as connection:
        assert connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        for name, rows in original_rows.items():
            restored_rows = connection.execute(f'SELECT * FROM "{name}"').fetchall()
            if name == "measurement_records":
                expected_directory = str(restored / "evidence" / "20260930" / "1" / "session-1")
                rows = [tuple(expected_directory if index == evidence_column else value
                              for index, value in enumerate(row)) for row in rows]
            assert restored_rows == rows
    assert verify_backup(restored) == {
        "measurement_count": 2,
        "machine_count": 1,
        "image_count": 6,
    }

    # 沿现有查询入口查找人工结果，并确认离线原件的字节未变。
    page = MeasurementRecordRepo(restored / "measurements.sqlite3").get_record_page(text_query="2926216C")
    assert len(page["records"]) == 1
    assert page["records"][0]["session_id"] == "session-1"
    assert {path.relative_to(offline_source): path.read_bytes()
            for path in offline_source.rglob("*") if path.is_file()} == source_bytes
    assert {path.relative_to(relocated_backup): path.read_bytes()
            for path in relocated_backup.rglob("*") if path.is_file()} == backup_bytes


@pytest.mark.parametrize("existing", ["empty", "populated", "file", "dangling_link"])
def test_restore_never_overwrites_existing_target(measurement_source, tmp_path: Path, existing: str) -> None:
    """验证已有目录、文件和悬空链接不会被恢复覆盖。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        existing: 恢复目标已存在的形式。

    Returns:
        返回示例：
            None  # 目标原样保留，恢复拒绝执行
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    destination = tmp_path / "existing"
    if existing == "file":
        destination.write_bytes(b"keep")
    elif existing == "dangling_link":
        destination.symlink_to(tmp_path / "absent", target_is_directory=True)
    else:
        destination.mkdir()
        if existing == "populated":
            (destination / "keep.txt").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        restore_backup(backup, destination)
    if existing == "file":
        assert destination.read_bytes() == b"keep"
    elif existing == "dangling_link":
        assert destination.is_symlink() and not destination.exists()
    else:
        assert list(destination.iterdir()) == ([] if existing == "empty" else [destination / "keep.txt"])


@pytest.mark.parametrize("target_tree", ["backup", "evidence", "backup_alias", "evidence_alias"])
def test_restore_rejects_writes_inside_source_trees(measurement_source, tmp_path: Path, target_tree: str) -> None:
    """验证直接路径和目录链接都不能把恢复内容写入来源目录树。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        target_tree: 目标所在的原数据目录或其别名。

    Returns:
        返回示例：
            None  # 来源目录不新增恢复内容
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    root = backup if target_tree.startswith("backup") else evidence_root
    if target_tree.endswith("alias"):
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    destination = root / "restored"
    with pytest.raises(ValueError, match="目录内部"):
        restore_backup(backup, destination)
    assert not destination.exists()


@pytest.mark.parametrize("field,value", [
    ("manifest", []),
    ("format_version", True),
    ("files", {}),
    ("file", None),
    ("path", 7),
    ("path", "../source/measurements.sqlite3"),
    ("path", "evidence//image.jpg"),
    ("size", True),
    ("sha256", "invalid"),
    ("records", None),
    ("session_id", "wrong-session"),
    ("source_directory", []),
    ("source_directory", "/wrong/directory"),
    ("backup_directory", "measurements.sqlite3"),
    ("backup_directory", "../source/evidence"),
    ("duplicate_record", True),
])
def test_restore_rejects_malformed_manifest(measurement_source, tmp_path: Path, field: str, value) -> None:
    """验证清单类型、路径和记录映射错误在创建恢复目标前被拒绝。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        field: 要破坏的清单字段。
        value: 写入该字段的错误内容。

    Returns:
        返回示例：
            None  # 错误备份未产生恢复目录且命令正常返回失败
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field == "manifest":
        manifest = value
    elif field == "file":
        manifest["files"][0] = value
    elif field in {"path", "size", "sha256"}:
        manifest["files"][0][field] = value
    elif field in {"session_id", "source_directory", "backup_directory"}:
        manifest["records"][0][field] = value
    elif field == "duplicate_record":
        manifest["records"].append(manifest["records"][0].copy())
    else:
        manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"
    assert main(["--restore", str(backup), str(destination)]) == 1
    assert not destination.exists()


@pytest.mark.parametrize("special", ["manifest_fifo", "image_fifo", "image_link", "directory_link"])
def test_verify_rejects_special_files_without_reading_them(measurement_source, tmp_path: Path, special: str) -> None:
    """验证特殊文件和目录链接会直接失败，不阻塞或读取链接目标。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        special: 要模拟的特殊文件类型。

    Returns:
        返回示例：
            None  # 特殊文件不通过校验，恢复目标不存在
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    image_path = next(backup.rglob("*.jpg"))
    original = backup / "manifest.json" if special == "manifest_fifo" else image_path
    if special == "directory_link":
        original = image_path.parent
    moved = original.rename(tmp_path / "moved")
    if special.endswith("fifo"):
        if not hasattr(os, "mkfifo"):
            pytest.skip("当前平台不支持 FIFO")
        os.mkfifo(original)
    else:
        original.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("copy_failure", ["interrupted", "corrupted"])
def test_restore_keeps_failed_copy_without_completion_manifest(
    measurement_source,
    tmp_path: Path,
    monkeypatch,
    capsys,
    copy_failure: str,
) -> None:
    """验证复制失败或损坏时保留现场且没有正式完成清单。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        monkeypatch: pytest 属性替换器。
        capsys: pytest 标准输出捕获器。
        copy_failure: 要模拟的复制异常。

    Returns:
        返回示例：
            None  # 命令报告目标路径，原数据不变且未完成目录不能复用
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    unchanged = {path: path.read_bytes() for root in (database_path.parent, backup)
                 for path in root.rglob("*") if path.is_file()}
    copy_file = backup_measurements.shutil.copyfileobj

    # 在临时证据的复制位置模拟明确的写入异常。
    def fail_image_copy(source_file, target_file) -> None:
        """为测试注入复制中断或字节损坏。

        Args:
            source_file: 原文件读取句柄。
            target_file: 恢复副本写入句柄。

        Returns:
            返回示例：
                None  # 完成模拟复制，或抛出 OSError
        """
        copy_file(source_file, target_file)
        if str(source_file.name).endswith(".jpg"):
            if copy_failure == "interrupted":
                raise OSError("模拟磁盘写入失败")
            target_file.write(b"damaged")

    monkeypatch.setattr(backup_measurements.shutil, "copyfileobj", fail_image_copy)
    destination = tmp_path / "restored"
    assert main(["--restore", str(backup), str(destination)]) == 1
    assert str(destination) in capsys.readouterr().err
    assert destination.is_dir() and not (destination / "manifest.json").exists()
    assert {path: path.read_bytes() for path in unchanged} == unchanged
    with pytest.raises(FileExistsError):
        restore_backup(backup, destination)


@pytest.mark.parametrize("conflict", ["directory", "database", "manifest"])
def test_restore_exclusive_creation_handles_late_conflicts(
    measurement_source,
    tmp_path: Path,
    monkeypatch,
    conflict: str,
) -> None:
    """验证排他创建不会覆盖检查后才出现的目标内容。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。
        monkeypatch: pytest 属性替换器。
        conflict: 模拟刚出现的目录、数据库或完成清单。

    Returns:
        返回示例：
            None  # 并发出现的内容保留，恢复返回冲突错误
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    destination = tmp_path / "restored"
    create_directory = Path.mkdir
    open_file = Path.open

    # 在排他目录创建前产生一个其他操作创建的空目录。
    def create_conflicting_directory(path: Path, *arguments, **keywords) -> None:
        """在目标创建时注入一次目录冲突。

        Args:
            path: 待创建目录。
            arguments: 原调用的位置参数。
            keywords: 原调用的关键字参数。

        Returns:
            返回示例：
                None  # 原 mkdir 执行或抛出 FileExistsError
        """
        if conflict == "directory" and path == destination and not path.exists():
            create_directory(path)
        return create_directory(path, *arguments, **keywords)

    # 在文件排他打开前产生已有内容。
    def open_conflicting_file(path: Path, mode="r", *arguments, **keywords):
        """在复制文件或清单写入时注入一次文件冲突。

        Args:
            path: 待打开文件。
            mode: 文件打开模式。
            arguments: 原调用的位置参数。
            keywords: 原调用的关键字参数。

        Returns:
            返回示例：
                file_handle  # 原 open 返回的文件句柄，冲突时抛出 FileExistsError
        """
        expected_name = "measurements.sqlite3" if conflict == "database" else "manifest.json"
        if conflict != "directory" and path == destination / expected_name and "x" in mode:
            with open_file(path, "wb") as existing_file:
                existing_file.write(b"keep")
        return open_file(path, mode, *arguments, **keywords)

    monkeypatch.setattr(Path, "mkdir", create_conflicting_directory)
    monkeypatch.setattr(Path, "open", open_conflicting_file)
    with pytest.raises(FileExistsError):
        restore_backup(backup, destination)
    if conflict == "directory":
        assert list(destination.iterdir()) == []
    else:
        target = destination / ("measurements.sqlite3" if conflict == "database" else "manifest.json")
        assert target.read_bytes() == b"keep"


def test_restore_rejects_trigger_changes_to_other_fields(measurement_source, tmp_path: Path) -> None:
    """验证恢复事务拒绝触发器改动原识别结果。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # 恢复副本事务回滚，原库与备份均保持原样
    """
    database_path, evidence_root = measurement_source
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER change_text AFTER UPDATE OF evidence_directory ON measurement_records "
            "BEGIN UPDATE measurement_records SET recognized_lines='[]' WHERE session_id=NEW.session_id; END"
        )
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    unchanged = {path: path.read_bytes() for path in (database_path, backup / "measurements.sqlite3")}
    destination = tmp_path / "restored"
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        restore_backup(backup, destination)
    assert not (destination / "manifest.json").exists()
    assert {path: path.read_bytes() for path in unchanged} == unchanged
    assert (destination / "measurements.sqlite3").read_bytes() == (backup / "measurements.sqlite3").read_bytes()


def test_restore_accepts_foreign_platform_source_paths(measurement_source, tmp_path: Path) -> None:
    """验证恢复仅使用清单中的相对映射，不依赖原 Windows 路径。

    Args:
        measurement_source: 临时业务库和证据根目录。
        tmp_path: pytest 创建的临时目录。

    Returns:
        返回示例：
            None  # Windows 来源路径替换为当前目标的绝对路径，恢复校验通过
    """
    database_path, evidence_root = measurement_source
    backup = create_backup(database_path, evidence_root, tmp_path / "backup")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    foreign_path = r"C:\Factory\evidence\20260930\1\session-1"
    with closing(sqlite3.connect(backup / "measurements.sqlite3")) as connection, connection:
        connection.execute("UPDATE measurement_records SET evidence_directory=?", (foreign_path,))
    manifest["source_database"] = r"C:\Factory\measurements.sqlite3"
    manifest["source_evidence_root"] = r"C:\Factory\evidence"
    manifest["records"][0]["source_directory"] = foreign_path
    manifest["files"][0] = describe_file(backup / "measurements.sqlite3", backup)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    restored = restore_backup(backup, tmp_path / "restored")
    record = MeasurementRecordRepo(restored / "measurements.sqlite3").get_record("session-1")
    assert Path(record["evidence_directory"]).is_relative_to(restored)
    assert verify_backup(restored)["image_count"] == 6
