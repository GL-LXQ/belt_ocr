"""验证界面业务结果、参数检查和监测线程生命周期。"""

import os
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from src.controller.controller import AppController, Result
from src.service.abnormal_event_service import AbnormalEventServiceError
from src.service.machine_service import MachineServiceError
from src.service.measurement_record_service import (
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)
from src.system_runtime_thread import SystemRuntimeThread
from ui.main_window import MainWindow


class FakeSystemRuntimeThread(QObject):
    """提供可手动发出监测结束通知的线程替身。"""

    camera_state_changed_signal = Signal(str, str, str)
    measurement_progress_changed_signal = Signal(str, str, str, str)
    cycle_closed_signal = Signal(str, str)
    ocr_result_changed_signal = Signal(str, str, tuple, tuple)
    finished = Signal()

    def __init__(self) -> None:
        """准备停止通知和监测结果。

        Args:
            无外部参数。

        Returns:
            None  # 监测替身已准备
        """
        super().__init__()
        self.stop_requested = Event()
        self.failure_message = ""
        self.started = False
        self.released = False

    def start(self) -> None:
        """记录监测启动。

        Args:
            无外部参数。

        Returns:
            None  # 启动状态已记录
        """
        self.started = True

    def deleteLater(self) -> None:
        """记录线程对象释放请求。

        Args:
            无外部参数。

        Returns:
            None  # 释放请求已记录
        """
        self.released = True


def test_runtime_thread_runs_and_stops_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证后台线程入口按原顺序启动并停止 Runtime。

    Args:
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 配置已加载且 Runtime 已启动和停止
    """
    configuration = object()
    load_configuration = Mock(return_value=configuration)
    system_runtime = Mock(failure=None)
    system_runtime.start = AsyncMock()
    system_runtime.stop = AsyncMock()
    runtime_factory = Mock(return_value=system_runtime)
    monkeypatch.setattr("src.system_runtime_thread.load_config", load_configuration)
    monkeypatch.setattr("src.system_runtime_thread.SystemRuntime", runtime_factory)

    # 提前提交停止请求并直接运行线程入口。
    configuration_directory = Path("config")
    runtime_thread = SystemRuntimeThread(configuration_directory)
    runtime_thread.stop_requested.set()
    runtime_thread.run()

    # 检查配置加载、Runtime 创建和异步资源释放。
    load_configuration.assert_called_once_with(configuration_directory)
    runtime_factory.assert_called_once_with(configuration)
    system_runtime.start.assert_awaited_once()
    system_runtime.stop.assert_awaited_once()
    assert runtime_thread.failure_message == ""


@pytest.fixture
def controller_services() -> tuple[AppController, Mock, Mock, Mock]:
    """创建三个业务服务和统一控制器。

    Args:
        无外部参数。

    Returns:
        (
            AppController(...),  # 界面控制器
            Mock(),  # 机器服务
            Mock(),  # 历史服务
            Mock(),  # 异常事件服务
        )
    """
    machine_service = Mock()
    measurement_record_service = Mock()
    abnormal_event_service = Mock()
    controller = AppController(
        machine_service,
        measurement_record_service,
        abnormal_event_service,
        Path("config"),
    )
    return (
        controller,
        machine_service,
        measurement_record_service,
        abnormal_event_service,
    )


@pytest.fixture
def qt_application() -> QApplication:
    """创建窗口测试所需的 Qt 应用。

    Args:
        无外部参数。

    Returns:
        QApplication([])  # 窗口事件测试使用的应用实例
    """
    application = QApplication.instance()
    return application or QApplication([])


def test_result_factories_preserve_public_fields() -> None:
    """验证成功和失败结果保留页面使用的三个字段。

    Args:
        无外部参数。

    Returns:
        None  # 两种结果的成功标志、数据和提示已验证
    """
    # 检查成功结果的三个字段。
    success_result = Result.ok("data")
    assert success_result.success is True
    assert success_result.data == "data"
    assert success_result.message == ""

    # 检查失败结果的三个字段。
    error_result = Result.error("失败", data=True)
    assert error_result.success is False
    assert error_result.data is True
    assert error_result.message == "失败"


def test_machine_calls_return_controller_results(controller_services) -> None:
    """验证机器请求转发及成功、重复和服务故障结果。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        None  # 机器请求均返回统一结果
    """
    controller = controller_services[0]
    machine_service = controller_services[1]
    machines = [{"id": 1, "machine_name": "皮带机"}]
    machine_service.list_machines.return_value = machines
    machine_service.list_enabled_machines.return_value = machines
    machine_service.create_machine.return_value = {
        "success": True,
        "machine_id": 1,
        "field": None,
    }
    machine_service.update_machine.return_value = {
        "success": False,
        "machine_id": 1,
        "field": "machine_name",
        "message": "机器名称已存在，请修改。",
    }

    # 查询和新增请求返回界面使用的数据。
    assert controller.list_machines() == Result.ok(machines)
    assert controller.list_enabled_machines() == Result.ok(machines)
    result = controller.create_machine(" 皮带机 ", " CAM001 ", " FREQ001 ")
    assert result == Result.ok(1)
    machine_service.create_machine.assert_called_once_with(
        "皮带机", "CAM001", "FREQ001", True, None
    )

    # 重复字段与预期服务异常转成失败结果。
    result = controller.update_machine(1, "皮带机", "CAM002", "FREQ002")
    assert result == Result.error("机器名称已存在，请修改。", data={"field": "machine_name"})
    machine_service.delete_machine.side_effect = MachineServiceError("机器删除失败")
    assert controller.delete_machine(1) == Result.error("机器删除失败")


@pytest.mark.parametrize(
    ("machine_name", "camera_serial", "frequency_meter_serial", "field"),
    (
        (" ", "CAM001", "FREQ001", "machine_name"),
        ("皮带机", " ", "FREQ001", "camera_serial"),
        ("皮带机", "CAM001", " ", "frequency_meter_serial"),
    ),
)
def test_machine_required_fields_are_checked(
    controller_services,
    machine_name: str,
    camera_serial: str,
    frequency_meter_serial: str,
    field: str,
) -> None:
    """验证新增和修改机器时拒绝三个必填字段的空白值。

    Args:
        controller_services: 控制器及三个业务服务。
        machine_name: 待提交的机器名称。
        camera_serial: 待提交的相机序列号。
        frequency_meter_serial: 待提交的频率仪序列号。
        field: 预期的空字段名。

    Returns:
        None  # 空白字段被拒绝且机器服务未调用
    """
    controller = controller_services[0]
    machine_service = controller_services[1]
    result = controller.create_machine(
        machine_name, camera_serial, frequency_meter_serial
    )
    assert not result.success
    assert result.data == {"field": field}
    machine_service.create_machine.assert_not_called()

    # 修改机器也拒绝相同的空白字段。
    result = controller.update_machine(
        1, machine_name, camera_serial, frequency_meter_serial
    )
    assert not result.success
    assert result.data == {"field": field}
    machine_service.update_machine.assert_not_called()


def test_history_parameters_and_review_failure(controller_services) -> None:
    """验证历史查询参数和重复复核错误转换。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        None  # 非法参数和已复核记录均返回失败结果
    """
    controller = controller_services[0]
    measurement_record_service = controller_services[2]
    assert not controller.list_measurement_records("invalid").success
    assert not controller.get_measurement_record("  ").success
    assert not controller.complete_measurement_review("  ").success
    measurement_record_service.list_records.assert_not_called()
    measurement_record_service.get_record.assert_not_called()
    measurement_record_service.complete_review.assert_not_called()

    # 合法查询转发筛选条件，重复复核转换为普通失败。
    measurement_record_service.list_records.return_value = [{"session_id": "session-1"}]
    result = controller.list_measurement_records("pending", "1")
    assert result.success
    assert result.data == [{"session_id": "session-1"}]
    measurement_record_service.list_records.assert_called_once_with("pending", "1")
    review_error = MeasurementReviewAlreadyCompletedError("该记录已完成复核。")
    measurement_record_service.complete_review.side_effect = review_error
    result = controller.complete_measurement_review(" session-1 ", None)
    assert result == Result.error("该记录已完成复核。", data=True)
    measurement_record_service.complete_review.assert_called_once_with(
        "session-1", None
    )

    # 普通复核错误不要求界面刷新详情。
    measurement_record_service.complete_review.side_effect = (
        MeasurementRecordServiceError("人工复核保存失败")
    )
    result = controller.complete_measurement_review("session-1", None)
    assert result == Result.error("人工复核保存失败")


def test_history_and_abnormal_service_errors_become_results(
    controller_services,
) -> None:
    """验证历史及异常事件的预期故障返回失败结果。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        None  # 业务异常没有传入界面
    """
    controller = controller_services[0]
    measurement_record_service = controller_services[2]
    abnormal_event_service = controller_services[3]
    measurement_record_service.get_record.side_effect = (
        MeasurementRecordServiceError("历史详情读取失败")
    )
    abnormal_event_service.list_events.side_effect = AbnormalEventServiceError(
        "异常事件读取失败"
    )
    assert controller.get_measurement_record("session-1") == Result.error("历史详情读取失败")
    assert controller.list_abnormal_events(" 1 ", " session-1 ") == Result.error(
        "异常事件读取失败",
    )
    abnormal_event_service.list_events.assert_called_once_with("1", "session-1")


def test_simple_requests_forward_service_data(controller_services) -> None:
    """验证其余查询、复核及删除接口转发服务结果。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        None  # 查询数据与写入结果返回给界面
    """
    controller = controller_services[0]
    machine_service = controller_services[1]
    measurement_record_service = controller_services[2]
    abnormal_event_service = controller_services[3]
    measurement_record_service.list_record_machines.return_value = [{"machine_id": "1"}]
    measurement_record_service.get_record.return_value = {"session_id": "session-1"}
    abnormal_event_service.list_machine_ids.return_value = ["1"]
    abnormal_event_service.list_events.return_value = [{"abnormal_event_id": 3}]
    abnormal_event_service.get_event.return_value = {"abnormal_event_id": 3}

    # 将服务查询数据统一放入成功结果。
    assert controller.list_record_machines().data == [{"machine_id": "1"}]
    assert controller.get_measurement_record(" session-1 ").data == {
        "session_id": "session-1",
    }
    assert controller.list_abnormal_event_machine_ids().data == ["1"]
    assert controller.list_abnormal_events().data == [{"abnormal_event_id": 3}]
    assert controller.get_abnormal_event(3).data == {"abnormal_event_id": 3}
    measurement_record_service.get_record.assert_called_once_with("session-1")

    # 将成功的复核和删除转换为统一结果。
    assert controller.complete_measurement_review(" session-1 ").success
    assert controller.delete_machine(1).success
    measurement_record_service.complete_review.assert_called_once_with(
        "session-1", None
    )
    machine_service.delete_machine.assert_called_once_with(1)


def test_unexpected_service_error_is_not_hidden(controller_services) -> None:
    """验证未知服务错误继续暴露给调用者。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        None  # 未知程序错误没有被转换为业务失败结果
    """
    controller = controller_services[0]
    machine_service = controller_services[1]
    machine_service.list_machines.side_effect = RuntimeError("程序错误")
    with pytest.raises(RuntimeError, match="程序错误"):
        controller.list_machines()


def test_monitoring_lifecycle_and_old_finished_signal(
    controller_services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证启动、停止、结束清理和旧线程结束通知隔离。

    Args:
        controller_services: 控制器及三个业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 当前线程引用只由自己的结束通知清理
    """
    controller = controller_services[0]
    first_thread = FakeSystemRuntimeThread()
    second_thread = FakeSystemRuntimeThread()
    thread_factory = Mock(side_effect=[first_thread, second_thread])
    monkeypatch.setattr("src.controller.controller.SystemRuntimeThread", thread_factory)
    finished_messages = Mock()
    controller.monitoring_finished_signal.connect(finished_messages)

    # 首次启动成功，重复启动不创建第二个线程。
    assert controller.start_monitoring().success
    assert first_thread.started
    assert controller.is_monitoring_running().data is True
    assert controller.start_monitoring() == Result.error("监测正在运行。")
    assert thread_factory.call_count == 1

    # 停止请求交给当前线程，结束后转发故障并释放引用。
    assert controller.stop_monitoring().success
    assert first_thread.stop_requested.is_set()
    first_thread.failure_message = "设备故障"
    first_thread.finished.emit()
    assert first_thread.released
    assert controller.runtime_thread is None
    assert controller.is_monitoring_running().data is False
    finished_messages.assert_called_once_with("设备故障")

    # 新线程启动后，旧线程的迟到结束通知不能清理新引用。
    assert controller.start_monitoring().success
    assert controller.runtime_thread is second_thread
    first_thread.finished.emit()
    assert controller.runtime_thread is second_thread
    assert finished_messages.call_count == 1
    second_thread.finished.emit()
    assert controller.runtime_thread is None
    assert finished_messages.call_count == 2


def test_monitoring_signals_are_forwarded(
    controller_services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证四个后台通知经 Controller 原样转发。

    Args:
        controller_services: 控制器及三个业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 界面只接收 Controller 的四个信号
    """
    controller = controller_services[0]
    runtime_thread = FakeSystemRuntimeThread()
    runtime_factory = Mock(return_value=runtime_thread)
    monkeypatch.setattr(
        "src.controller.controller.SystemRuntimeThread", runtime_factory
    )
    camera_notification = Mock()
    progress_notification = Mock()
    cycle_notification = Mock()
    ocr_notification = Mock()
    controller.camera_state_changed_signal.connect(camera_notification)
    controller.measurement_progress_changed_signal.connect(progress_notification)
    controller.cycle_closed_signal.connect(cycle_notification)
    controller.ocr_result_changed_signal.connect(ocr_notification)

    # 后台通知按原有字段顺序进入 Controller 信号。
    assert controller.start_monitoring().success
    runtime_thread.camera_state_changed_signal.emit("1", "已连接", "")
    runtime_thread.measurement_progress_changed_signal.emit(
        "1", "session-1", "image_capture", "running"
    )
    runtime_thread.cycle_closed_signal.emit("1", "session-1")
    runtime_thread.ocr_result_changed_signal.emit(
        "1", "session-1", ("003",), ("003",)
    )
    camera_notification.assert_called_once_with("1", "已连接", "")
    progress_notification.assert_called_once_with(
        "1", "session-1", "image_capture", "running"
    )
    cycle_notification.assert_called_once_with("1", "session-1")
    ocr_notification.assert_called_once_with("1", "session-1", ("003",), ("003",))


def test_window_closes_after_monitoring_cleanup(
    controller_services,
    monkeypatch: pytest.MonkeyPatch,
    qt_application: QApplication,
) -> None:
    """验证关闭请求等待 Controller 完成监测清理。

    Args:
        controller_services: 控制器及三个业务服务。
        monkeypatch: pytest 提供的属性替换工具。
        qt_application: 窗口测试使用的 Qt 应用。

    Returns:
        None  # 首次关闭等待监测结束，随后窗口关闭
    """
    controller = controller_services[0]
    machine_service = controller_services[1]
    machine_service.list_machines.return_value = []
    machine_service.list_enabled_machines.return_value = []
    runtime_thread = FakeSystemRuntimeThread()
    runtime_factory = Mock(return_value=runtime_thread)
    monkeypatch.setattr(
        "src.controller.controller.SystemRuntimeThread", runtime_factory
    )
    window = MainWindow(controller)
    try:
        # 启动监测后首次关闭只提交停止请求。
        window.show()
        assert controller.start_monitoring().success
        window.close()
        assert window.isVisible()
        assert runtime_thread.stop_requested.is_set()

        # 线程结束并清理引用后窗口完成关闭。
        runtime_thread.finished.emit()
        assert controller.runtime_thread is None
        assert not window.isVisible()
    finally:
        window.close()
        window.deleteLater()
