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
    assert card_data["tone"] == "error"
    assert card_data["status"] == "故障"
    assert card_data["state"] == "相机故障"
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
    assert card_data["tone"] == "error"
    assert card_data["status"] == "故障"
    assert card_data["state"] == "图像采集失败"
    card.set_camera_status.assert_called_once_with("相机故障")
    assert page.connection_states["1"] == ("相机故障", "GetImageBuffer 失败")
