"""使用临时业务库和合成 JPG 验证图片证据的有限读取。"""

from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage

from src.controller.controller import AppController, Result
from src.repo.machine_repo import MachineRepo
from src.repo.measurement_record_repo import MeasurementRecordRepo
from src.service.measurement_record_service import MeasurementRecordService
from ui import image_evidence
from ui.image_evidence import (
    EvidenceReadFailure,
    EvidenceReadSignals,
    EvidenceReadTask,
    build_evidence_measurement,
    decode_evidence_image,
    list_evidence_images,
    read_card_evidence,
    read_measurement_evidence,
)


def save_jpeg(path: Path, size: QSize = QSize(1600, 800)) -> None:
    """在临时目录中生成一张纯色 JPEG。

    Args:
        path: 测试图片的完整路径。
        size: 测试原图的像素尺寸。

    Returns:
        None  # 合成 JPEG 已写入指定路径
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(size, QImage.Format.Format_RGB32)
    image.fill(QColor("#3572a0"))
    assert image.save(str(path), "JPEG")


@pytest.fixture
def measurement_controller(tmp_path: Path) -> AppController:
    """建立只使用临时数据库的真实测量读取链。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        AppController(...)  # 含一条真实测量记录的临时控制器
    """
    # 创建临时业务表和合成证据，不加载配置或设备。
    database_path = tmp_path / "measurements.sqlite3"
    evidence_directory = tmp_path / "stored-evidence"
    save_jpeg(evidence_directory / "stored.JPG")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        MachineRepo.create_table(connection)
        MeasurementRecordRepo.create_table(connection)

    # 使用真实机器表保存详情需要的机器名称。
    machine_repo = MachineRepo(database_path)
    machine_id = machine_repo.insert("测试皮带", "test-camera", "test-meter")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "INSERT INTO measurement_records "
            "(session_id, machine_id, start_time, finish_time, recognized_lines, "
            "final_frequency_hz, evidence_directory, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evidence-session",
                str(machine_id),
                "2026-10-01T08:00:00+00:00",
                "2026-10-01T08:01:00+00:00",
                '["ABC00001", "003"]',
                48.5,
                str(evidence_directory),
                1,
            ),
        )

    # 只接通测量业务链，其他服务不参与证据读取。
    measurement_service = MeasurementRecordService(MeasurementRecordRepo(database_path))
    return AppController(Mock(), measurement_service, Mock(), tmp_path / "unused-configuration")


def test_list_reads_only_exact_stored_directory_and_regular_jpegs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证只枚举指定目录当前层并保留 JPG 与 JPEG 的原始文件名。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 根目录、相邻测量和子目录均未扫描
    """
    # 建立当前测量、同级测量和子目录中的合成图片。
    evidence_directory = tmp_path / "stored" / "session"
    filenames = ("z-last.jpeg", "B-frame.JpEg", "a-first.JPG", "a-second.jpg")
    for filename in filenames:
        save_jpeg(evidence_directory / filename)
    save_jpeg(tmp_path / "root-frame.jpg")
    save_jpeg(evidence_directory.parent / "other-session" / "other.jpg")
    save_jpeg(evidence_directory / "nested" / "hidden.jpeg")

    # 排除伪装成图片的目录和其他后缀文件。
    (evidence_directory / "directory.JPG").mkdir()
    (evidence_directory / "preview.png").write_bytes(b"not a jpeg")
    (evidence_directory / "unfinished.jpg.partial").write_bytes(b"partial")
    original_scandir = os.scandir
    scanned_directories = []

    def scan_selected_directory(directory: str):
        """仅允许枚举记录保存的目录。

        Args:
            directory: 本次读取请求的目录字符串。

        Returns:
            os.scandir(...)  # 指定目录的真实枚举器
        """
        scanned_directories.append(directory)
        assert directory == str(evidence_directory)
        return original_scandir(directory)

    # 通过枚举入口限制证明没有额外根目录扫描。
    monkeypatch.setattr(image_evidence.os, "scandir", scan_selected_directory)
    listing = list_evidence_images(str(evidence_directory), Event())
    assert scanned_directories == [str(evidence_directory)]
    assert listing.state == "available"
    assert [image.filename for image in listing.images] == sorted(filenames, key=lambda name: (name.casefold(), name))
    assert all(image.path.parent == evidence_directory for image in listing.images)
    assert all(set(vars(image)) == {"path"} for image in listing.images)


@pytest.mark.parametrize("directory_kind", ("missing", "empty_path", "regular_file"))
def test_missing_directory_keeps_count_unknown(tmp_path: Path, directory_kind: str) -> None:
    """验证不存在、空字符串和普通文件路径都显示目录缺失。

    Args:
        tmp_path: pytest 提供的临时目录。
        directory_kind: 本轮测试的无效目录类型。

    Returns:
        None  # 目录缺失时数量未知且没有缩略图
    """
    directory = "" if directory_kind == "empty_path" else str(tmp_path / directory_kind)
    if directory_kind == "regular_file":
        Path(directory).write_text("not a directory", encoding="utf-8")

    # 同时验证组内列表和卡片使用相同的目录状态。
    listing = list_evidence_images(directory, Event())
    card = read_card_evidence(directory, Event())
    assert listing.state == card.state == "missing_directory"
    assert listing.images == ()
    assert card.count is None
    assert card.thumbnail.isNull()
    assert card.filename == ""


@pytest.mark.parametrize("add_non_jpeg", (False, True))
def test_empty_or_non_jpeg_directory_reports_zero(tmp_path: Path, add_non_jpeg: bool) -> None:
    """验证空目录和没有 JPG 的目录都显示现存零张。

    Args:
        tmp_path: pytest 提供的临时目录。
        add_non_jpeg: 是否在目录中加入非 JPG 文件。

    Returns:
        None  # 可读取但没有 JPG 的目录返回零张
    """
    if add_non_jpeg:
        (tmp_path / "notes.txt").write_text("test only", encoding="utf-8")
        (tmp_path / "directory.jpeg").mkdir()

    # 目录可读取时零张与未知数量保持区别。
    listing = list_evidence_images(str(tmp_path), Event())
    card = read_card_evidence(str(tmp_path), Event())
    assert listing.state == card.state == "no_jpg"
    assert listing.images == ()
    assert card.count == 0
    assert card.thumbnail.isNull()


@pytest.mark.parametrize(
    "failure,expected_state",
    [(PermissionError("denied"), "access_denied"), (OSError("read failed"), "read_error")],
)
def test_directory_read_failures_keep_count_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected_state: str,
) -> None:
    """验证目录权限和读取错误不会变成零张或崩溃。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        failure: 确定触发的文件系统异常。
        expected_state: 对应的目录展示状态。

    Returns:
        None  # 读取失败保留未知数量和准确状态
    """
    # 使用确定的异常替身，避免测试权限受运行账户影响。
    scanner = Mock(side_effect=failure)
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)
    listing = list_evidence_images(str(tmp_path), Event())
    card = read_card_evidence(str(tmp_path), Event())

    # 两个读取入口都保留目录错误。
    assert listing.state == card.state == expected_state
    assert listing.images == ()
    assert card.count is None
    assert card.thumbnail.isNull()
    assert scanner.call_count == 2


def test_corrupt_jpeg_preserves_filename_and_list_position(tmp_path: Path) -> None:
    """验证损坏 JPG 留在文件列表中且原始文件名不改写。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        None  # 坏图仍计数且逐图解码返回损坏状态
    """
    corrupt_path = tmp_path / "001 原始证据.JPEG"
    corrupt_path.write_bytes(b"broken jpeg")
    save_jpeg(tmp_path / "002-good.JPG")

    # 枚举只反映现存文件，不预先过滤损坏图片。
    listing = list_evidence_images(str(tmp_path), Event())
    decoded = decode_evidence_image(listing.images[0].path)
    assert listing.state == "available"
    assert [image.filename for image in listing.images] == [corrupt_path.name, "002-good.JPG"]
    assert listing.images[0].path == corrupt_path
    assert decoded.state == "corrupt"
    assert decoded.image.isNull()


def test_card_skips_corrupt_first_image_and_keeps_one_scaled_thumbnail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证卡片跳过首张坏图并只保留下一张可用缩略图。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 总数包含坏图且卡片只保留一张缩小后的 QImage
    """
    # 建立坏图、首张可用图和无需解码的第三张图片。
    (tmp_path / "001-broken.jpg").write_bytes(b"broken jpeg")
    save_jpeg(tmp_path / "002-usable.JPEG")
    save_jpeg(tmp_path / "003-unused.jpg")
    decoder = Mock(wraps=decode_evidence_image)
    monkeypatch.setattr(image_evidence, "decode_evidence_image", decoder)

    # 卡片只尝试到首张可用图，全部读取请求都带缩略图边界。
    card = read_card_evidence(str(tmp_path), Event())
    assert card.state == "available"
    assert card.count == 3
    assert card.filename == "002-usable.JPEG"
    assert [call.args[0].name for call in decoder.call_args_list] == ["001-broken.jpg", "002-usable.JPEG"]
    assert all(call.args[1] == QSize(640, 240) for call in decoder.call_args_list)

    # 缩略图保留纵横比且不把整组原图带回卡片。
    assert card.thumbnail.size() == QSize(480, 240)
    assert len([value for value in vars(card).values() if isinstance(value, QImage)]) == 1
    assert not hasattr(card, "images")
    assert decode_evidence_image(tmp_path / "002-usable.JPEG").image.size() == QSize(1600, 800)


def test_all_corrupt_images_keep_total_and_first_filename(tmp_path: Path) -> None:
    """验证全组坏图仍显示现存文件总数和首个真实文件名。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        None  # 坏图没有被伪装成空目录或数量未知
    """
    for filename in ("002-broken.JPEG", "001-broken.JPG", "003-broken.jpg"):
        (tmp_path / filename).write_bytes(b"broken jpeg")

    # 列表数量不受逐图解码失败影响。
    card = read_card_evidence(str(tmp_path), Event())
    assert card.count == 3
    assert card.filename == "001-broken.JPG"
    assert card.state == "corrupt"
    assert card.thumbnail.isNull()


def test_image_removed_after_listing_reports_missing(tmp_path: Path) -> None:
    """验证枚举后消失的单张图片仍保留文件名并显示缺失。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        None  # 已列出的图片返回单图缺失状态
    """
    image_path = tmp_path / "removed.JPEG"
    save_jpeg(image_path)
    listing = list_evidence_images(str(tmp_path), Event())
    image_path.unlink()

    # 文件变化不改写已经提供给查看器的文件列表。
    decoded = decode_evidence_image(listing.images[0].path)
    assert listing.images[0].filename == "removed.JPEG"
    assert decoded.state == "image_missing"
    assert decoded.image.isNull()


@pytest.mark.parametrize(
    "failure,expected_state",
    [(PermissionError("denied"), "image_denied"), (OSError("read failed"), "corrupt")],
)
def test_individual_image_open_failure_returns_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected_state: str,
) -> None:
    """验证单图权限拒绝与其他文件错误返回明确状态。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        failure: 确定触发的文件打开异常。
        expected_state: 对应的单图展示状态。

    Returns:
        None  # 单图读取失败时返回空图和准确状态
    """
    image_path = tmp_path / "denied.JPG"
    save_jpeg(image_path)
    open_file = Mock(side_effect=failure)
    monkeypatch.setattr(Path, "open", open_file)

    # 在图片打开边界模拟错误，不修改真实账户权限。
    decoded = decode_evidence_image(image_path)
    assert decoded.state == expected_state
    assert decoded.image.isNull()
    open_file.assert_called_once_with("rb")


def test_listing_cancels_between_files_and_discards_partial_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证目录读取在文件之间取消并丢弃部分列表。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 第二个文件未检查且部分列表不进入页面
    """
    save_jpeg(tmp_path / "001.jpg")
    save_jpeg(tmp_path / "002.jpg")
    cancelled = Event()

    def inspect_first_file() -> bool:
        """在确认首个文件后取消本次目录读取。

        Args:
            无。

        Returns:
            True  # 首个路径是测试生成的普通文件
        """
        cancelled.set()
        return True

    # 用枚举替身控制取消时机，不依赖线程执行速度。
    first_file = SimpleNamespace(name="001.jpg", path=str(tmp_path / "001.jpg"), is_file=inspect_first_file)
    second_file = SimpleNamespace(name="002.jpg", path=str(tmp_path / "002.jpg"), is_file=Mock(return_value=True))
    scanner = MagicMock()
    scanner.__enter__.return_value = iter((first_file, second_file))
    monkeypatch.setattr(image_evidence.os, "scandir", Mock(return_value=scanner))

    # 已累计的首张图片也不能作为完整结果发布。
    listing = list_evidence_images(str(tmp_path), cancelled)
    assert listing.state == "loading"
    assert listing.images == ()
    second_file.is_file.assert_not_called()
    scanner.__exit__.assert_called_once()


def test_precancelled_listing_and_card_skip_directory_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证已取消的目录和卡片请求不再打开目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 已取消请求不枚举目录或读取任何图片
    """
    cancelled = Event()
    cancelled.set()
    scanner = Mock(side_effect=AssertionError("cancelled listing must not scan"))
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)

    # 直接入口也在执行文件系统操作前检查取消。
    listing = list_evidence_images(str(tmp_path), cancelled)
    card = read_card_evidence(str(tmp_path), cancelled)
    assert listing.state == card.state == "loading"
    assert listing.images == ()
    assert card.count is None
    assert card.thumbnail.isNull()
    scanner.assert_not_called()


def test_card_cancels_between_decodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证卡片坏图解码后取消会阻止下一张图片读取。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 取消后的卡片不继续读取文件或返回部分缩略图
    """
    (tmp_path / "001-broken.jpg").write_bytes(b"broken jpeg")
    save_jpeg(tmp_path / "002-unused.jpg")
    cancelled = Event()
    decoded_paths = []

    def decode_then_cancel(path: Path, thumbnail_size: QSize):
        """完成首张真实解码后取消卡片读取。

        Args:
            path: 本次尝试读取的图片路径。
            thumbnail_size: 卡片要求的缩略图大小。

        Returns:
            EvidenceDecodedImage(QImage(), "corrupt")  # 首张坏图的真实结果
        """
        decoded_paths.append(path)
        decoded = decode_evidence_image(path, thumbnail_size)
        cancelled.set()
        return decoded

    # 在逐文件边界取消，第二张可用图不得继续解码。
    monkeypatch.setattr(image_evidence, "decode_evidence_image", decode_then_cancel)
    card = read_card_evidence(str(tmp_path), cancelled)
    assert decoded_paths == [tmp_path / "001-broken.jpg"]
    assert card.state == "loading"
    assert card.count is None
    assert card.thumbnail.isNull()


def test_read_task_cancellation_skips_later_records_and_late_results() -> None:
    """验证测量切换后尚未开始和刚完成的读取都不发布旧结果。

    Args:
        无。

    Returns:
        None  # 下一条记录不读取且失效结果不发送
    """
    # 以同一个取消标记模拟当前页的两个测量读取任务。
    cancelled = Event()
    signals = EvidenceReadSignals()
    delivered = Mock()
    signals.completed.connect(delivered)
    second_read = Mock(return_value="second record")

    def read_then_cancel() -> str:
        """在首个测量读取完成时取消当前页面。

        Args:
            无。

        Returns:
            "first record"  # 已完成但失效的首个测量结果
        """
        cancelled.set()
        return "first record"

    # 首个任务返回后取消生效，后续任务不进入读取函数。
    first_task = EvidenceReadTask(4, "first-session", read_then_cancel, cancelled, signals)
    second_task = EvidenceReadTask(4, "second-session", second_read, cancelled, signals)
    first_task.run()
    second_task.run()
    delivered.assert_not_called()
    second_read.assert_not_called()


@pytest.mark.parametrize("raise_failure", (False, True))
def test_read_task_publishes_identity_or_read_failure(raise_failure: bool) -> None:
    """验证未取消任务保留代次标识并转换读取异常。

    Args:
        raise_failure: 是否让读取函数抛出可展示异常。

    Returns:
        None  # 一次读取只发送一次结果且保留定位标识
    """
    signals = EvidenceReadSignals()
    delivered = Mock()
    signals.completed.connect(delivered)
    read = Mock(side_effect=OSError("temporary read failure")) if raise_failure else Mock(return_value="record")

    # 同步运行有限任务，避免引入不必要的工作线程。
    task = EvidenceReadTask(7, ("session", 2), read, Event(), signals)
    task.run()
    read.assert_called_once_with()
    delivered.assert_called_once()
    generation, identity, result = delivered.call_args.args
    assert generation == 7
    assert identity == ("session", 2)

    # 读取错误保持显式故障对象，不伪装成空目录。
    if raise_failure:
        assert isinstance(result, EvidenceReadFailure)
        assert result.message == "temporary read failure"
    else:
        assert result == "record"


def test_detail_reloads_newest_saved_directory_through_real_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    measurement_controller: AppController,
) -> None:
    """验证打开查看器经过真实业务链重新取得最新证据目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。
        measurement_controller: 真实临时测量控制器。

    Returns:
        None  # 查看器使用最新详情且不读取旧目录或当前采集配置
    """
    # 先通过真实列表获取卡片当时保存的旧目录。
    page = measurement_controller.list_measurement_records(page=1, page_size=12)
    assert page.success
    stale_record = build_evidence_measurement(page.data["records"][0])
    assert stale_record.images == ()
    assert stale_record.evidence_state == "loading"
    assert stale_record.image_count_caption == "数量未知"

    # 模拟列表出现后数据库中该条测量的目录和人工结果变化。
    newest_directory = tmp_path / "newest-saved-evidence"
    save_jpeg(newest_directory / "newest.JPEG")
    database_path = measurement_controller.measurement_record_service.measurement_record_repo.database_path
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "UPDATE measurement_records SET evidence_directory = ?, reviewed_at = ?, reviewed_lines = ? "
            "WHERE session_id = ?",
            (str(newest_directory), "2026-10-01T09:00:00+00:00", "[]", stale_record.session_id),
        )

    # 只允许读取更新后详情指向的目录，并禁止读取配置。
    original_scandir = os.scandir
    scanner = Mock(wraps=original_scandir)
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)
    read_configuration = Mock(side_effect=AssertionError("evidence must not read configuration"))
    monkeypatch.setattr(measurement_controller.configuration_service, "read_configuration", read_configuration)
    detail = read_measurement_evidence(measurement_controller, stale_record.session_id, Event())

    # 最新详情只返回路径列表，并保留空人工结果的优先级。
    scanner.assert_called_once_with(str(newest_directory))
    read_configuration.assert_not_called()
    assert detail is not None
    assert detail.evidence_directory != stale_record.evidence_directory
    assert detail.evidence_directory == str(newest_directory)
    assert [image.filename for image in detail.images] == ["newest.JPEG"]
    assert detail.image_count == 1
    assert detail.image_count_caption == "现存 1 张"
    assert detail.evidence_state == "available"
    assert detail.machine_name == "测试皮带"
    assert detail.frequency == 48.5
    assert detail.review_status == "reviewed"
    assert detail.recognized_lines == ("ABC00001", "003")
    assert detail.effective_lines == ()
    assert not (tmp_path / "unused-configuration").exists()


def test_deleted_measurement_does_not_scan_directory(
    monkeypatch: pytest.MonkeyPatch,
    measurement_controller: AppController,
) -> None:
    """验证测量详情不存在时不扫描任何证据目录。

    Args:
        monkeypatch: pytest 提供的属性替换工具。
        measurement_controller: 真实临时测量控制器。

    Returns:
        None  # 详情缺失返回空且不枚举文件
    """
    scanner = Mock(side_effect=AssertionError("missing record must not scan"))
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)
    detail = read_measurement_evidence(measurement_controller, "deleted-session", Event())
    assert detail is None
    scanner.assert_not_called()


def test_detail_cancellation_after_database_read_skips_directory(
    monkeypatch: pytest.MonkeyPatch,
    measurement_controller: AppController,
) -> None:
    """验证数据库详情返回时取消会阻止后续目录枚举。

    Args:
        monkeypatch: pytest 提供的属性替换工具。
        measurement_controller: 真实临时测量控制器。

    Returns:
        None  # 已取消的测量详情不再读取文件目录
    """
    cancelled = Event()
    original_get_record = measurement_controller.get_measurement_record

    def read_then_cancel(session_id: str) -> Result:
        """真实读取详情后取消本次请求。

        Args:
            session_id: 点击测量的周期编号。

        Returns:
            Result.ok(...)  # 已完成的真实数据库详情结果
        """
        result = original_get_record(session_id)
        cancelled.set()
        return result

    # 在数据库与目录之间的边界注入取消。
    monkeypatch.setattr(measurement_controller, "get_measurement_record", read_then_cancel)
    scanner = Mock(side_effect=AssertionError("cancelled detail must not scan"))
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)
    assert read_measurement_evidence(measurement_controller, "evidence-session", cancelled) is None
    scanner.assert_not_called()


def test_precancelled_detail_skips_controller_and_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证已取消的详情请求不进入业务查询或目录枚举。

    Args:
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 预先取消阻止数据库和文件系统读取
    """
    controller = Mock()
    controller.get_measurement_record.side_effect = AssertionError("cancelled detail must not query")
    cancelled = Event()
    cancelled.set()
    scanner = Mock(side_effect=AssertionError("cancelled detail must not scan"))
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)

    # 请求取消后直接结束，旧记录编号不再进入控制器。
    assert read_measurement_evidence(controller, "evidence-session", cancelled) is None
    controller.get_measurement_record.assert_not_called()
    scanner.assert_not_called()


def test_detail_controller_failure_remains_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证数据库业务错误不会被当作目录缺失处理。

    Args:
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 详情错误保留原提示并且不开始目录读取
    """
    controller = Mock()
    controller.get_measurement_record.return_value = Result.error("历史详情读取失败。")
    scanner = Mock(side_effect=AssertionError("failed detail must not scan"))
    monkeypatch.setattr(image_evidence.os, "scandir", scanner)

    # 业务失败交由有限任务统一转为错误结果。
    with pytest.raises(RuntimeError, match="历史详情读取失败"):
        read_measurement_evidence(controller, "evidence-session", Event())
    scanner.assert_not_called()
