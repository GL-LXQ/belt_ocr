"""验证皮带机动画控制接口和机器卡片频率显示。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.belt_animation import BeltAnimationWidget
from ui.pages.realtime_page import MachineCard


@pytest.fixture(scope="module")
def qt_application() -> QApplication:
    """创建组件测试所需的 Qt 应用实例。

    Args:
        无。

    Returns:
        返回示例：
            QApplication([])  # 供测试组件使用的 Qt 应用
    """
    application = QApplication.instance()
    return application or QApplication([])


def test_animation_control_interfaces(qt_application: QApplication) -> None:
    """验证机器、采集和频率监听控制接口更新动画状态。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 控制接口状态与调用一致
    """
    animation_widget = BeltAnimationWidget()
    animation_widget.start_machine()
    assert animation_widget._machine_state.name == "STARTING"

    animation_widget.start_capture()
    animation_widget.set_frequency_listening(True)
    assert animation_widget.capturing is True
    assert animation_widget.frequency_listening is True

    animation_widget.stop_machine()
    assert animation_widget._machine_state.name == "STOPPING"
    assert animation_widget.capturing is False
    assert animation_widget.frequency_listening is False
    animation_widget.animation_timer.stop()
    animation_widget.extension_animation.stop()
    animation_widget.close()


def test_capture_and_frequency_controls_can_stop_independently(
    qt_application: QApplication,
) -> None:
    """验证采集动画和频率监听可分别关闭。

    Args:
        qt_application: 测试期间保持存活的 Qt 应用实例。

    Returns:
        返回示例：
            None  # 采集和频率监听分别按接口调用关闭
    """
    animation_widget = BeltAnimationWidget()
    animation_widget.start_capture()
    animation_widget.set_frequency_listening(True)
    animation_widget.stop_machine()
    assert animation_widget.capturing is False
    assert animation_widget.frequency_listening is False

    animation_widget.start_capture()
    animation_widget.set_frequency_listening(True)
    animation_widget.stop_capture()
    animation_widget.set_frequency_listening(False)
    assert animation_widget.capturing is False
    assert animation_widget.frequency_listening is False
    animation_widget.animation_timer.stop()
    animation_widget.close()


@pytest.mark.parametrize(
    ("frequency", "expected_text"),
    ((None, "--"), (0.0, "0.0 Hz"), (12.5, "12.5 Hz")),
)
def test_machine_card_formats_frequency_values(
    frequency: float | None,
    expected_text: str,
) -> None:
    """验证卡片区分无频率数据、真实零频率和实际频率。

    Args:
        frequency: 待显示的频率值。
        expected_text: 期望显示的频率文字。

    Returns:
        返回示例：
            None  # 频率标签收到期望文字
    """
    frequency_label = Mock()
    card = SimpleNamespace(frequency_label=frequency_label)
    MachineCard.set_frequency(card, frequency)
    frequency_label.setText.assert_called_once_with(expected_text)


def test_machine_card_sets_and_clears_ocr_result() -> None:
    """验证卡片按行显示并清空 OCR 结果。

    Args:
        无。

    Returns:
        返回示例：
            None  # OCR 标签显示换行结果并可恢复默认文字
    """
    ocr_result_label = Mock()
    card = SimpleNamespace(ocr_result_label=ocr_result_label)
    result_lines = ("2378244 VEGA × 5EPJ1152", "2926 215C", "003")

    MachineCard.set_ocr_result(card, result_lines)
    ocr_result_label.setText.assert_called_once_with(
        "2378244 VEGA × 5EPJ1152\n2926 215C\n003"
    )

    MachineCard.set_ocr_result(card, ())
    ocr_result_label.setText.assert_called_with("--")
    MachineCard.clear_ocr_result(card)
    assert ocr_result_label.setText.call_args_list[-1].args == ("--",)
