"""离屏验证系统配置预览、草稿和导航，不启动运行服务。"""

import builtins
from copy import deepcopy
from functools import partial
import io
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QWidget
import yaml

import config_util
import src.config_util as source_config_util
from src.controller.controller import AppController, Result


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """提供不显示窗口的 Qt 应用。

    Args:
        无。

    Returns:
        QApplication()  # 离屏测试使用的应用
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def configuration_document() -> dict:
    """提供包含空值、零值和超常数值的六段配置。

    Args:
        无。

    Returns:
        {
            "application": {...},  # 应用路径和退出期限
            "camera": {...},  # 相机参数和 SDK 路径
            "ocr": {...},  # OCR 等待期限
            "frequency": {...},  # 频率范围
            "machine": {...},  # 机器周期参数
            "io": {...},  # 串口参数和 DI 映射
        }
    """
    return {
        "application": {
            "database_path": "runtime/business.sqlite3",
            "recovery_database_path": "runtime/recovery.sqlite3",
            "evidence_directory": "evidence",
            "shutdown_timeout_ms": 12001,
        },
        "camera": {
            "mvs_development_directory": "vendor/development",
            "mvs_dll_directory": "vendor/runtime",
            "capture_window_ms": 123456789,
            "camera_timeout_ms": -7,
            "camera_pixel_format": "VendorSpecificPacked12",
            "camera_exposure_time_us": None,
            "camera_gain": 0.0,
            "camera_line_selector": None,
            "camera_line_mode": "Strobe",
            "camera_line_source": None,
            "camera_strobe_enabled": True,
        },
        "ocr": {
            "ocr_lock_wait_timeout_ms": -7,
            "ocr_result_timeout_ms": 3001,
        },
        "frequency": {
            "frequency_interval_ms": 101,
            "minimum_frequency_hz": 0.0123456789,
            "maximum_frequency_hz": 987654321.1234567,
        },
        "machine": {
            "max_cycle_open_ms": 60001,
            "event_queue_capacity": 2147483659,
        },
        "io": {
            "modbus_serial_port": "COM42",
            "modbus_baudrate": 115200,
            "modbus_parity": "E",
            "modbus_stopbits": 2,
            "modbus_bytesize": 7,
            "modbus_unit_id": 17,
            "modbus_timeout_seconds": 3.125,
            "modbus_input_address": 0,
            "modbus_poll_interval_ms": 51,
            "modbus_reconnect_interval_ms": 1001,
            "io_machine_channels": {
                1: 0,
                99: 12,
            },
        },
    }


@pytest.fixture
def configuration_directory(tmp_path: Path, configuration_document: dict) -> Path:
    """将六段测试配置写入临时目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        configuration_document: 测试配置内容。

    Returns:
        Path("/tmp/configuration")  # 包含 config.yaml 的临时目录
    """
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def controller(configuration_directory: Path) -> Mock:
    """提供只有机器查询返回数据的控制器替身。

    Args:
        configuration_directory: 临时配置目录。

    Returns:
        Mock()  # 不运行硬件和业务服务的控制器
    """
    controller = Mock(spec=AppController)
    controller.configuration_directory = configuration_directory
    controller.list_machines.return_value = Result.ok({
        "machines": [
            {
                "id": 1,
                "machine_name": "实际一号皮带机",
            },
            {
                "id": 2,
                "machine_name": "未配置通道的二号机",
            },
        ],
    })
    return controller


@pytest.fixture(autouse=True)
def forbid_runtime_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻止预览测试加载或校验运行配置。

    Args:
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 两种配置模块导入路径均已禁止运行校验
    """
    for configuration_module in (config_util, source_config_util):
        monkeypatch.setattr(
            configuration_module, "load_config", Mock(side_effect=AssertionError("预览不能加载运行配置"))
        )
        monkeypatch.setattr(
            configuration_module.AppConfig,
            "validate",
            Mock(side_effect=AssertionError("预览不能校验运行配置")),
        )


@pytest.fixture
def preview_page(qt_application: QApplication, controller: Mock):
    """创建尚未读取配置的页面并在结束后释放。

    Args:
        qt_application: 离屏 Qt 应用。
        controller: 不执行运行操作的控制器。

    Returns:
        SystemConfigurationPage()  # 尚未加载配置的页面
    """
    from ui.pages.system_configuration_page import SystemConfigurationPage

    page = SystemConfigurationPage(controller)
    yield page
    page.deleteLater()
    qt_application.processEvents()


def test_shell_loads_actual_values_once_without_runtime_validation(
    preview_page,
    configuration_directory: Path,
    controller: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证初次进入才读文件，并保留原始值且不执行运行校验。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        controller: 不执行运行操作的控制器。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 配置只读一次，空值和超常数值完整保留
    """
    import ui.configuration_preview as preview_module

    # 读取预期值并记录页面使用的唯一配置读取入口。
    expected_values = config_util.read_configuration_settings(configuration_directory)
    reader = Mock(wraps=preview_module.read_configuration_settings)
    monkeypatch.setattr(preview_module, "read_configuration_settings", reader)
    assert not preview_page.configuration_loaded
    controller.list_machines.assert_not_called()
    assert not preview_page.save_button.isEnabled()
    assert preview_page.save_button.text() == "保存配置 · 待接入"

    # 页面只读取一次，不改变运行校验不接受的实际配置。
    preview_page.load_configuration_preview()
    preview_page.load_configuration_preview()
    assert preview_page.configuration_loaded
    assert preview_page.error_card.isHidden()
    assert preview_page.baseline_values == expected_values
    assert preview_page.get_draft_values() == expected_values
    assert not preview_page.is_dirty
    assert not preview_page.undo_button.isEnabled()
    reader.assert_called_once_with(configuration_directory)
    assert controller.method_calls == [call.list_machines()]

    # 检查数字输入未截断或钳制现有数值。
    for field_name in ("ocr_lock_wait_timeout_ms", "modbus_baudrate", "maximum_frequency_hz"):
        assert preview_page.editors[field_name].get_value() == expected_values[field_name]
    assert preview_page.editors["camera_exposure_time_us"].get_value() is None
    assert preview_page.editors["camera_gain"].get_value() == 0.0
    assert preview_page.strobe_combo.currentData() is True


@pytest.mark.parametrize(
    ("field_name", "equivalent_text", "changed_text", "changed_value"),
    [
        ("max_cycle_open_ms", "060001", "9000000000", 9000000000),
        ("minimum_frequency_hz", "0.012345678900", "4.75", 4.75),
        ("camera_gain", "0.000", "-2.75", -2.75),
    ],
)
def test_numeric_edits_normalize_dirty_state_and_undo(
    preview_page,
    field_name: str,
    equivalent_text: str,
    changed_text: str,
    changed_value: int | float,
) -> None:
    """验证等价数值文本不产生修改状态，实际修改可撤销。

    Args:
        preview_page: 尚未加载配置的页面。
        field_name: 数值配置键。
        equivalent_text: 与基线相等的数值文本。
        changed_text: 修改后的数值文本。
        changed_value: 修改后的预期数值。

    Returns:
        None  # 数值比较、草稿和撤销状态符合预期
    """
    # 等价输入保持未修改状态。
    preview_page.load_configuration_preview()
    baseline_values = deepcopy(preview_page.baseline_values)
    editor = preview_page.editors[field_name]
    editor.input.setText(equivalent_text)
    assert not preview_page.is_dirty
    assert not preview_page.undo_button.isEnabled()

    # 实际修改仅进入草稿，保存仍不可用。
    editor.input.setText(changed_text)
    assert preview_page.is_dirty
    assert preview_page.undo_button.isEnabled()
    assert not preview_page.save_button.isEnabled()
    assert preview_page.get_draft_values()[field_name] == changed_value
    assert preview_page.baseline_values == baseline_values

    # 点击撤销恢复原值和页面状态。
    preview_page.undo_button.click()
    assert not preview_page.is_dirty
    assert not preview_page.undo_button.isEnabled()
    assert preview_page.get_draft_values() == baseline_values
    assert editor.get_value() == baseline_values[field_name]


@pytest.mark.parametrize(
    ("section_name", "field_name", "stored_text", "edited_text", "expected_value", "expected_type"),
    [
        ("camera", "camera_gain", "80", "80.0", 80.0, float),
        ("io", "modbus_baudrate", "9600", "9600.0", 9600, int),
    ],
)
def test_quoted_numeric_baseline_keeps_string_type_until_user_edits(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
    section_name: str,
    field_name: str,
    stored_text: str,
    edited_text: str,
    expected_value: int | float,
    expected_type: type,
) -> None:
    """验证带引号数值保留字符串，用户转成数值后才标记修改。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 测试配置内容。
        section_name: 配置所在的 YAML 段落。
        field_name: 数值编辑器对应的配置键。
        stored_text: YAML 中保留的字符串值。
        edited_text: 用户填写的不同数值文本。
        expected_value: 编辑后应返回的数值。
        expected_type: 编辑后应返回的数值类型。

    Returns:
        None  # 字符串转数值属于修改，撤销恢复原始字符串
    """
    # 读取带引号的数值字符串，初始草稿不主动纠正类型。
    configuration_document[section_name][field_name] = stored_text
    (configuration_directory / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.load_configuration_preview()
    editor = preview_page.editors[field_name]
    assert preview_page.baseline_values[field_name] == stored_text
    assert type(preview_page.baseline_values[field_name]) is str
    assert editor.get_value() == stored_text
    assert type(editor.get_value()) is str
    assert not preview_page.is_dirty

    # 不同文本被解析成真实数值，类型变化必须开启撤销。
    editor.input.setText(edited_text)
    assert editor.get_value() == expected_value
    assert type(editor.get_value()) is expected_type
    assert type(preview_page.get_draft_values()[field_name]) is expected_type
    assert preview_page.is_dirty
    assert preview_page.undo_button.isEnabled()
    assert type(preview_page.baseline_values[field_name]) is str

    # 撤销精确恢复字符串类型及原来的显示文本。
    preview_page.undo_button.click()
    assert editor.input.text() == stored_text
    assert editor.get_value() == stored_text
    assert type(editor.get_value()) is str
    assert type(preview_page.get_draft_values()[field_name]) is str
    assert not preview_page.is_dirty
    assert not preview_page.undo_button.isEnabled()


@pytest.mark.parametrize("stored_value", [float("inf"), float("-inf"), float("nan"), -17.5, 1e300])
def test_nonfinite_and_out_of_range_numeric_baselines_remain_unchanged(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
    stored_value: float,
) -> None:
    """验证非有限数值和超常数值可原样预览与撤销。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 测试配置内容。
        stored_value: 无需运行校验的实际浮点数配置值。

    Returns:
        None  # 实际数值未被夹取，加载和撤销均保持未修改状态
    """
    # 预览保留运行校验不接受的值，不将其自动转换为默认值。
    configuration_document["camera"]["camera_gain"] = stored_value
    (configuration_directory / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.load_configuration_preview()
    editor = preview_page.editors["camera_gain"]
    assert preview_page.configuration_loaded
    assert repr(editor.get_value()) == repr(stored_value)
    assert type(editor.get_value()) is float
    assert not preview_page.is_dirty

    # 临时数值可编辑，撤销后恢复非有限值或超常值的原始表示。
    editor.input.setText("1.25")
    assert editor.get_value() == 1.25
    assert preview_page.is_dirty
    preview_page.undo_changes()
    assert repr(editor.get_value()) == repr(stored_value)
    assert type(editor.get_value()) is float
    assert not preview_page.is_dirty
    assert not preview_page.undo_button.isEnabled()


@pytest.mark.parametrize("field_name", ["camera_exposure_time_us", "camera_gain"])
def test_nullable_camera_values_distinguish_null_and_zero(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
    field_name: str,
) -> None:
    """验证曝光和增益的沿用设备模式保留空值且与零值区分。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 测试配置内容。
        field_name: 可空的相机数值配置键。

    Returns:
        None  # 空值和零值可切换且撤销后恢复空值
    """
    # 将当前测试字段设为空值后再首次读取。
    configuration_document["camera"][field_name] = None
    (configuration_directory / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.load_configuration_preview()
    editor = preview_page.editors[field_name]
    assert editor.mode_combo.currentData() is None
    assert editor.get_value() is None
    assert not editor.input.isEnabled()

    # 指定零值产生修改，切回沿用设备清除修改。
    editor.mode_combo.setCurrentIndex(editor.mode_combo.findData("value"))
    editor.input.setText("0")
    assert editor.input.isEnabled()
    assert editor.get_value() == 0
    assert preview_page.is_dirty
    editor.mode_combo.setCurrentIndex(editor.mode_combo.findData(None))
    assert editor.get_value() is None
    assert not preview_page.is_dirty

    # 撤销还原可空输入的模式和基线值。
    editor.mode_combo.setCurrentIndex(editor.mode_combo.findData("value"))
    editor.input.setText("8.125")
    assert preview_page.is_dirty
    preview_page.undo_changes()
    assert editor.mode_combo.currentData() is None
    assert editor.get_value() is None
    assert preview_page.get_draft_values()[field_name] is None
    assert not preview_page.is_dirty


def test_strobe_preserves_all_three_values_and_restores_baseline(preview_page) -> None:
    """验证频闪选择分别保存空值、关闭和开启。

    Args:
        preview_page: 尚未加载配置的页面。

    Returns:
        None  # 三态值不互相混淆，撤销恢复配置值
    """
    preview_page.load_configuration_preview()
    for strobe_value in (None, False, True):
        preview_page.strobe_combo.setCurrentIndex(preview_page.strobe_combo.findData(strobe_value))
        assert preview_page.get_draft_values()["camera_strobe_enabled"] is strobe_value
        assert preview_page.is_dirty is (strobe_value is not True)

    preview_page.strobe_combo.setCurrentIndex(preview_page.strobe_combo.findData(False))
    preview_page.undo_changes()
    assert preview_page.strobe_combo.currentData() is True
    assert not preview_page.is_dirty


def test_unrecognized_strobe_value_is_preserved_for_preview(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
) -> None:
    """验证未识别的已有频闪值不会被自动改成布尔值。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 测试配置内容。

    Returns:
        None  # 未识别的配置值仍能预览并完整保留
    """
    configuration_document["camera"]["camera_strobe_enabled"] = "现场待确认"
    (configuration_directory / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.load_configuration_preview()
    assert preview_page.configuration_loaded
    assert preview_page.strobe_combo.currentData() == "现场待确认"
    assert preview_page.get_draft_values()["camera_strobe_enabled"] == "现场待确认"
    assert not preview_page.is_dirty


def test_paths_and_unexposed_camera_fields_remain_readonly(
    preview_page,
    configuration_directory: Path,
    qt_application: QApplication,
) -> None:
    """验证路径可复制，采集窗口和像素格式不进入可编辑字段。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        qt_application: 离屏 Qt 应用。

    Returns:
        None  # 只读信息和复制操作未改变草稿
    """
    # 所有展示路径均为只读，复制内容与展示内容一致。
    preview_page.load_configuration_preview()
    baseline_values = deepcopy(preview_page.baseline_values)
    expected_paths = {
        "configuration_file": configuration_directory / "config.yaml",
        **{field_name: baseline_values[field_name] for field_name in (
            "database_path", "recovery_database_path", "evidence_directory",
            "mvs_development_directory", "mvs_dll_directory",
        )},
    }
    assert set(preview_page.path_inputs) == set(expected_paths)
    for field_name, expected_path in expected_paths.items():
        path_input = preview_page.path_inputs[field_name]
        assert path_input.isReadOnly()
        assert path_input.text() == str(expected_path)
        preview_page.path_copy_buttons[field_name].click()
        assert qt_application.clipboard().text() == str(expected_path)

    # 未开放字段展示配置原值并保留在草稿中。
    for field_name in ("capture_window_ms", "camera_pixel_format"):
        assert field_name not in preview_page.editors
        assert preview_page.readonly_inputs[field_name].isReadOnly()
        assert str(baseline_values[field_name]) in preview_page.readonly_inputs[field_name].text()
    preview_page.editors["ocr_lock_wait_timeout_ms"].input.setText("88")
    for field_name in (
        "capture_window_ms", "camera_pixel_format", "camera_timeout_ms",
        "event_queue_capacity", "modbus_timeout_seconds",
    ):
        assert preview_page.get_draft_values()[field_name] == baseline_values[field_name]
    assert preview_page.baseline_values == baseline_values


def test_optional_path_display_does_not_fill_null_baseline_values(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
) -> None:
    """验证可选路径的展示回退不改变原配置空值。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 测试配置内容。

    Returns:
        None  # 恢复库可展示推导路径，但草稿中的可选路径仍为空
    """
    # 清空可选路径后首次读取配置。
    configuration_document["application"]["recovery_database_path"] = None
    configuration_document["camera"]["mvs_dll_directory"] = None
    (configuration_directory / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.load_configuration_preview()
    baseline_values = preview_page.baseline_values
    recovery_path = baseline_values["database_path"].with_suffix(".recovery.sqlite3")
    assert preview_page.path_inputs["recovery_database_path"].text() == str(recovery_path)

    # 编辑其他参数不填入可选路径的展示默认值。
    preview_page.editors["ocr_lock_wait_timeout_ms"].input.setText("61")
    for field_name in ("recovery_database_path", "mvs_dll_directory"):
        assert preview_page.path_inputs[field_name].isReadOnly()
        assert baseline_values[field_name] is None
        assert preview_page.get_draft_values()[field_name] is None


def test_advanced_toggle_starts_collapsed_and_preserves_draft(preview_page) -> None:
    """验证高级设置默认收起且切换不清除草稿。

    Args:
        preview_page: 尚未加载配置的页面。

    Returns:
        None  # 高级设置展开收起均保留配置草稿
    """
    preview_page.load_configuration_preview()
    assert preview_page.advanced_toggle.isCheckable()
    assert not preview_page.advanced_toggle.isChecked()
    assert preview_page.advanced_body.isHidden()

    # 不显示页面，只检查高级区域自身的显隐标记。
    preview_page.advanced_toggle.click()
    assert preview_page.advanced_toggle.isChecked()
    assert not preview_page.advanced_body.isHidden()
    preview_page.editors["ocr_lock_wait_timeout_ms"].input.setText("82")
    preview_page.advanced_toggle.click()
    assert preview_page.advanced_body.isHidden()
    assert preview_page.get_draft_values()["ocr_lock_wait_timeout_ms"] == 82
    assert preview_page.is_dirty


def test_machine_di_mapping_uses_actual_ids_names_and_zero_based_channels(preview_page) -> None:
    """验证 DI 映射保留机器编号、实际名称、未知机器和零基通道。

    Args:
        preview_page: 尚未加载配置的页面。

    Returns:
        None  # 映射编辑和撤销保持机器编号到 DI 索引的方向
    """
    # 仅展示配置中的映射，机器列表不增加未配置的行。
    preview_page.load_configuration_preview()
    assert set(preview_page.io_editors) == {"1", "99"}
    assert preview_page.io_editors["1"].get_value() == 0
    assert preview_page.io_editors["99"].get_value() == 12
    label_text = "\n".join(label.text() for label in preview_page.findChildren(QLabel))
    assert "实际一号皮带机" in label_text
    assert "99" in label_text

    # DI 输入按整数解析，修改草稿不改写基线嵌套字典。
    preview_page.io_editors["1"].input.setText("00")
    assert not preview_page.is_dirty
    preview_page.io_editors["1"].input.setText("3")
    assert preview_page.is_dirty
    assert preview_page.get_draft_values()["io_machine_channels"] == {"1": 3, "99": 12}
    assert type(preview_page.get_draft_values()["io_machine_channels"]["1"]) is int
    assert preview_page.baseline_values["io_machine_channels"] == {"1": 0, "99": 12}

    # 返回的草稿副本不允许调用者间接修改页面或基线。
    detached_draft = preview_page.get_draft_values()
    detached_draft["io_machine_channels"]["1"] = 123
    assert preview_page.get_draft_values()["io_machine_channels"]["1"] == 3
    preview_page.undo_changes()
    assert preview_page.io_editors["1"].get_value() == 0
    assert not preview_page.is_dirty


def test_empty_di_mapping_does_not_fabricate_bindings(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
) -> None:
    """验证空通道映射不会按机器列表自动生成绑定。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 测试配置内容。

    Returns:
        None  # 空映射仍为空且页面正常加载
    """
    configuration_document["io"]["io_machine_channels"] = {}
    (configuration_directory / "config.yaml").write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.load_configuration_preview()
    assert preview_page.configuration_loaded
    assert preview_page.io_editors == {}
    assert preview_page.get_draft_values()["io_machine_channels"] == {}
    assert not preview_page.is_dirty


@pytest.mark.parametrize("invalid_content", [None, "camera: [", "application: {}\ncamera: {}\n"])
def test_failed_preview_retains_shell_and_can_retry(
    preview_page,
    configuration_directory: Path,
    configuration_document: dict,
    invalid_content: str | None,
) -> None:
    """验证缺失、损坏和缺少路径的文件保留错误页面并允许重试。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        configuration_document: 完整的测试配置。
        invalid_content: 损坏文件内容，空值表示移除文件。

    Returns:
        None  # 错误页面稳定，修复文件后重试读取成功
    """
    # 建立缺失文件或损坏配置。
    configuration_path = configuration_directory / "config.yaml"
    if invalid_content is None:
        configuration_path.unlink()
    else:
        configuration_path.write_text(invalid_content, encoding="utf-8")
    preview_page.load_configuration_preview()
    assert not preview_page.configuration_loaded
    assert not preview_page.error_card.isHidden()
    assert preview_page.error_label.text().strip()
    assert not preview_page.save_button.isEnabled()
    assert not preview_page.undo_button.isEnabled()
    assert not preview_page.is_dirty

    # 修复文件后通过页面重试按钮重新加载。
    configuration_path.write_text(
        yaml.safe_dump(configuration_document, allow_unicode=True), encoding="utf-8"
    )
    preview_page.retry_button.click()
    assert preview_page.configuration_loaded
    assert preview_page.error_card.isHidden()
    assert preview_page.get_draft_values() == config_util.read_configuration_settings(configuration_directory)
    assert not preview_page.is_dirty


def test_edits_undo_and_disabled_save_do_not_write_or_call_runtime(
    preview_page,
    configuration_directory: Path,
    controller: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证编辑、撤销和禁用保存均不写文件或调用运行服务。

    Args:
        preview_page: 尚未加载配置的页面。
        configuration_directory: 临时配置目录。
        controller: 不执行运行操作的控制器。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 配置文件、目录和运行调用均未改变
    """
    # 保存原始文件状态并完成只读加载。
    configuration_path = configuration_directory / "config.yaml"
    original_bytes = configuration_path.read_bytes()
    original_modified_time = configuration_path.stat().st_mtime_ns
    original_paths = set(configuration_directory.rglob("*"))
    preview_page.load_configuration_preview()
    controller.reset_mock()

    # 禁止 Python 文件写入入口和 YAML 序列化入口。
    original_open = builtins.open
    original_io_open = io.open

    def open_readonly(original_function, file, mode="r", *arguments, **keyword_arguments):
        """允许只读打开并立即拒绝写入模式。

        Args:
            original_function: 原文件打开函数。
            file: 文件路径或描述符。
            mode: 文件打开模式。
            arguments: 其余位置参数。
            keyword_arguments: 其余关键字参数。

        Returns:
            TextIOWrapper()  # 以只读模式打开的文件
        """
        assert not any(flag in mode for flag in "wax+"), f"预览尝试写入文件：{file}"
        return original_function(file, mode, *arguments, **keyword_arguments)

    monkeypatch.setattr(builtins, "open", partial(open_readonly, original_open))
    monkeypatch.setattr(io, "open", partial(open_readonly, original_io_open))
    monkeypatch.setattr(yaml, "safe_dump", Mock(side_effect=AssertionError("预览不能保存 YAML")))
    preview_page.editors["ocr_lock_wait_timeout_ms"].input.setText("66")
    preview_page.io_editors["1"].input.setText("4")
    preview_page.save_button.click()
    assert not preview_page.save_button.isEnabled()
    preview_page.undo_changes()
    preview_page.save_button.click()
    preview_page.load_configuration_preview()

    # 核对文件内容、修改时间和控制器调用均未变化。
    assert configuration_path.read_bytes() == original_bytes
    assert configuration_path.stat().st_mtime_ns == original_modified_time
    assert set(configuration_directory.rglob("*")) == original_paths
    assert controller.method_calls == []


def create_inert_business_page(page_key: str, controller: Mock, parent: QWidget | None = None) -> QWidget:
    """创建导航集成测试使用的无业务占位页。

    Args:
        page_key: 页面导航标识。
        controller: 未调用的控制器替身。
        parent: 页面父控件。

    Returns:
        QWidget()  # 只包含导航标识的测试页面
    """
    page = QWidget(parent)
    page.setObjectName(page_key)
    return page


def test_main_window_navigation_reuses_settings_page_and_keeps_draft(
    qt_application: QApplication,
    controller: Mock,
    configuration_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证主窗口首次进入读取配置，往返导航保留同一页面草稿。

    Args:
        qt_application: 离屏 Qt 应用。
        controller: 不执行运行操作的控制器。
        configuration_directory: 临时配置目录。
        monkeypatch: pytest 提供的替换工具。

    Returns:
        None  # 系统配置接入导航且不重读、不保存、不调用运行服务
    """
    import ui.configuration_preview as preview_module
    import ui.main_window as window_module
    from ui.pages.system_configuration_page import SystemConfigurationPage

    # 替换其他业务页，只测试真实主窗口和系统配置页。
    for class_name, page_key in (
        ("RealtimePage", "realtime"),
        ("HistoryPage", "history"),
        ("AbnormalEventsPage", "abnormal_events"),
        ("MachinesPage", "machines"),
    ):
        monkeypatch.setattr(window_module, class_name, partial(create_inert_business_page, page_key))
    reader = Mock(wraps=preview_module.read_configuration_settings)
    monkeypatch.setattr(preview_module, "read_configuration_settings", reader)
    configuration_path = configuration_directory / "config.yaml"
    original_bytes = configuration_path.read_bytes()
    window = window_module.MainWindow(controller)

    try:
        # 构造窗口不读取配置，首次进入读取且展示正式页面。
        settings_page = window.settings_page
        assert isinstance(settings_page, SystemConfigurationPage)
        assert window.pages["settings"] is settings_page
        assert window.stackedWidget.count() == 7
        reader.assert_not_called()
        window.switch_page("settings")
        qt_application.processEvents()
        assert window.current_page_key == "settings"
        assert window.windowTitle() == "BeltVision | 系统配置"
        assert settings_page.configuration_loaded

        # 切走再返回时保留同一实例和修改后的草稿。
        settings_page.editors["ocr_lock_wait_timeout_ms"].input.setText("79")
        window.switch_page("images")
        qt_application.processEvents()
        window.switch_page("settings")
        qt_application.processEvents()
        assert window.pages["settings"] is settings_page
        assert window.settings_page is settings_page
        assert settings_page.get_draft_values()["ocr_lock_wait_timeout_ms"] == 79
        assert settings_page.is_dirty
        assert not settings_page.save_button.isEnabled()
        settings_page.save_button.click()
        reader.assert_called_once_with(configuration_directory)
        assert configuration_path.read_bytes() == original_bytes
        assert controller.method_calls == [call.list_machines()]
    finally:
        window.deleteLater()
        qt_application.processEvents()
