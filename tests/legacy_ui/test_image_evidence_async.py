"""离屏验证有限后台读取、旧结果隔离与关闭清理。"""

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Timer, get_ident
from time import monotonic
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer, QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from src.controller.controller import Result
from ui import image_evidence_viewer as viewer_module
from ui.image_evidence import EvidenceCardImage, EvidenceDecodedImage, EvidenceImage, EvidenceMeasurement
from ui.pages import image_management_page as page_module


def wait_until(condition, timeout: float = 4) -> None:
    """处理 Qt 事件直到指定后台结果已抵达。

    Args:
        condition: 不改变界面的完成条件。
        timeout: 测试等待上限秒数。

    Returns:
        None  # 条件已满足，超时会使测试失败
    """
    deadline = monotonic() + timeout
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


def page_result(session_id: str = "", directory: str = "") -> Result:
    """建立只含零条或一条记录的服务返回值。

    Args:
        session_id: 测试记录编号，空值表示没有记录。
        directory: 该记录保存的证据目录。

    Returns:
        Result.ok({
            "records": [],  # 本页记录
            "total": 0,  # 符合筛选的总数
            "total_pages": 1,  # 有效总页数
        })
    """
    records = []
    if session_id:
        records.append({
            "session_id": session_id,
            "machine_id": "1",
            "machine_name": "测试机器",
            "finish_time": "2026-09-27T08:00:00+00:00",
            "recognized_lines": (session_id,),
            "reviewed_lines": None,
            "final_frequency_hz": 50.0,
            "needs_review": False,
            "reviewed_at": None,
            "evidence_directory": directory,
        })
    return Result.ok({
        "records": records,
        "total": len(records),
        "total_pages": 1,
    })


def group_record(session_id: str, image_count: int = 2) -> EvidenceMeasurement:
    """建立只含路径的真实组展示数据，不访问磁盘。

    Args:
        session_id: 当前测量编号。
        image_count: 组内文件数量。

    Returns:
        EvidenceMeasurement(...)  # 指向测试假路径的当前测量
    """
    return EvidenceMeasurement(
        session_id=session_id,
        machine_id="1",
        machine_name="测试机器",
        finished_at=datetime.now(timezone.utc),
        review_status="normal",
        recognized_lines=(session_id,),
        reviewed_lines=None,
        frequency=50.0,
        evidence_directory=f"/test/{session_id}",
        images=tuple(EvidenceImage(Path(f"/test/{session_id}/{index:03d}.jpg")) for index in range(image_count)),
        evidence_state="available" if image_count else "no_jpg",
        image_count=image_count,
    )


def decoded_image(width: int, thumbnail_size: QSize | None = None) -> EvidenceDecodedImage:
    """为并发测试建立小型内存 QImage。

    Args:
        width: 用于区分请求的原图宽度。
        thumbnail_size: 缩略图请求的目标尺寸。

    Returns:
        EvidenceDecodedImage(QImage(...), "available")  # 小型纯色测试图片
    """
    image = QImage(thumbnail_size or QSize(width, 80), QImage.Format.Format_RGB32)
    image.fill(QColor("blue"))
    return EvidenceDecodedImage(image, "available")


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """复用离屏 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 当前测试的离屏应用
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page_host(qt_application: QApplication):
    """创建尚未显示的页面以便测试控制首个后台查询。

    Args:
        qt_application: 离屏 Qt 应用。

    Returns:
        (
            QWidget(),  # 页面承载窗口
            ImageManagementPage(),  # 待显示的真实数据页面
            Mock(),  # 可控制查询时序的 Controller 替身
        )
    """
    controller = Mock()
    controller.list_measurement_records.return_value = page_result()
    controller.list_record_machines.return_value = Result.ok({"machines": []})
    host = QWidget()
    host.resize(1424, 900)
    layout = QVBoxLayout(host)
    page = page_module.ImageManagementPage(host, controller=controller)
    layout.addWidget(page)
    yield host, page, controller

    # 先等待有限工作完成，再删除窗口与页面对象。
    page.shutdown()
    host.close()
    host.deleteLater()
    qt_application.processEvents()


def test_slow_query_keeps_ui_responsive_and_old_result_cannot_replace_new_page(page_host) -> None:
    """验证慢查询在后台执行，较新的筛选不受旧查询覆盖。

    Args:
        page_host: 尚未显示的页面及查询替身。

    Returns:
        None  # UI 事件在查询结束前执行，最终只显示新筛选结果
    """
    host, page, controller = page_host
    started = Event()
    release = Event()
    query_threads = []

    def read_records(**parameters) -> Result:
        """阻塞首个查询以模拟较慢的计数。

        Args:
            parameters: 页面交给 Controller 的完整筛选参数。

        Returns:
            Result(...)  # 与本次已提交文字对应的记录
        """
        query_threads.append(get_ident())
        if not parameters["text_query"]:
            started.set()
            release.wait(3)
            return page_result("old")
        return page_result("new")

    controller.list_measurement_records.side_effect = read_records
    try:
        host.show()
        wait_until(started.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        wait_until(lambda: bool(ticks))
        assert not release.is_set()
        assert query_threads[0] != get_ident()

        # 新查询立即替换正在查询的页面代次。
        page.text_query_edit.setText("NEW")
        page.apply_text_search()
        wait_until(lambda: bool(page.cards) and page.cards[0].record.session_id == "new")
        release.set()
        wait_until(lambda: page.thread_pool.activeThreadCount() == 0)
        QTest.qWait(20)
        assert page.cards[0].record.session_id == "new"
        assert all(thread_id != get_ident() for thread_id in query_threads)
        assert all(call.kwargs["page_size"] == 12 for call in controller.list_measurement_records.call_args_list)
    finally:
        release.set()


def test_repeated_queries_keep_only_latest_queued_request_and_two_active_workers(page_host) -> None:
    """验证连续筛选不会累积无界队列，最多两个查询同时运行。

    Args:
        page_host: 尚未显示的页面及查询替身。

    Returns:
        None  # 两个运行中的旧查询结束后，只执行最后一个排队请求
    """
    host, page, controller = page_host
    release = Event()
    lock = Lock()
    running = 0
    maximum_running = 0
    queries = []

    def read_records(**parameters) -> Result:
        """记录并发数并阻塞最先运行的两个查询。

        Args:
            parameters: 本次页面筛选参数。

        Returns:
            Result(...)  # 空的当前页查询结果
        """
        nonlocal running, maximum_running
        with lock:
            queries.append(parameters["text_query"])
            running += 1
            maximum_running = max(maximum_running, running)
        release.wait(3)
        with lock:
            running -= 1
        return page_result()

    controller.list_measurement_records.side_effect = read_records
    try:
        host.show()
        wait_until(lambda: len(queries) == 1)
        page.text_query_edit.setText("1")
        page.apply_text_search()
        wait_until(lambda: len(queries) == 2)
        for number in range(2, 32):
            page.text_query_edit.setText(str(number))
            page.apply_text_search()
        assert len(queries) == 2
        assert page.thread_pool.maxThreadCount() == 2

        # 释放后只保留最后一次请求，机器选项也不为旧代次继续读取。
        release.set()
        wait_until(lambda: page.record_count_label.text() == "共 0 次测量")
        assert queries == ["", "1", "31"]
        assert maximum_running == 2
        controller.list_record_machines.assert_called_once()
    finally:
        release.set()


def test_old_card_thumbnail_is_ignored_after_filter_and_render_stays_on_gui_thread(
    page_host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证旧卡片图片不覆盖新卡片，缩略图赋值在主线程。

    Args:
        page_host: 尚未显示的页面及查询替身。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 新目录缩略图保持有效，后台线程不修改控件
    """
    host, page, controller = page_host
    started = Event()
    release = Event()
    ui_threads = []
    io_threads = []
    original_set_evidence = page_module.EvidenceThumbnail.set_evidence

    def read_card(directory: str, cancelled: Event) -> EvidenceCardImage:
        """只阻塞旧记录的图片读取。

        Args:
            directory: 记录保存的目录。
            cancelled: 页面代次取消标志。

        Returns:
            EvidenceCardImage(...)  # 带有目录标识的测试缩略图
        """
        io_threads.append(get_ident())
        if directory == "old-directory":
            started.set()
            release.wait(3)
        return EvidenceCardImage(1, decoded_image(100).image, directory, "available")

    def set_evidence(thumbnail, result: EvidenceCardImage) -> None:
        """记录真正修改控件的线程。

        Args:
            thumbnail: 当前缩略图控件。
            result: 后台返回的卡片数据。

        Returns:
            None  # 原缩略图赋值逻辑已执行
        """
        ui_threads.append(get_ident())
        original_set_evidence(thumbnail, result)

    monkeypatch.setattr(page_module, "read_card_evidence", read_card)
    monkeypatch.setattr(page_module.EvidenceThumbnail, "set_evidence", set_evidence)
    controller.list_measurement_records.return_value = page_result("same-session", "old-directory")
    try:
        host.show()
        wait_until(started.is_set)
        controller.list_measurement_records.return_value = page_result("same-session", "new-directory")
        page.render_records()
        wait_until(lambda: bool(page.cards) and page.cards[0].thumbnail.toolTip() == "new-directory")
        release.set()
        wait_until(lambda: page.thread_pool.activeThreadCount() == 0)
        QTest.qWait(20)
        assert page.cards[0].thumbnail.toolTip() == "new-directory"
        assert ui_threads == [get_ident()]
        assert all(thread_id != get_ident() for thread_id in io_threads)
    finally:
        release.set()


def test_group_switch_and_close_drop_old_detail_results(page_host, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证切换测量及关闭查看器后，旧详情不能重新打开或覆盖内容。

    Args:
        page_host: 页面及查询替身。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 最新组保持正确，关闭后不恢复旧测量
    """
    host, page, controller = page_host
    started = Event()
    release = Event()

    def read_group(current_controller, session_id: str, cancelled: Event) -> EvidenceMeasurement:
        """阻塞旧组以控制详情到达顺序。

        Args:
            current_controller: 现有测量 Controller。
            session_id: 点击的测量编号。
            cancelled: 当前查看器的取消标志。

        Returns:
            EvidenceMeasurement(...)  # 对应编号的组内记录
        """
        if session_id == "old":
            started.set()
            release.wait(3)
        return group_record(session_id, 0)

    monkeypatch.setattr(viewer_module, "read_measurement_evidence", read_group)
    try:
        host.show()
        page.viewer.open_session(controller, "old")
        wait_until(started.is_set)
        page.viewer.open_session(controller, "new")
        wait_until(lambda: page.viewer.record is not None)
        assert page.viewer.record.session_id == "new"
        release.set()
        wait_until(lambda: page.viewer.thread_pool.activeThreadCount() == 0)
        assert page.viewer.record.session_id == "new"

        # 再次延迟详情并关闭，完成后的结果不能重新显示查看器。
        started.clear()
        release.clear()
        page.viewer.open_session(controller, "old")
        wait_until(started.is_set)
        page.viewer.close()
        release.set()
        wait_until(lambda: page.viewer.thread_pool.activeThreadCount() == 0)
        QTest.qWait(20)
        assert not page.viewer.isVisible()
        assert page.viewer.record is None
    finally:
        release.set()


def test_current_full_image_and_visible_thumbnails_are_bounded_and_stale_safe(
    page_host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证只解码当前大图和可见缩略图，旧大图不能覆盖新选择。

    Args:
        page_host: 页面及查询替身。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 长图片组不会预读全部原图，当前画布保持新图片
    """
    host, page, controller = page_host
    started = Event()
    release = Event()
    calls = []

    def read_image(path: Path, thumbnail_size: QSize | None = None) -> EvidenceDecodedImage:
        """阻塞第一张原图，其他小图立即返回。

        Args:
            path: 测试图片路径。
            thumbnail_size: 缩略图边界，None 表示当前原图。

        Returns:
            EvidenceDecodedImage(...)  # 图片下标对应宽度的测试图
        """
        index = int(path.stem)
        calls.append((index, thumbnail_size is None, get_ident()))
        if index == 0 and thumbnail_size is None:
            started.set()
            release.wait(3)
        return decoded_image(100 + index, thumbnail_size)

    monkeypatch.setattr(viewer_module, "decode_evidence_image", read_image)
    try:
        host.show()
        viewer = page.viewer
        viewer.open_record(group_record("group", 40))
        wait_until(started.is_set)
        viewer.select_image(1)
        wait_until(lambda: viewer.loaded_image_index == 1)
        assert viewer.canvas.image_item.pixmap().width() == 101
        release.set()
        wait_until(lambda: viewer.thread_pool.activeThreadCount() == 0)
        QTest.qWait(20)
        assert viewer.image_index == 1
        assert viewer.canvas.image_item.pixmap().width() == 101
        assert {index for index, full, thread_id in calls if full} == {0, 1}
        assert len({index for index, full, thread_id in calls if not full}) <= 12
        assert all(thread_id != get_ident() for index, full, thread_id in calls)

        # 滚动缩略图条只补读新视口，并释放原视口的图片图标。
        initial_thumbnails = set(viewer.loaded_thumbnails)
        viewer.thumbnail_scroll.horizontalScrollBar().setValue(viewer.thumbnail_scroll.horizontalScrollBar().maximum())
        wait_until(lambda: bool(viewer.loaded_thumbnails - initial_thumbnails))
        wait_until(lambda: viewer.thread_pool.activeThreadCount() == 0)
        assert len(viewer.loaded_thumbnails) <= 12
        assert not initial_thumbnails & viewer.loaded_thumbnails
        assert {index for index, full, thread_id in calls if full} == {0, 1}
    finally:
        release.set()


def test_hide_and_shutdown_clear_queued_reads_without_resurrecting_page(page_host) -> None:
    """验证离页丢弃后台结果，关闭等待当前有限读取安全结束。

    Args:
        page_host: 尚未显示的页面及查询替身。

    Returns:
        None  # 隐藏页无新卡片，关闭后页面与查看器线程池已释放
    """
    host, page, controller = page_host
    started = Event()
    release = Event()

    def read_records(**parameters) -> Result:
        """等待测试允许当前数据库读取结束。

        Args:
            parameters: 当前页筛选参数。

        Returns:
            Result(...)  # 不应在离页后展示的测量记录
        """
        started.set()
        release.wait(3)
        return page_result("late")

    controller.list_measurement_records.side_effect = read_records
    try:
        host.show()
        wait_until(started.is_set)
        page.hide()
        release.set()
        wait_until(lambda: page.thread_pool.activeThreadCount() == 0)
        QTest.qWait(20)
        assert page.cards == []
        controller.list_record_machines.assert_not_called()

        # 再次进入时执行新查询，关闭前让有限读取自然完成。
        started.clear()
        release.clear()
        page.show()
        wait_until(started.is_set)
        Timer(0.02, release.set).start()
        page.shutdown()
        assert page.thread_pool is None
        assert page.viewer.thread_pool is None
        QTest.qWait(20)
        assert page.cards == []
    finally:
        release.set()


@pytest.mark.parametrize("width,lower_limit", [(15000, True), (8, False)])
def test_async_image_keeps_fit_zoom_button_limits(
    page_host,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    lower_limit: bool,
) -> None:
    """验证异步原图显示后仍保留适应窗口计算出的缩放边界。

    Args:
        page_host: 页面及查询替身。
        monkeypatch: pytest 提供的替换工具。
        width: 用于触发极小或极大适应比例的测试图宽度。
        lower_limit: True 验证缩小边界，False 验证放大边界。

    Returns:
        None  # 受限按钮保持禁用，100% 后恢复正常的双向缩放
    """
    host, page, controller = page_host
    monkeypatch.setattr(
        viewer_module,
        "decode_evidence_image",
        lambda path, thumbnail_size=None: decoded_image(width, thumbnail_size),
    )
    host.show()
    viewer = page.viewer
    viewer.open_record(group_record("zoom", 1))
    wait_until(lambda: viewer.loaded_image_index == 0)
    scale = viewer.canvas.transform().m11()
    button = viewer.zoom_out_button if lower_limit else viewer.zoom_in_button
    assert scale < 0.1 if lower_limit else scale > 4.0
    assert not button.isEnabled()
    button.click()
    assert viewer.canvas.transform().m11() == scale

    # 手动恢复 100% 后，原图支持正常的放大和缩小。
    viewer.actual_size_button.click()
    assert viewer.canvas.transform().m11() == pytest.approx(1.0)
    assert viewer.zoom_out_button.isEnabled()
    assert viewer.zoom_in_button.isEnabled()
