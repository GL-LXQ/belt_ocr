"""显式备份、校验或恢复业务库与关联证据，不修改或清理源数据。"""

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]


def describe_file(file_path: Path, backup_directory: Path) -> dict:
    """计算单个备份文件的相对路径、大小和摘要。

    Args:
        file_path: 已复制到备份目录的文件。
        backup_directory: 备份根目录。

    Returns:
        返回示例：
            {
                "path": "evidence/20261002/1/session/1.jpg",  # 备份内相对路径
                "size": 1024,  # 文件字节数
                "sha256": "abcdef...",  # 文件内容摘要
            }
    """
    # 分块读取文件，避免将大文件整体加载到内存。
    digest = hashlib.sha256()
    with file_path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": file_path.relative_to(backup_directory).as_posix(),
        "size": file_path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def read_snapshot_records(database_path: Path) -> tuple[list[tuple[str, str]], int]:
    """检查快照完整性并读取测量记录的图片目录与机器数量。

    Args:
        database_path: 要以只读方式检查的数据库快照。

    Returns:
        返回示例：
            (
                [("session-1", "/data/evidence/20261002/1/session-1")],  # 周期与原图片目录
                1,  # 机器记录总数，含已停用或软删除机器
            )
    """
    # 校验整个 SQLite 文件，再读取备份需要的关联信息。
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("业务库完整性检查失败。")
        records = connection.execute(
            "SELECT session_id, evidence_directory FROM measurement_records ORDER BY session_id"
        ).fetchall()
        machine_count = connection.execute("SELECT COUNT(*) FROM machine").fetchone()[0]
    return records, machine_count


def resolve_backup_path(backup_directory: Path, relative_path: str) -> Path:
    """检查清单中的相对路径并限定到备份目录内部。

    Args:
        backup_directory: 备份根目录绝对路径。
        relative_path: 清单保存的正斜线相对路径。

    Returns:
        返回示例：
            Path("/backup/evidence/20261002/1/session")  # 已验证存在的备份内部路径
    """
    # 拒绝绝对路径、上级跳转和非跨平台路径。
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("备份清单路径必须是非空字符串。")
    portable_path = PurePosixPath(relative_path)
    if (
        portable_path.is_absolute() or ".." in portable_path.parts or "\\" in relative_path or ":" in relative_path
        or relative_path != portable_path.as_posix() or relative_path == "."
    ):
        raise ValueError(f"备份清单路径不合法：{relative_path}")

    # 逐级拒绝符号链接，再检查实际路径归属。
    candidate = backup_directory
    for component in portable_path.parts:
        candidate = candidate / component
        if candidate.is_symlink():
            raise ValueError(f"备份清单路径使用符号链接：{relative_path}")
    resolved = candidate.resolve(strict=True)
    if resolved == backup_directory or not resolved.is_relative_to(backup_directory):
        raise ValueError(f"备份清单路径超出目录或使用符号链接：{relative_path}")
    return resolved


def validate_backup_manifest(manifest: dict) -> None:
    """检查外部备份清单的字段类型与基础格式。

    Args:
        manifest: 从 JSON 读取或恢复流程更新的清单。

    Returns:
        返回示例：
            None  # 字段类型有效，内容与实际文件由后续校验核对
    """
    # 检查版本、来源和汇总字段。
    if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int:
        raise ValueError("备份清单格式不合法。")
    if manifest["format_version"] != 1:
        raise ValueError("不支持的备份清单版本。")
    for name in ("created_at_utc", "source_database", "source_evidence_root"):
        if not isinstance(manifest.get(name), str) or not manifest[name]:
            raise ValueError(f"备份清单字段不合法：{name}")
    for name in ("source_database", "source_evidence_root"):
        if not Path(manifest[name]).is_absolute() and not PureWindowsPath(manifest[name]).is_absolute():
            raise ValueError(f"备份清单来源必须是绝对路径：{name}")
    for name in ("measurement_count", "machine_count", "image_count"):
        if type(manifest.get(name)) is not int or manifest[name] < 0:
            raise ValueError(f"备份清单数量不合法：{name}")

    # 检查每个文件的路径、大小和 SHA-256 字段。
    if not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("records"), list):
        raise ValueError("备份清单的文件与记录必须是列表。")
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("备份文件字段不合法。")
        if not isinstance(item["path"], str) or type(item["size"]) is not int or item["size"] < 0:
            raise ValueError("备份文件路径或大小不合法。")
        digest = item["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise ValueError("备份文件 SHA-256 不合法。")

    # 检查每条记录的标识和跨平台原路径。
    for item in manifest["records"]:
        if not isinstance(item, dict) or set(item) != {"session_id", "source_directory", "backup_directory"}:
            raise ValueError("备份记录字段不合法。")
        if any(not isinstance(value, str) or not value for value in item.values()):
            raise ValueError("备份记录字段必须是非空字符串。")
        source_directory = item["source_directory"]
        if not Path(source_directory).is_absolute() and not PureWindowsPath(source_directory).is_absolute():
            raise ValueError("备份记录的原证据目录必须是绝对路径。")


def verify_backup(backup_directory: Path, *, manifest: dict | None = None) -> dict:
    """核对备份清单、文件摘要、数据库完整性和证据路径映射。

    Args:
        backup_directory: 已发布备份或本次暂存目录。
        manifest: 恢复期间尚未写入的清单；None 表示读取正式清单。

    Returns:
        返回示例：
            {
                "measurement_count": 2,  # 测量记录数
                "machine_count": 1,  # 机器记录数
                "image_count": 8,  # JPG 证据文件数
            }
    """
    # 读取清单格式与文件列表，不修改备份文件。
    backup_directory = backup_directory.resolve(strict=True)
    manifest_files = set()
    if manifest is None:
        manifest_path = resolve_backup_path(backup_directory, "manifest.json")
        if not manifest_path.is_file():
            raise ValueError("备份清单不是普通文件。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_files.add("manifest.json")
    validate_backup_manifest(manifest)
    files = manifest["files"]
    expected_paths = {item["path"] for item in files}
    if len(expected_paths) != len(files) or "measurements.sqlite3" not in expected_paths:
        raise ValueError("备份清单包含重复文件或缺少业务库。")

    # 逐文件核对大小与摘要，并拒绝未登记的文件或符号链接。
    for item in files:
        file_path = resolve_backup_path(backup_directory, item["path"])
        if not file_path.is_file():
            raise ValueError(f"备份内容不是普通文件：{item['path']}")
        if describe_file(file_path, backup_directory) != item:
            raise ValueError(f"备份文件校验失败：{item['path']}")
    actual_paths = set()
    for file_path in backup_directory.rglob("*"):
        if file_path.is_symlink():
            raise ValueError(f"备份中不允许符号链接：{file_path}")
        if file_path.is_file():
            actual_paths.add(file_path.relative_to(backup_directory).as_posix())
        elif not file_path.is_dir():
            raise ValueError(f"备份中存在非普通文件：{file_path}")
    if actual_paths != expected_paths | manifest_files:
        raise ValueError("备份文件数量与清单不一致。")

    # 核对数据库记录和路径映射，确保每条记录都有完整证据目录。
    records, machine_count = read_snapshot_records(backup_directory / "measurements.sqlite3")
    mappings = manifest["records"]
    if records != [(item["session_id"], item["source_directory"]) for item in mappings]:
        raise ValueError("备份路径映射与数据库记录不一致。")
    image_paths = set()
    for item in mappings:
        evidence_directory = resolve_backup_path(backup_directory, item["backup_directory"])
        relative_directory = PurePosixPath(item["backup_directory"])
        if (
            len(relative_directory.parts) < 2 or relative_directory.parts[0] != "evidence"
            or not evidence_directory.is_dir()
        ):
            raise ValueError(f"备份证据目录不合法：{item['session_id']}")
        images = [
            path for path in evidence_directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
        ]
        if not images:
            raise ValueError(f"记录缺少 JPG 证据：{item['session_id']}")
        image_paths.update(path.relative_to(backup_directory).as_posix() for path in images)
    if expected_paths != image_paths | {"measurements.sqlite3"}:
        raise ValueError("备份图片与记录目录不一致。")

    # 核对清单中的总数并返回检查结果。
    counts = {
        "measurement_count": len(records),
        "machine_count": machine_count,
        "image_count": len(image_paths),
    }
    if any(manifest[name] != value for name, value in counts.items()):
        raise ValueError("备份数量与清单不一致。")
    return counts


def restore_backup(backup_directory: Path, destination: Path) -> Path:
    """在全新目录恢复独立业务库和证据，失败时保留未完成内容。

    Args:
        backup_directory: 已有且可通过校验的备份目录。
        destination: 操作者指定的全新目录，不允许已存在或位于备份及原证据目录内部。

    Returns:
        返回示例：
            Path("/recovered/20261002")  # 记录路径已重映射且完整校验通过的恢复目录
    """
    # 完整校验备份，再读取恢复所需的文件和记录映射。
    backup_directory = backup_directory.resolve(strict=True)
    verify_backup(backup_directory)
    manifest = json.loads((backup_directory / "manifest.json").read_text(encoding="utf-8"))
    validate_backup_manifest(manifest)

    # 拒绝已有目标以及会写入备份或原证据树的目标。
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"恢复目标已存在：{destination}")
    destination = destination.resolve()
    source_evidence_root = Path(manifest["source_evidence_root"])
    if destination.is_relative_to(backup_directory):
        raise ValueError("恢复目标不能位于备份目录内部。")
    if source_evidence_root.is_absolute() and destination.is_relative_to(source_evidence_root.resolve()):
        raise ValueError("恢复目标不能位于原证据目录内部。")
    destination.mkdir(parents=True, exist_ok=False)

    # 仅复制清单登记的文件，使用排他创建并同步写盘。
    for item in manifest["files"]:
        source_path = resolve_backup_path(backup_directory, item["path"])
        if not source_path.is_file():
            raise ValueError(f"备份内容不是普通文件：{item['path']}")
        target_path = destination / item["path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.parent != destination:
            resolve_backup_path(destination, target_path.parent.relative_to(destination).as_posix())
        with source_path.open("rb") as source_file, target_path.open("xb") as target_file:
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
    verify_backup(destination, manifest=manifest)

    # 仅在复制库内按 Session 重映射路径，其他业务字段保持原值。
    database_path = destination / "measurements.sqlite3"
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=rw", uri=True)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        with connection:
            for item in manifest["records"]:
                evidence_directory = str(destination / item["backup_directory"])
                connection.execute(
                    "UPDATE measurement_records SET evidence_directory=? WHERE session_id=?",
                    (evidence_directory, item["session_id"]),
                )
                item["source_directory"] = evidence_directory

    # 更新恢复副本的清单并再次核对全部内容。
    manifest["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["source_database"] = str(database_path)
    manifest["source_evidence_root"] = str(destination / "evidence")
    for item in manifest["files"]:
        if item["path"] == "measurements.sqlite3":
            item.update(describe_file(database_path, destination))
    verify_backup(destination, manifest=manifest)

    # 所有内容通过校验后才排他写入正式清单，不覆盖已有文件。
    with (destination / "manifest.json").open("x", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())
    return destination


def create_backup(
    database_path: Path,
    evidence_root: Path,
    destination: Path,
    timeout_seconds: float = 10.0,
) -> Path:
    """生成已校验的业务库与证据备份，失败时保留暂存目录。

    Args:
        database_path: 原业务库文件，必须已经存在。
        evidence_root: 原证据根目录。
        destination: 新备份目录，不允许已存在或位于证据根目录内部。
        timeout_seconds: SQLite 快照操作的最大等待秒数。

    Returns:
        返回示例：
            Path("/backup/20261002")  # 校验通过后发布的备份目录
    """
    # 解析源路径并检查目标，阻止覆盖和递归备份。
    database_path = database_path.resolve(strict=True)
    evidence_root = evidence_root.resolve(strict=True)
    destination = destination.absolute()
    if not database_path.is_file() or not evidence_root.is_dir():
        raise ValueError("必须提供已有业务库文件和证据根目录。")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("快照等待时间必须是有限的正数。")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"备份目标已存在：{destination}")
    destination = destination.resolve()
    if destination.is_relative_to(evidence_root):
        raise ValueError("备份目标不能位于源证据目录内部。")
    staging_directory = destination.with_name(destination.name + ".partial")
    staging_directory.mkdir(parents=True, exist_ok=False)

    # 通过 SQLite 在线备份接口读取一致快照，超时中止并保留暂存内容。
    deadline = time.monotonic() + timeout_seconds

    def check_progress(status: int, remaining: int, total: int) -> None:
        """在每批快照复制或锁等待后检查超时。

        Args:
            status: SQLite 本批复制状态。
            remaining: 剩余页面数。
            total: 总页面数。

        Returns:
            返回示例：
                None  # 继续复制，超时则抛出 TimeoutError
        """
        if time.monotonic() > deadline:
            raise TimeoutError("业务库快照超时，请避开写入繁忙时段重试。")

    snapshot_path = staging_directory / "measurements.sqlite3"
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=0.05)) as source:
        source.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            source.backup(snapshot, pages=128, progress=check_progress, sleep=0.05)

            # 仅将备份副本转换为独立文件，避免携带 WAL 旁文件。
            snapshot.execute("PRAGMA journal_mode=DELETE")
    records, machine_count = read_snapshot_records(snapshot_path.resolve())

    # 仅复制快照引用的目录内全部 JPG，不使用界面预览的图片数量限制。
    files = [describe_file(snapshot_path, staging_directory)]
    mappings = []
    copied_directories = set()
    for session_id, source_directory in records:
        if not isinstance(source_directory, str) or not Path(source_directory).is_absolute():
            raise ValueError(f"证据目录不是有效绝对路径：{session_id}")
        evidence_directory = Path(source_directory).resolve(strict=True)
        if evidence_directory == evidence_root or not evidence_directory.is_relative_to(evidence_root):
            raise ValueError(f"证据目录超出配置根目录：{session_id}")
        relative_directory = Path("evidence") / evidence_directory.relative_to(evidence_root)
        mappings.append({
            "session_id": session_id,
            "source_directory": source_directory,
            "backup_directory": relative_directory.as_posix(),
        })
        if evidence_directory in copied_directories:
            continue

        # 拒绝嵌套目录和符号链接，按现有每轮平铺 JPG 的目录结构复制。
        image_paths = []
        for image_path in evidence_directory.iterdir():
            if image_path.is_symlink() or image_path.is_dir():
                raise ValueError(f"证据目录存在符号链接或嵌套目录：{image_path}")
            if image_path.suffix.lower() in {".jpg", ".jpeg"}:
                if not image_path.is_file():
                    raise ValueError(f"证据不是普通图片文件：{image_path}")
                image_paths.append(image_path)
        if not image_paths:
            raise ValueError(f"记录缺少 JPG 证据：{session_id}")
        target_directory = staging_directory / relative_directory
        target_directory.mkdir(parents=True)
        for image_path in sorted(image_paths):
            target_path = target_directory / image_path.name
            source_digest = describe_file(image_path, evidence_root)["sha256"]
            shutil.copyfile(image_path, target_path)
            with target_path.open("rb+") as copied_file:
                os.fsync(copied_file.fileno())
            metadata = describe_file(target_path, staging_directory)
            if metadata["sha256"] != source_digest:
                raise ValueError(f"证据图片在复制期间变化或复制校验失败：{image_path}")
            files.append(metadata)
        copied_directories.add(evidence_directory)

    # 保存快照来源、文件清单和恢复时需要的每条记录路径映射。
    manifest = {
        "format_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_database": str(database_path),
        "source_evidence_root": str(evidence_root),
        "measurement_count": len(records),
        "machine_count": machine_count,
        "image_count": len(files) - 1,
        "records": mappings,
        "files": files,
    }
    with (staging_directory / "manifest.json").open("w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())

    # 全部验证通过后才发布正式备份，不覆盖已经出现的目标目录。
    verify_backup(staging_directory)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"备份目标已存在：{destination}")
    staging_directory.rename(destination)
    return destination


def main(arguments: list[str] | None = None) -> int:
    """解析显式备份、校验或恢复命令并输出结果。

    Args:
        arguments: 命令参数，None 表示使用进程命令行。

    Returns:
        返回示例：
            0  # 备份、校验或恢复成功
            1  # 操作失败，未修改源数据
    """
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--destination", type=Path, help="新备份目录，不允许覆盖")
    actions.add_argument("--verify", type=Path, help="仅检查一个已有备份")
    actions.add_argument("--restore", type=Path, nargs=2, metavar=("BACKUP", "NEW_DIRECTORY"), help="恢复到全新目录")
    parser.add_argument("--config-dir", type=Path, default=PROJECT_DIRECTORY / "config", help="现有配置目录")
    parser.add_argument("--database", type=Path, help="显式覆盖业务库路径，相对当前工作目录")
    parser.add_argument("--evidence-root", type=Path, help="显式覆盖证据根目录，相对当前工作目录")
    parser.add_argument("--timeout-seconds", type=float, default=10.0, help="SQLite 快照最大等待秒数")
    options = parser.parse_args(arguments)

    # 校验和恢复不读取生产配置；创建备份沿用现有配置路径规则。
    try:
        if options.verify:
            counts = verify_backup(options.verify)
            print(f"备份校验通过：{counts}")
            return 0
        if options.restore:
            destination = restore_backup(*options.restore)
            print(f"恢复完成：{destination}；生产配置未切换。")
            return 0
        settings = {}
        if options.database is None or options.evidence_root is None:
            sys.path.insert(0, str(PROJECT_DIRECTORY / "src"))
            from config_util import read_configuration_settings
            settings = read_configuration_settings(options.config_dir)
        destination = create_backup(
            options.database or settings["database_path"],
            options.evidence_root or settings["evidence_directory"],
            options.destination,
            options.timeout_seconds,
        )
        print(f"备份完成：{destination}")
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        if options.restore:
            print(
                f"恢复失败：{error}；本次恢复未完成。目标：{options.restore[1].absolute()}；"
                "不会自动删除任何内容，请检查后使用新目标重试。",
                file=sys.stderr,
            )
        else:
            print(f"备份失败：{error}；已产生的 .partial 目录保留，请检查后使用新目标重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
