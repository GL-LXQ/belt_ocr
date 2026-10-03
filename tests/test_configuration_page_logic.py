"""不创建 Qt 窗口，使用控件替身验证配置页面的状态与保存边界。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.controller.controller import Result
from ui.pages.system_configuration_page import SystemConfigurationPage


@pytest.mark.parametrize(
    ("dirty", "valid", "running", "read_error", "enabled"),
    [
        (False, True, False, "", False),
        (True, True, False, "", True),
        (True, False, False, "", False),
        (True, True, True, "", False),
        (True, True, False, "读取失败", False),
    ],
)
def test_save_enablement_requires_dirty_valid_stopped_and_readable(
    dirty: bool,
    valid: bool,
    running: bool,
    read_error: str,
    enabled: bool,
) -> None:
    """验证保存按钮同时受修改、校验、监测和读取状态约束。

    Args:
        dirty: 是否修改字段。
        valid: 草稿校验是否通过。
        running: 是否仍有监测线程。
        read_error: 最近读取失败说明。
        enabled: 预期保存按钮状态。

    Returns:
        返回示例：
            None  # 通过控件替身确认状态，没有创建 QApplication
    """
    # 创建仅有页面状态方法所需属性的替身。
    draft = {"camera_gain": 1.5 if dirty else 0.0}
    controller = Mock()
    controller.validate_configuration.return_value = (
        Result.ok() if valid else Result.error("增益错误", {"field": "camera_gain"})
    )
    controller.is_monitoring_running.return_value = Result.ok({"running": running})
    page = SimpleNamespace(
        configuration_loaded=True,
        get_draft_values=Mock(return_value=draft),
        baseline_values={"camera_gain": 0.0},
        editors={"camera_gain": SimpleNamespace(value_kind="number")},
        io_editors={},
        controller=controller,
        saved_message="",
        read_error_message=read_error,
        status_label=Mock(),
        status_hint=Mock(),
        save_button=Mock(),
        undo_button=Mock(),
        error_card=Mock(),
        error_label=Mock(),
        field_error_button=Mock(),
    )

    # 直接调用状态逻辑，不构造 QWidget 或执行事件循环。
    SystemConfigurationPage.update_draft_state(page)
    assert page.is_dirty is dirty
    page.save_button.setEnabled.assert_called_once_with(enabled)
    assert page.validation_field == (None if valid else "camera_gain")
    controller.save_configuration.assert_not_called()


def test_draft_preserves_null_and_leaves_missing_di_channel_unassigned() -> None:
    """验证新 DI 行不自动生成通道，草稿与基线相互独立。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 缺失绑定保持缺失，null 和只读字段保持原值
    """
    # 让未绑定机器返回空值，已有机器返回实际零索引。
    baseline = {"camera_gain": None, "capture_window_ms": 1000, "io_machine_channels": {"1": 0}}
    page = SimpleNamespace(
        baseline_values=deepcopy(baseline),
        editors={"camera_gain": Mock(get_value=Mock(return_value=None))},
        strobe_combo=Mock(currentData=Mock(return_value=None)),
        io_editors={
            "1": Mock(get_value=Mock(return_value=0)),
            "2": Mock(get_value=Mock(return_value=None)),
        },
    )
    draft = SystemConfigurationPage.get_draft_values(page)
    assert draft == baseline
    draft["io_machine_channels"]["1"] = 4
    assert page.baseline_values == baseline


@pytest.mark.parametrize("success", [True, False])
def test_save_updates_baseline_only_after_success(success: bool) -> None:
    """验证保存结果控制基线更新，失败保留草稿并定位字段。

    Args:
        success: 保存是否成功。

    Returns:
        返回示例：
            None  # 成功发布新基线，失败保持旧基线和输入
    """
    # 提供保存前后的独立数据，以及不产生 UI 的操作替身。
    baseline = {"camera_gain": 0.0}
    draft = {"camera_gain": 1.5}
    controller = Mock()
    controller.save_configuration.return_value = (
        Result.ok({"settings": draft}, "配置已保存，下次开始监测时生效。")
        if success else Result.error("增益错误", {"field": "camera_gain"})
    )
    page = SimpleNamespace(
        configuration_loaded=True,
        is_dirty=True,
        baseline_values=deepcopy(baseline),
        controller=controller,
        get_draft_values=Mock(return_value=draft),
        undo_changes=Mock(),
        populate_readonly_values=Mock(),
        update_draft_state=Mock(),
        error_label=Mock(),
        error_card=Mock(),
        field_error_button=Mock(),
        status_hint=Mock(),
        focus_validation_error=Mock(),
    )

    # 成功只通过已有回填入口发布基线，失败不回填控件。
    SystemConfigurationPage.save_configuration(page)
    assert page.baseline_values == (draft if success else baseline)
    assert page.configuration_loaded
    if success:
        page.update_draft_state.assert_called_once_with()
        assert "下次开始监测时生效" in page.saved_message
        page.focus_validation_error.assert_not_called()
    else:
        page.undo_changes.assert_not_called()
        page.update_draft_state.assert_not_called()
        page.focus_validation_error.assert_called_once_with()
        assert page.validation_field == "camera_gain"
        assert page.is_dirty


@pytest.mark.parametrize("success", [True, False])
def test_reload_preserves_only_user_changes_and_reports_failures(success: bool) -> None:
    """验证重新读取保留用户修改，并接收外部修改的未编辑字段。

    Args:
        success: 重新读取是否成功。

    Returns:
        返回示例：
            None  # 成功保留编辑并提示核对，失败保留原基线和草稿
    """
    # 外部同时修改已编辑的增益和未编辑的周期期限。
    baseline = {"camera_gain": 0.0, "max_cycle_open_ms": 60000, "io_machine_channels": {"1": 0}}
    draft = {"camera_gain": 1.5, "max_cycle_open_ms": 60000, "io_machine_channels": {"1": 3}}
    external = {"camera_gain": 2.0, "max_cycle_open_ms": 70000, "io_machine_channels": {"1": 9}}
    controller = Mock()
    controller.read_configuration.return_value = (
        Result.ok({"settings": external}) if success else Result.error("无法读取配置文件")
    )
    gain_editor = Mock(value_kind="number")
    cycle_editor = Mock(value_kind="integer")
    channel_editor = Mock()
    page = SimpleNamespace(
        configuration_loaded=True,
        baseline_values=deepcopy(baseline),
        controller=controller,
        get_draft_values=Mock(return_value=draft),
        editors={"camera_gain": gain_editor, "max_cycle_open_ms": cycle_editor},
        io_editors={"1": channel_editor},
        build_io_bindings=Mock(),
        undo_changes=Mock(),
        populate_readonly_values=Mock(),
        update_draft_state=Mock(),
        apply_page_style=Mock(),
        form=Mock(),
        status_hint=Mock(),
        error_label=Mock(),
        error_card=Mock(),
        field_error_button=Mock(),
        save_button=Mock(),
    )

    # 使用真实重新读取逻辑，但回填和控件都是非 Qt 替身。
    SystemConfigurationPage.reload_configuration(page)
    assert page.baseline_values == (external if success else baseline)
    if success:
        gain_editor.set_value.assert_called_once_with(1.5)
        channel_editor.set_value.assert_called_once_with(3)
        cycle_editor.set_value.assert_not_called()
        page.status_hint.setText.assert_called_with("已重新读取并保留草稿修改，请核对后保存")
        assert page.read_error_message == ""
    else:
        page.undo_changes.assert_not_called()
        gain_editor.set_value.assert_not_called()
        page.save_button.setEnabled.assert_called_with(False)
        assert page.read_error_message == "无法读取配置文件"
