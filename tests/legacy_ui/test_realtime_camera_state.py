"""验证相机故障与测量进度在实时页中分别展示。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def test_camera_fault_and_latest_measurement_progress_are_both_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认原始相机故障保留在连接缓存，本轮流程由测量进度更新。

    Args:
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 相机状态和最新失败阶段分别保留
    """
    # 为实时页模块加入项目根目录。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 建立只记录卡片更新的页面替身。
    card = SimpleNamespace(
        title=SimpleNamespace(text=Mock(return_value="机器 1")),
        frequency_label=SimpleNamespace(text=Mock(return_value="--")),
        update_data=Mock(),
        clear_ocr_result=Mock(),
        set_camera_status=Mock(),
        set_machine_status=Mock(),
        setToolTip=Mock(),
        belt_animation=Mock(),
    )
    page = SimpleNamespace(
        selected_machine_id="1",
        update_dashboard_summary=Mock(),
        refresh_selected_machine_detail=Mock(),
        connection_states={},
        cards_by_machine_id={"1": card},
        ocr_results_by_machine_id={"1": ("session-1", ("AB",))},
        measurement_states_by_machine_id={
            "1": {
                "session_id": "session-1",
                "progress_statuses": {},
                "machine_running": True,
            },
        },
    )

    # 显示相机设备故障和原始原因。
    RealtimePage.update_connection_state(page, "1", "相机故障", "GetImageBuffer 失败")
    card.update_data.assert_not_called()
    card.set_camera_status.assert_called_once_with("相机故障")
    card.setToolTip.assert_called_once_with("GetImageBuffer 失败")
    page.update_dashboard_summary.assert_not_called()

    # 更新测量失败进度并核对本轮流程显示最新阶段。
    card.update_data.reset_mock()
    RealtimePage.update_measurement_progress(
        page,
        "1",
        "session-1",
        "image_capture",
        "failed",
    )
    assert page.measurement_states_by_machine_id["1"]["progress_statuses"] == {
        "image_capture": "failed",
    }
    assert page.refresh_selected_machine_detail.call_count == 2
    card.belt_animation.stop_capture.assert_called_once_with()
    card_data = card.update_data.call_args.args[0]
    assert card_data == {
        "title": "机器 1",
        "state": "图像采集失败",
        "frequency": "--",
    }
    card.set_camera_status.assert_called_once_with("相机故障")
    assert page.connection_states["1"] == ("相机故障", "GetImageBuffer 失败")
    card.set_machine_status.assert_not_called()
    page.update_dashboard_summary.assert_not_called()

    # 旧周期的进度通知不修改当前周期和卡片。
    card.update_data.reset_mock()
    RealtimePage.update_measurement_progress(
        page, "1", "previous-session", "image_capture", "success"
    )
    card.update_data.assert_not_called()
    measurement_state = page.measurement_states_by_machine_id["1"]
    assert measurement_state["session_id"] == "session-1"
    assert measurement_state["progress_statuses"] == {
        "image_capture": "failed",
    }

    # CLOSE 结束当前周期动画并恢复流程和频率占位。
    RealtimePage.update_cycle_closed(page, "1", "session-1")
    assert not measurement_state["machine_running"]
    assert page.refresh_selected_machine_detail.call_count == 3
    card.belt_animation.stop_machine.assert_called_once_with()
    card.update_data.assert_called_once_with({
        "title": "机器 1",
        "state": "--",
        "frequency": "--",
    })

    # 当前周期关闭后保留已缓存的 OCR。
    assert page.ocr_results_by_machine_id["1"] == ("session-1", ("AB",))
    card.clear_ocr_result.assert_not_called()
    page.update_dashboard_summary.assert_not_called()

    # 新周期启动后重置卡片和详情调用记录。
    RealtimePage.update_measurement_progress(
        page, "1", "session-2", "session_start", "success"
    )
    card.update_data.reset_mock()
    card.belt_animation.stop_machine.reset_mock()
    page.refresh_selected_machine_detail.reset_mock()

    # 旧周期的关闭通知不更新新周期的卡片和动画。
    RealtimePage.update_cycle_closed(page, "1", "session-1")
    card.update_data.assert_not_called()
    card.belt_animation.stop_machine.assert_not_called()
    page.refresh_selected_machine_detail.assert_not_called()

    # 旧周期关闭后新周期继续运行。
    measurement_state = page.measurement_states_by_machine_id["1"]
    assert measurement_state["session_id"] == "session-2"
    assert measurement_state["machine_running"]
    assert measurement_state["progress_statuses"] == {
        "session_start": "success",
    }


@pytest.mark.parametrize(
    ("stage", "status", "today_refresh_count"),
    [
        ("image_capture", "running", 0),
        ("frequency_collection", "running", 0),
        ("character_recognition", "running", 0),
        ("evidence_storage", "success", 1),
    ],
)
def test_measurement_progress_only_refreshes_today_after_storage(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    status: str,
    today_refresh_count: int,
) -> None:
    """确认单轮进度不刷新设备总览，正式入库成功只刷新今日统计。

    Args:
        monkeypatch: pytest 提供的属性替换工具。
        stage: 当前周期的处理阶段。
        status: 当前阶段的处理状态。
        today_refresh_count: 预期的今日统计刷新次数。

    Returns:
        返回示例：
            None  # 设备总览没有刷新，进度和详情继续更新，入库成功读取今日统计
    """
    # 为实时页模块加入项目根目录。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 建立展示当前周期进度的机器卡片。
    card = SimpleNamespace(
        title=SimpleNamespace(text=Mock(return_value="机器 1")),
        frequency_label=SimpleNamespace(text=Mock(return_value="--")),
        update_data=Mock(),
        belt_animation=Mock(),
    )

    # 准备页面当前周期缓存和两类总览刷新入口。
    page = SimpleNamespace(
        selected_machine_id="1",
        cards_by_machine_id={"1": card},
        measurement_states_by_machine_id={
            "1": {
                "session_id": "session-1",
                "progress_statuses": {},
                "machine_running": True,
            },
        },
        update_dashboard_summary=Mock(),
        refresh_today_detection_summary=Mock(),
        refresh_selected_machine_detail=Mock(),
    )

    # 核对进度更新仅在正式入库成功时读取今日统计。
    RealtimePage.update_measurement_progress(page, "1", "session-1", stage, status)
    page.update_dashboard_summary.assert_not_called()
    assert page.refresh_today_detection_summary.call_count == today_refresh_count
    card.update_data.assert_called_once()
    page.refresh_selected_machine_detail.assert_called_once_with()


@pytest.mark.parametrize("status", ["online", "offline", "fault"])
def test_machine_status_displays_backend_notification(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """确认实时页缓存并原样显示后端机器整体状态。

    Args:
        monkeypatch: pytest 提供的属性替换工具。
        status: 本次后端发送的整体状态标识。

    Returns:
        返回示例：
            None  # 状态已缓存并显示，总览和选中详情已刷新
    """
    # 为实时页模块加入项目根目录。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 建立只接收整体状态通知的卡片和页面。
    card = SimpleNamespace(set_machine_status=Mock())
    page = SimpleNamespace(
        machine_statuses_by_machine_id={},
        cards_by_machine_id={"1": card},
        selected_machine_id="1",
        update_dashboard_summary=Mock(),
        refresh_selected_machine_detail=Mock(),
    )

    # 核对后端状态被原样交给卡片。
    RealtimePage.update_machine_status(page, "1", status)
    assert page.machine_statuses_by_machine_id["1"] == status
    card.set_machine_status.assert_called_once_with(status)
    page.update_dashboard_summary.assert_called_once_with()
    page.refresh_selected_machine_detail.assert_called_once_with()


def test_machine_status_caches_notification_without_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认卡片不存在时仍保留后端状态供刷新恢复。

    Args:
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 状态已缓存，页面未尝试刷新不存在的卡片
    """
    # 为实时页模块加入项目根目录。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 建立没有卡片的页面。
    page = SimpleNamespace(
        machine_statuses_by_machine_id={},
        cards_by_machine_id={},
        update_dashboard_summary=Mock(),
        refresh_selected_machine_detail=Mock(),
    )

    # 保存通知并跳过卡片刷新。
    RealtimePage.update_machine_status(page, "1", "online")
    assert page.machine_statuses_by_machine_id["1"] == "online"
    page.update_dashboard_summary.assert_not_called()
    page.refresh_selected_machine_detail.assert_not_called()


@pytest.mark.parametrize(
    "machine_statuses, online_count, fault_count",
    [
        (
            {
                "1": "online",
                "2": "online",
                "3": "fault",
                "4": "offline",
            },
            2,
            1,
        ),
        ({}, 0, 0),
    ],
)
def test_device_overview_counts_only_backend_machine_statuses(
    monkeypatch: pytest.MonkeyPatch,
    machine_statuses: dict[str, str],
    online_count: int,
    fault_count: int,
) -> None:
    """确认设备总览只统计后端整体状态，未收到状态的机器不计入在线或故障。

    Args:
        monkeypatch: pytest 提供的属性替换工具。
        machine_statuses: 后端已发送的机器整体状态。
        online_count: 预期在线机器数。
        fault_count: 预期故障机器数。

    Returns:
        返回示例：
            None  # 相机状态和单轮失败未参与设备总览统计
    """
    # 为实时页模块加入项目根目录。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 准备与后端整体状态不同的相机和测量状态。
    page = SimpleNamespace(
        machines=[{"id": machine_id} for machine_id in range(1, 5)],
        machine_statuses_by_machine_id=machine_statuses,
        device_overview_card=SimpleNamespace(set_values=Mock()),
        connection_states={
            "1": ("相机故障", "取帧失败"),
            "3": ("相机已连接", ""),
            "4": ("相机已连接", ""),
        },
        measurement_states_by_machine_id={
            "2": {
                "progress_statuses": {
                    "character_recognition": "failed",
                },
                "machine_running": True,
            },
        },
    )

    # 核对设备总览仅使用后端整体状态。
    RealtimePage.update_dashboard_summary(page)
    page.device_overview_card.set_values.assert_called_once_with(
        4, online_count, fault_count
    )


@pytest.mark.parametrize("stage", ["evidence_storage", "character_recognition"])
@pytest.mark.parametrize("status", ["success", "failed"])
def test_old_cycle_storage_refreshes_today_without_changing_latest_card(
    monkeypatch: pytest.MonkeyPatch, stage: str, status: str,
) -> None:
    """确认旧轮入库刷新今日统计，旧进度、文字和关闭不影响新轮。

    Args:
        monkeypatch: pytest 属性替换工具。
        stage: 旧轮通知阶段。
        status: 旧轮阶段状态。

    Returns:
        返回示例：
            None  # 只有旧轮入库成功刷新统计，最新周期卡片和动画保持原样
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 保留最新周期的文字、频率和动画状态。
    card = Mock()
    latest = {
        "session_id": "new", "machine_running": True,
        "progress_statuses": {"image_capture": "success"},
    }
    page = SimpleNamespace(
        selected_machine_id="1", cards_by_machine_id={"1": card},
        measurement_states_by_machine_id={"1": latest},
        ocr_results_by_machine_id={"1": ("new", ("2926215C",))},
        refresh_today_detection_summary=Mock(),
        refresh_selected_machine_detail=Mock(),
    )

    # 旧轮数据只触发必要统计读取，不改变最新周期的缓存或卡片。
    RealtimePage.update_measurement_progress(page, "1", "old", stage, status)
    RealtimePage.update_ocr_result(page, "1", "old", ("2926214C",))
    RealtimePage.update_cycle_closed(page, "1", "old")
    assert page.refresh_today_detection_summary.call_count == int(
        stage == "evidence_storage" and status == "success"
    )
    assert latest == {
        "session_id": "new", "machine_running": True,
        "progress_statuses": {"image_capture": "success"},
    }
    assert page.ocr_results_by_machine_id["1"] == ("new", ("2926215C",))
    assert card.mock_calls == []
    page.refresh_selected_machine_detail.assert_not_called()


def test_capacity_warning_reaches_page_through_runtime_thread_and_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认后台线程的满载通知经 Controller 到达实时页提示入口。

    Args:
        monkeypatch: pytest 属性替换工具。

    Returns:
        返回示例：
            None  # 实际 Qt 信号贯通，页面使用现有 warning 提示
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QApplication
    from src.controller.controller import AppController
    from src.runtime.system_runtime_thread import SystemRuntimeThread
    from ui.pages.realtime_page import RealtimePage

    # 创建无可见窗口的 Qt 应用、真实 Controller 和后台线程。
    application = QApplication.instance() or QApplication([])
    controller = AppController(Mock(), Mock(), Mock(), Path("unused-config"))
    thread = SystemRuntimeThread(Path("unused-config"))
    thread.stop_requested.set()
    warning = "后台处理积压，本次启动未采集，请暂停换带"
    runtime = SimpleNamespace(failure=None, stop=AsyncMock())

    async def start_runtime(*callbacks):
        """通过 Runtime 接入的回调发出积压提示。

        Args:
            callbacks: 后台线程传入的通知入口。

        Returns:
            返回示例：
                None  # 满载提示已发出
        """
        callbacks[5]("1", warning)

    runtime.start = start_runtime
    monkeypatch.setattr(
        "src.runtime.system_runtime_thread.load_config",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        "src.runtime.system_runtime_thread.SystemRuntime",
        Mock(return_value=runtime),
    )
    monkeypatch.setattr(
        "src.controller.controller.SystemRuntimeThread",
        Mock(return_value=thread),
    )
    show_warning = Mock()
    monkeypatch.setattr("ui.pages.realtime_page.InfoBar.warning", show_warning)
    page = SimpleNamespace()
    controller.machine_warning_signal.connect(
        lambda machine_id, message: RealtimePage.show_machine_warning(
            page, machine_id, message
        )
    )
    received = QSignalSpy(controller.machine_warning_signal)

    # 启动真实 QThread，只检查信号和提示参数。
    try:
        assert controller.start_monitoring().success
        assert thread.wait(2000)
        application.processEvents()
        assert received.count() == 1
        assert received.at(0) == ["1", warning]
        show_warning.assert_called_once_with("机器 1", warning, duration=-1, parent=page)
        runtime.stop.assert_awaited_once()
    finally:
        thread.stop_requested.set()
        thread.wait(2000)
