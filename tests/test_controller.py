"""验证界面业务结果、参数检查和监测线程生命周期。"""

import os
from datetime import date
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from src.controller.controller import AppController, Result
from src.service.abnormal_event_service import AbnormalEventServiceError
from src.service.machine_service import MachineDuplicateFieldError, MachineServiceError
from src.service.measurement_record_service import (
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)
from src.runtime.system_runtime_thread import SystemRuntimeThread
from ui.main_window import MainWindow


class FakeSystemRuntimeThread(QObject):
    """提供可手动发出监测结束通知的线程替身。"""

    camera_state_changed_signal = Signal(str, str, str)
    measurement_progress_changed_signal = Signal(str, str, str, str)
    cycle_closed_signal = Signal(str, str)
    ocr_result_changed_signal = Signal(str, str, tuple)
    machine_status_changed_signal = Signal(str, str)
    machine_warning_signal = Signal(str, str)
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
    monkeypatch.setattr("src.runtime.system_runtime_thread.load_config", load_configuration)
    monkeypatch.setattr("src.runtime.system_runtime_thread.SystemRuntime", runtime_factory)

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
    measurement_record_service.get_daily_summary.return_value = {
        "recognition_count": 0,
        "pending_review_count": 0,
    }
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
    success_result = Result.ok({"machines": []})
    assert success_result.success is True
    assert success_result.data == {"machines": []}
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
    machine_service.list_machines.return_value = {"machines": machines}
    machine_service.list_enabled_machines.return_value = {"machines": machines}
    machine_service.create_machine.return_value = {"machine_id": 1}
    machine_service.update_machine.side_effect = MachineDuplicateFieldError(
        "machine_name"
    )

    # 查询和新增请求返回界面使用的数据。
    assert controller.list_machines() == Result.ok({"machines": machines})
    assert controller.list_enabled_machines() == Result.ok({"machines": machines})
    result = controller.create_machine(" 皮带机 ", " CAM001 ", " FREQ001 ")
    assert result == Result.ok({"machine_id": 1})
    machine_service.create_machine.assert_called_once_with(
        "皮带机", "CAM001", "FREQ001", True, None
    )

    # 重复字段与预期服务异常转成失败结果。
    result = controller.update_machine(1, "皮带机", "CAM002", "FREQ002")
    assert result == Result.error("机器名称已存在，请修改。", data={"field": "machine_name"})

    # 修改成功时返回原有的机器编号。
    machine_service.update_machine.side_effect = None
    machine_service.update_machine.return_value = {"machine_id": 1}
    assert controller.update_machine(1, "皮带机", "CAM002", "FREQ002") == Result.ok(
        {"machine_id": 1}
    )

    # 删除故障继续返回业务提示。
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


def test_today_measurement_summary_uses_local_date(controller_services) -> None:
    """确认今日统计入口使用本地日期并原样返回服务数据。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        返回示例：
            None  # 当天日期已转发，服务统计已放入成功结果
    """
    # 准备今日识别和待复核统计。
    controller = controller_services[0]
    measurement_record_service = controller_services[2]
    summary_data = {
        "recognition_count": 128,
        "pending_review_count": 6,
    }
    measurement_record_service.get_daily_summary.return_value = summary_data

    # 请求今天的统计并核对日期与返回值。
    target_date = date.today()
    assert controller.get_today_measurement_summary() == Result.ok(summary_data)
    measurement_record_service.get_daily_summary.assert_called_once_with(target_date)


def test_today_summary_converts_service_failure(controller_services) -> None:
    """确认今日统计服务故障转换为界面失败结果。

    Args:
        controller_services: 控制器及三个业务服务。

    Returns:
        返回示例：
            None  # 服务异常已转换，Controller 未修改监测状态
    """
    # 准备今日统计读取故障。
    controller = controller_services[0]
    measurement_record_service = controller_services[2]
    measurement_record_service.get_daily_summary.side_effect = (
        MeasurementRecordServiceError("今日检测统计读取失败。")
    )

    # 核对失败提示和监测线程状态。
    assert controller.get_today_measurement_summary() == Result.error(
        "今日检测统计读取失败。"
    )
    assert controller.runtime_thread is None


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
    assert controller.list_measurement_records(page=0) == Result.error("分页参数无效。")
    assert controller.list_measurement_records(page_size=0) == Result.error("分页参数无效。")
    assert controller.list_measurement_records(
        start_date=date(2026, 9, 29), end_date=date(2026, 9, 28)
    ) == Result.error("开始日期不能晚于结束日期。")
    assert not controller.get_measurement_record("  ").success
    assert not controller.complete_measurement_review("  ").success
    measurement_record_service.list_records.assert_not_called()
    measurement_record_service.get_record.assert_not_called()
    measurement_record_service.complete_review.assert_not_called()

    # 拒绝非法文字选项，保持服务未调用。
    for text_match_mode in ("like", "EXACT", ""):
        result = controller.list_measurement_records(text_match_mode=text_match_mode)
        assert result == Result.error("文字匹配方式无效，请选择包含或精确。")
    for text_length in (0, 4, 7, "8"):
        result = controller.list_measurement_records(text_length=text_length)
        assert result == Result.error(
            "文字位数无效，请选择全部、20 位、8 位、3 位或 2 位。"
        )
    measurement_record_service.list_records.assert_not_called()

    # 合法查询原样转发筛选条件。
    measurement_record_service.list_records.return_value = {
        "records": [{"session_id": "session-1"}],
        "page": 2,
        "page_size": 20,
        "total": 21,
        "total_pages": 2,
    }
    start_date = date(2026, 9, 27)
    end_date = date(2026, 9, 28)
    result = controller.list_measurement_records(
        "pending",
        "1",
        2,
        20,
        start_date=start_date,
        end_date=end_date,
        text_query=" 2926\t215c ",
        text_match_mode="exact",
        text_length=8,
    )
    assert result.success
    assert result.data == measurement_record_service.list_records.return_value
    measurement_record_service.list_records.assert_called_once_with(
        "pending",
        "1",
        2,
        20,
        start_date,
        end_date,
        text_query=" 2926\t215c ",
        text_match_mode="exact",
        text_length=8,
    )

    # 未传文字参数的旧调用继续使用默认选项。
    controller.list_measurement_records()
    measurement_record_service.list_records.assert_called_with(
        None,
        None,
        1,
        20,
        None,
        None,
        text_query=None,
        text_match_mode="contains",
        text_length=None,
    )

    # 重复复核转换为普通失败。
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
    abnormal_event_service.list_events.assert_called_once_with("1", "session-1", start_date=None, end_date=None)


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
    measurement_record_service.list_record_machines.return_value = {
        "machines": [{"machine_id": "1"}],
    }
    measurement_record_service.get_record.return_value = {
        "record": {"session_id": "session-1"},
    }
    abnormal_event_service.list_machine_ids.return_value = {"machine_ids": ["1"]}
    abnormal_event_service.list_events.return_value = {
        "events": [{"abnormal_event_id": 3}],
    }
    abnormal_event_service.get_event.return_value = {
        "event": {"abnormal_event_id": 3},
    }

    # 将服务查询数据统一放入成功结果。
    assert controller.list_record_machines().data == {"machines": [{"machine_id": "1"}]}
    assert controller.get_measurement_record(" session-1 ").data == {
        "record": {
            "session_id": "session-1",
        },
    }
    assert controller.list_abnormal_event_machine_ids().data == {"machine_ids": ["1"]}
    assert controller.list_abnormal_events().data == {
        "events": [{"abnormal_event_id": 3}],
    }
    assert controller.get_abnormal_event(3).data == {"event": {"abnormal_event_id": 3}}
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
    """验证启动、停止、结束清理、旧线程结束通知隔离和监测期间的机器写操作限制。

    Args:
        controller_services: 控制器及三个业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 当前线程引用只由自己的结束通知清理，机器写操作随监测结束恢复
    """
    controller = controller_services[0]
    machine_service = controller_services[1]
    first_thread = FakeSystemRuntimeThread()
    second_thread = FakeSystemRuntimeThread()
    thread_factory = Mock(side_effect=[first_thread, second_thread])
    monkeypatch.setattr("src.controller.controller.SystemRuntimeThread", thread_factory)
    finished_messages = Mock()
    controller.monitoring_finished_signal.connect(finished_messages)
    monitoring_rejected = Result.error("监测运行中，请先停止监测后再修改机器配置。")

    # 首次启动成功，重复启动不创建第二个线程。
    assert controller.start_monitoring().success
    assert first_thread.started
    assert controller.is_monitoring_running().data == {"running": True}
    assert controller.start_monitoring() == Result.error("监测正在运行。")
    assert thread_factory.call_count == 1

    # 监测运行中拒绝机器写操作，机器服务保持不被调用。
    assert controller.create_machine(" 皮带机 ", " CAM001 ", " FREQ001 ") == monitoring_rejected
    assert controller.update_machine(1, "皮带机", "CAM002", "FREQ002") == monitoring_rejected
    assert controller.delete_machine(1) == monitoring_rejected
    machine_service.create_machine.assert_not_called()
    machine_service.update_machine.assert_not_called()
    machine_service.delete_machine.assert_not_called()

    # 停止请求交给当前线程，结束后转发故障并释放引用。
    assert controller.stop_monitoring().success
    assert first_thread.stop_requested.is_set()
    first_thread.failure_message = "设备故障"

    # 线程尚未结束时停止期间同样禁止机器写操作。
    assert controller.delete_machine(1) == monitoring_rejected
    machine_service.delete_machine.assert_not_called()

    first_thread.finished.emit()
    assert first_thread.released
    assert controller.runtime_thread is None
    assert controller.is_monitoring_running().data == {"running": False}
    finished_messages.assert_called_once_with("设备故障")

    # 线程引用清空后机器写操作恢复正常。
    machine_service.create_machine.return_value = {"machine_id": 1}
    assert controller.create_machine("皮带机", "CAM001", "FREQ001") == Result.ok(
        {"machine_id": 1}
    )
    machine_service.create_machine.assert_called_once_with(
        "皮带机", "CAM001", "FREQ001", True, None
    )

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
    """验证六个后台通知经 Controller 原样转发。

    Args:
        controller_services: 控制器及三个业务服务。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        None  # 界面只接收 Controller 的六个信号
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
    machine_status_notification = Mock()
    machine_warning_notification = Mock()
    controller.camera_state_changed_signal.connect(camera_notification)
    controller.measurement_progress_changed_signal.connect(progress_notification)
    controller.cycle_closed_signal.connect(cycle_notification)
    controller.ocr_result_changed_signal.connect(ocr_notification)
    controller.machine_status_changed_signal.connect(machine_status_notification)
    controller.machine_warning_signal.connect(machine_warning_notification)

    # 后台通知按原有字段顺序进入 Controller 信号。
    assert controller.start_monitoring().success
    runtime_thread.camera_state_changed_signal.emit("1", "已连接", "")
    runtime_thread.measurement_progress_changed_signal.emit(
        "1", "session-1", "image_capture", "running"
    )
    runtime_thread.cycle_closed_signal.emit("1", "session-1")
    runtime_thread.ocr_result_changed_signal.emit("1", "session-1", ("003",))
    runtime_thread.machine_status_changed_signal.emit("1", "online")
    runtime_thread.machine_warning_signal.emit("1", "后台处理积压，本次启动未采集，请暂停换带")
    camera_notification.assert_called_once_with("1", "已连接", "")
    progress_notification.assert_called_once_with(
        "1", "session-1", "image_capture", "running"
    )
    cycle_notification.assert_called_once_with("1", "session-1")
    ocr_notification.assert_called_once_with("1", "session-1", ("003",))
    machine_status_notification.assert_called_once_with("1", "online")
    machine_warning_notification.assert_called_once_with(
        "1", "后台处理积压，本次启动未采集，请暂停换带"
    )


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
    machine_service.list_machines.return_value = {"machines": []}
    machine_service.list_enabled_machines.return_value = {"machines": []}
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
