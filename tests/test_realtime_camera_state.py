"""验证相机故障通知在实时页中的显示优先级。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_camera_fault_remains_visible_after_measurement_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认测量进度更新节点时保留相机故障文字和原因。

    Args:
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 卡片保持相机故障状态，进度节点完成更新
    """
    # 为实时页模块加入项目根目录。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from ui.pages.realtime_page import RealtimePage

    # 建立只记录卡片更新的页面替身。
    card = SimpleNamespace(
        title=SimpleNamespace(text=Mock(return_value="机器 1")),
        frequency_label=SimpleNamespace(text=Mock(return_value="--")),
        steps=SimpleNamespace(update_steps=Mock()),
        update_data=Mock(),
        setToolTip=Mock(),
        progress_session_id=None,
        progress_statuses={},
    )
    page = SimpleNamespace(connection_states={}, cards_by_machine_id={"1": card})

    # 显示相机设备故障和原始原因。
    RealtimePage.update_connection_state(page, "1", "相机故障", "GetImageBuffer 失败")
    card_data = card.update_data.call_args.args[0]
    assert card_data["tone"] == "waiting"
    assert card_data["status"] == "相机故障"
    assert card_data["state"] == "相机故障"
    assert card_data["events"] == (("连接", "GetImageBuffer 失败"),)
    card.setToolTip.assert_called_once_with("GetImageBuffer 失败")

    # 更新测量失败进度并核对故障卡片未被覆盖。
    card.update_data.reset_mock()
    RealtimePage.update_measurement_progress(page, "1", "session-1", "image_capture", "failed")
    card.steps.update_steps.assert_called_once_with({"image_capture": "failed"})
    card.update_data.assert_not_called()
    assert page.connection_states["1"] == ("相机故障", "GetImageBuffer 失败")
