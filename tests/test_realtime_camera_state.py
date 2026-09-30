"""验证相机故障与测量进度在实时页中分别展示。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_camera_fault_and_latest_measurement_progress_are_both_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认相机故障保留在连接状态，当前流程显示最新测量阶段。

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
        set_camera_status=Mock(),
        set_machine_status=Mock(),
        setToolTip=Mock(),
        progress_session_id="session-1",
        progress_statuses={},
        belt_animation=Mock(),
    )
    page = SimpleNamespace(
        selected_machine_id="1",
        update_dashboard_summary=Mock(),
        refresh_selected_machine_detail=Mock(),
        connection_states={},
        cards_by_machine_id={"1": card},
        ocr_results_by_machine_id={"1": ("session-1", (), ())},
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
    card_data = card.update_data.call_args.args[0]
    assert card_data == {
        "title": "机器 1",
        "state": "相机故障",
        "frequency": "--",
    }
    card.set_camera_status.assert_called_once_with("相机故障")
    card.setToolTip.assert_called_once_with("GetImageBuffer 失败")

    # 更新测量失败进度并核对当前流程显示最新阶段。
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

    # 旧周期的进度通知不修改当前周期和卡片。
    card.update_data.reset_mock()
    RealtimePage.update_measurement_progress(
        page, "1", "previous-session", "image_capture", "success"
    )
    card.update_data.assert_not_called()
    assert card.progress_session_id == "session-1"
    assert card.progress_statuses == {
        "image_capture": "failed",
    }


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
