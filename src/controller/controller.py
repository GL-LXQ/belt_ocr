"""统一接收界面请求并管理后台监测线程。"""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from src.service.abnormal_event_service import (
    AbnormalEventService,
    AbnormalEventServiceError,
)
from src.service.machine_service import MachineService, MachineServiceError
from src.service.measurement_record_service import (
    MeasurementRecordService,
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)
from src.system_runtime_thread import SystemRuntimeThread


@dataclass
class Result:
    """保存界面请求的成功状态、数据和提示。"""

    success: bool
    data: object | None = None
    message: str = ""

    @classmethod
    def ok(cls, data: object | None = None, message: str = "") -> "Result":
        """创建成功结果。

        Args:
            data: 返回给页面的业务数据。
            message: 返回给页面的提示。

        Returns:
            Result(
                success=True,  # 请求成功
                data="data",  # 页面数据
                message="",  # 页面提示
            )
        """
        return cls(
            success=True,
            data=data,
            message=message,
        )

    @classmethod
    def error(cls, message: str, data: object | None = None) -> "Result":
        """创建失败结果。

        Args:
            message: 返回给页面的失败提示。
            data: 返回给页面的附加数据。

        Returns:
            Result(
                success=False,  # 请求失败
                data=True,  # 页面附加数据
                message="失败",  # 失败提示
            )
        """
        return cls(
            success=False,
            data=data,
            message=message,
        )


class AppController(QObject):
    """转发界面业务请求并管理唯一的监测线程。"""

    camera_state_changed_signal = Signal(str, str, str)
    measurement_progress_changed_signal = Signal(str, str, str, str)
    cycle_closed_signal = Signal(str, str)
    ocr_result_changed_signal = Signal(str, str, tuple, tuple)
    monitoring_finished_signal = Signal(str)

    def __init__(
        self,
        machine_service: MachineService,
        measurement_record_service: MeasurementRecordService,
        abnormal_event_service: AbnormalEventService,
        configuration_directory: Path,
    ) -> None:
        """保存业务服务和监测配置目录。

        Args:
            machine_service: 机器业务服务。
            measurement_record_service: 测量记录业务服务。
            abnormal_event_service: 异常事件业务服务。
            configuration_directory: 公共配置目录。

        Returns:
            None  # Controller 已初始化，尚无监测线程
        """
        super().__init__()

        # 保存页面请求所需的业务服务和监测配置。
        self.machine_service = machine_service
        self.measurement_record_service = measurement_record_service
        self.abnormal_event_service = abnormal_event_service
        self.configuration_directory = configuration_directory
        self.runtime_thread: SystemRuntimeThread | None = None

    def list_machines(self) -> Result:
        """读取全部未删除的机器。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=[],  # 机器记录列表为空
                message="",  # 失败提示
            )
        """
        # 读取机器并转换预期服务故障。
        try:
            machines = self.machine_service.list_machines()
            return Result.ok(machines)
        except MachineServiceError as error:
            return Result.error(str(error))

    def list_enabled_machines(self) -> Result:
        """读取已启用的机器。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=[],  # 已启用机器列表为空
                message="",  # 失败提示
            )
        """
        # 读取已启用机器并转换预期服务故障。
        try:
            machines = self.machine_service.list_enabled_machines()
            return Result.ok(machines)
        except MachineServiceError as error:
            return Result.error(str(error))

    def create_machine(
        self,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> Result:
        """检查必填字段并创建机器。

        Args:
            machine_name: 机器名称。
            camera_serial: 相机序列号。
            frequency_meter_serial: 频率仪序列号。
            enabled: 是否启用机器。
            remark: 可选备注。

        Returns:
            Result(
                success=True,  # 是否创建成功
                data=1,  # 新机器编号
                message="",  # 失败提示
            )
        """
        # 清理三个必填字段并返回第一个空字段。
        machine_name = machine_name.strip()
        camera_serial = camera_serial.strip()
        frequency_meter_serial = frequency_meter_serial.strip()
        for field, value, title in (
            ("machine_name", machine_name, "机器名称"),
            ("camera_serial", camera_serial, "相机序列号"),
            ("frequency_meter_serial", frequency_meter_serial, "频率仪序列号"),
        ):
            if not value:
                return Result.error(f"请填写{title}。", data={"field": field})

        # 调用机器服务并转换重复字段或数据库故障。
        try:
            result = self.machine_service.create_machine(
                machine_name, camera_serial, frequency_meter_serial, enabled, remark
            )
        except MachineServiceError as error:
            return Result.error(str(error))
        if not result["success"]:
            return Result.error(result["message"], data={"field": result["field"]})
        return Result.ok(result["machine_id"])

    def update_machine(
        self,
        machine_id: int,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> Result:
        """检查必填字段并更新机器。

        Args:
            machine_id: 待更新机器编号。
            machine_name: 机器名称。
            camera_serial: 相机序列号。
            frequency_meter_serial: 频率仪序列号。
            enabled: 是否启用机器。
            remark: 可选备注。

        Returns:
            Result(
                success=True,  # 是否更新成功
                data=1,  # 已更新机器编号
                message="",  # 失败提示
            )
        """
        # 清理三个必填字段并返回第一个空字段。
        machine_name = machine_name.strip()
        camera_serial = camera_serial.strip()
        frequency_meter_serial = frequency_meter_serial.strip()
        for field, value, title in (
            ("machine_name", machine_name, "机器名称"),
            ("camera_serial", camera_serial, "相机序列号"),
            ("frequency_meter_serial", frequency_meter_serial, "频率仪序列号"),
        ):
            if not value:
                return Result.error(f"请填写{title}。", data={"field": field})

        # 调用机器服务并转换重复字段或数据库故障。
        try:
            result = self.machine_service.update_machine(
                machine_id,
                machine_name,
                camera_serial,
                frequency_meter_serial,
                enabled,
                remark,
            )
        except MachineServiceError as error:
            return Result.error(str(error))
        if not result["success"]:
            return Result.error(result["message"], data={"field": result["field"]})
        return Result.ok(result["machine_id"])

    def delete_machine(self, machine_id: int) -> Result:
        """软删除指定机器。

        Args:
            machine_id: 待删除机器编号。

        Returns:
            Result(
                success=True,  # 是否删除成功
                data=None,  # 删除操作没有返回数据
                message="",  # 失败提示
            )
        """
        # 删除机器并转换预期服务故障。
        try:
            self.machine_service.delete_machine(machine_id)
            return Result.ok()
        except MachineServiceError as error:
            return Result.error(str(error))

    def list_record_machines(self) -> Result:
        """读取历史记录关联的机器。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=[],  # 历史机器选项为空
                message="",  # 失败提示
            )
        """
        # 读取历史机器并转换预期服务故障。
        try:
            machines = self.measurement_record_service.list_record_machines()
            return Result.ok(machines)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def list_measurement_records(
        self, review_status: str | None = None, machine_id: str | None = None
    ) -> Result:
        """检查复核状态并读取测量记录。

        Args:
            review_status: None 或 normal、pending、reviewed。
            machine_id: 可选机器编号。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=[],  # 测量记录列表为空
                message="",  # 失败提示
            )
        """
        if review_status not in (None, "normal", "pending", "reviewed"):
            return Result.error("复核状态无效。")

        # 读取筛选记录并转换预期服务故障。
        try:
            records = self.measurement_record_service.list_records(
                review_status, machine_id
            )
            return Result.ok(records)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def get_measurement_record(self, session_id: str) -> Result:
        """检查周期编号并读取测量详情。

        Args:
            session_id: 测量周期编号。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=None,  # 周期编号没有对应记录
                message="",  # 失败提示
            )
        """
        session_id = session_id.strip()
        if not session_id:
            return Result.error("Session ID 不能为空。")

        # 读取测量详情并转换预期服务故障。
        try:
            record = self.measurement_record_service.get_record(session_id)
            return Result.ok(record)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def complete_measurement_review(
        self, session_id: str, edited_text: str | None = None
    ) -> Result:
        """检查周期编号并完成人工复核。

        Args:
            session_id: 待复核周期编号。
            edited_text: 可选人工编辑文字。

        Returns:
            Result(
                success=True,  # 是否完成复核
                data=None,  # 复核操作没有返回数据
                message="",  # 失败提示
            )
            Result(
                success=False,  # 记录已经完成复核
                data=True,  # 页面需要重新读取当前详情
                message="该记录已完成复核。",  # 失败提示
            )
        """
        session_id = session_id.strip()
        if not session_id:
            return Result.error("Session ID 不能为空。")

        # 完成人工复核并转换预期服务故障。
        try:
            self.measurement_record_service.complete_review(session_id, edited_text)
            return Result.ok()
        except MeasurementReviewAlreadyCompletedError as error:
            return Result.error(str(error), data=True)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def list_abnormal_event_machine_ids(self) -> Result:
        """读取异常事件关联的机器编号。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=[],  # 机器编号列表为空
                message="",  # 失败提示
            )
        """
        # 读取异常机器编号并转换预期服务故障。
        try:
            machine_ids = self.abnormal_event_service.list_machine_ids()
            return Result.ok(machine_ids)
        except AbnormalEventServiceError as error:
            return Result.error(str(error))

    def list_abnormal_events(
        self, machine_id: str | None = None, session_id: str | None = None
    ) -> Result:
        """整理可选筛选值并读取异常事件。

        Args:
            machine_id: 可选机器编号。
            session_id: 可选完整周期编号。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=[],  # 异常事件列表为空
                message="",  # 失败提示
            )
        """
        # 去除可选筛选值两端的空格。
        if machine_id is not None:
            machine_id = machine_id.strip() or None
        if session_id is not None:
            session_id = session_id.strip() or None

        # 读取异常事件并转换预期服务故障。
        try:
            events = self.abnormal_event_service.list_events(machine_id, session_id)
            return Result.ok(events)
        except AbnormalEventServiceError as error:
            return Result.error(str(error))

    def get_abnormal_event(self, abnormal_event_id: int) -> Result:
        """读取指定异常事件详情。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data=None,  # 主键没有对应事件
                message="",  # 失败提示
            )
        """
        # 读取异常详情并转换预期服务故障。
        try:
            event = self.abnormal_event_service.get_event(abnormal_event_id)
            return Result.ok(event)
        except AbnormalEventServiceError as error:
            return Result.error(str(error))

    def start_monitoring(self) -> Result:
        """创建并启动唯一的监测线程。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 是否启动监测
                data=None,  # 启动操作没有返回数据
                message="",  # 重复启动提示
            )
        """
        if self.runtime_thread is not None:
            return Result.error("监测正在运行。")

        # 创建当前监测线程。
        self.runtime_thread = SystemRuntimeThread(self.configuration_directory)

        # 转发相机和测量状态信号。
        self.runtime_thread.camera_state_changed_signal.connect(
            self.camera_state_changed_signal.emit
        )
        self.runtime_thread.measurement_progress_changed_signal.connect(
            self.measurement_progress_changed_signal.emit
        )

        # 转发周期和 OCR 信号。
        self.runtime_thread.cycle_closed_signal.connect(
            self.cycle_closed_signal.emit
        )
        self.runtime_thread.ocr_result_changed_signal.connect(
            self.ocr_result_changed_signal.emit
        )

        # 连接结束回调。
        self.runtime_thread.finished.connect(self.finish_monitoring)

        # 启动监测。
        self.runtime_thread.start()
        return Result.ok()

    def stop_monitoring(self) -> Result:
        """通知当前监测线程停止。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 停止请求已处理
                data=None,  # 停止操作没有返回数据
                message="",  # 提示信息
            )
        """
        if self.runtime_thread is not None:
            self.runtime_thread.stop_requested.set()
        return Result.ok()

    def is_monitoring_running(self) -> Result:
        """查询监测线程是否尚未完成清理。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 状态查询成功
                data=True,  # 仍有监测线程等待清理
                message="",  # 提示信息
            )
        """
        return Result.ok(self.runtime_thread is not None)

    @Slot()
    def finish_monitoring(self) -> None:
        """清理当前结束的线程并转发最终故障信息。

        Args:
            无外部参数。

        Returns:
            None  # 当前线程已清理，监测结束信号已发出
        """
        runtime_thread = self.sender()
        if runtime_thread is not self.runtime_thread:
            return

        # 读取监测结束时的失败信息。
        failure_message = runtime_thread.failure_message

        # 安排线程对象释放并清空当前引用。
        runtime_thread.deleteLater()
        self.runtime_thread = None

        # 在引用清理后通知界面监测已结束。
        self.monitoring_finished_signal.emit(failure_message)
