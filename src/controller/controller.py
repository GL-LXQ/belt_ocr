"""复用业务服务，为 HTTP 接口提供统一业务结果。"""

from datetime import date
from pathlib import Path

from collections.abc import Callable
from hashlib import sha256
from functools import wraps
from threading import RLock

from src.controller.result import Result

from src.service.abnormal_event_service import (
    AbnormalEventService,
    AbnormalEventServiceError,
)
from src.service.configuration_service import ConfigurationService, ConfigurationServiceError
from src.service.machine_service import (
    MachineDuplicateFieldError,
    MachineService,
    MachineServiceError,
)
from src.service.measurement_record_service import (
    MeasurementRecordService,
    MeasurementRecordServiceError,
    MeasurementReviewAlreadyCompletedError,
)


def serialize_configuration(operation):
    """串行访问配置服务的读取基线与写入过程。

    Args:
        operation: 配置控制器方法。

    Returns:
        callable  # 在同一配置锁内执行的原方法
    """
    @wraps(operation)
    def locked(self, *arguments, **keywords):
        """等待其他配置请求完成后调用业务方法。

        Args:
            self: 当前控制器。
            arguments: 原方法的位置参数。
            keywords: 原方法的命名参数。

        Returns:
            Result(...)  # 原业务方法的结果
        """
        with self.configuration_lock:
            return operation(self, *arguments, **keywords)
    return locked


class AppController:
    """转发 HTTP 业务请求，复用现有服务和统一结果。"""

    def __init__(
        self,
        machine_service: MachineService,
        measurement_record_service: MeasurementRecordService,
        abnormal_event_service: AbnormalEventService,
        configuration_directory: Path,
        is_monitoring_active: Callable[[], bool] | None = None,
    ) -> None:
        """保存业务服务和监测配置目录。

        Args:
            machine_service: 机器业务服务。
            measurement_record_service: 测量记录业务服务。
            abnormal_event_service: 异常事件业务服务。
            configuration_directory: 公共配置目录。
            is_monitoring_active: 返回当前是否正在启动、运行或清理。

        Returns:
            None  # Controller 已初始化，尚无监测任务
        """
        # 保存页面请求所需的业务服务和监测配置。
        self.machine_service = machine_service
        self.measurement_record_service = measurement_record_service
        self.abnormal_event_service = abnormal_event_service
        self.configuration_directory = configuration_directory
        self.configuration_service = ConfigurationService(configuration_directory)
        self.configuration_lock = RLock()
        self.is_monitoring_active = is_monitoring_active or (lambda: False)

    @serialize_configuration
    def read_configuration(self) -> Result:
        """读取系统配置草稿，不启动设备或校验运行条件。

        Args:
            无外部参数。

        Returns:
            返回示例：
                Result(
                    success=True,  # 读取成功
                    data={  # 页面数据
                        "settings": {},  # 实际配置字段
                    },
                    message="",  # 读取提示
                )
        """
        try:
            settings = self.configuration_service.read_configuration()
            return Result.ok({
                "settings": settings,
                "revision": sha256(self.configuration_service.source_bytes).hexdigest(),
            })
        except ConfigurationServiceError as error:
            return Result.error(str(error), data={"field": error.field})

    @serialize_configuration
    def validate_configuration(self, draft: dict) -> Result:
        """使用当前机器记录校验系统配置草稿。

        Args:
            draft: 页面提交的配置草稿。

        Returns:
            返回示例：
                Result(
                    success=False,  # 校验失败
                    data={  # 页面定位信息
                        "field": "camera_gain",  # 待修正的配置字段
                    },
                    message="camera_gain 不能小于零。",  # 可读提示
                )
        """
        # 机器记录每次查询，避免保存已删除或遗漏新启用机器的绑定。
        try:
            machines = self.machine_service.list_machines()["machines"]
            self.configuration_service.validate_configuration(draft, machines)
            return Result.ok()
        except ConfigurationServiceError as error:
            return Result.error(str(error), data={"field": error.field})
        except MachineServiceError as error:
            return Result.error(f"无法校验机器与 DI 绑定：{error}")

    @serialize_configuration
    def save_configuration(self, draft: dict, revision: str | None = None) -> Result:
        """仅在监测任务完全清理后保存配置，供下次启动读取。

        Args:
            draft: 页面提交的配置草稿。
            revision: 页面读取时的配置内容摘要。

        Returns:
            返回示例：
                Result(
                    success=True,  # 配置已落盘
                    data={  # 新的页面基线
                        "settings": {},  # 已保存的配置字段
                    },
                    message="配置已保存，下次开始监测时生效。",  # 生效说明
                )
        """
        # 启动中、运行中和停止清理中都持有任务，均禁止写入。
        if self.is_monitoring_active():
            return Result.error("监测启动、运行或停止清理中，请等待完全停止后再保存配置。")
        try:
            machines = self.machine_service.list_machines()["machines"]
            if revision is not None:
                current_bytes = self.configuration_service.configuration_path.read_bytes()
                if sha256(current_bytes).hexdigest() != revision:
                    return Result.error(
                        "配置已被其他窗口修改，请重新读取后再保存。",
                        {"field": "revision"},
                    )
            settings = self.configuration_service.save_configuration(draft, machines)
            return Result.ok({
                "settings": settings,
                "revision": sha256(self.configuration_service.source_bytes).hexdigest(),
            }, "配置已保存，下次开始监测时生效。")
        except ConfigurationServiceError as error:
            return Result.error(str(error), data={"field": error.field})
        except MachineServiceError as error:
            return Result.error(f"无法校验机器与 DI 绑定：{error}")
        except OSError:
            return Result.error("无法读取配置版本，请重新读取配置。", {"field": "revision"})

    def list_machines(self) -> Result:
        """读取全部未删除的机器。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data={  # 页面业务数据
                    "machines": [],  # 机器记录列表为空
                },
                message="",  # 失败提示
            )
        """
        # 读取机器并转换预期服务故障。
        try:
            machine_data = self.machine_service.list_machines()
            return Result.ok(machine_data)
        except MachineServiceError as error:
            return Result.error(str(error))

    def list_enabled_machines(self) -> Result:
        """读取已启用的机器。

        Args:
            无外部参数。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data={  # 页面业务数据
                    "machines": [],  # 已启用机器列表为空
                },
                message="",  # 失败提示
            )
        """
        # 读取已启用机器并转换预期服务故障。
        try:
            machine_data = self.machine_service.list_enabled_machines()
            return Result.ok(machine_data)
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
                data={  # 页面业务数据
                    "machine_id": 1,  # 新机器编号
                },
                message="",  # 失败提示
            )
            Result(
                success=False,  # 监测运行中禁止修改机器配置
                data=None,  # 拒绝操作没有返回数据
                message="监测运行中，请先停止监测后再修改机器配置。",  # 失败提示
            )
        """
        # 监测任务尚未结束时禁止修改机器配置。
        if self.is_monitoring_active():
            return Result.error("监测运行中，请先停止监测后再修改机器配置。")

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

        # 调用机器服务新增记录。
        try:
            machine_data = self.machine_service.create_machine(
                machine_name, camera_serial, frequency_meter_serial, enabled, remark
            )
        except MachineDuplicateFieldError as error:
            return Result.error(str(error), data={"field": error.field})
        except MachineServiceError as error:
            return Result.error(str(error))

        # 返回已创建机器的业务数据。
        return Result.ok(machine_data)

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
                data={  # 页面业务数据
                    "machine_id": 1,  # 已更新机器编号
                },
                message="",  # 失败提示
            )
            Result(
                success=False,  # 监测运行中禁止修改机器配置
                data=None,  # 拒绝操作没有返回数据
                message="监测运行中，请先停止监测后再修改机器配置。",  # 失败提示
            )
        """
        # 监测任务尚未结束时禁止修改机器配置。
        if self.is_monitoring_active():
            return Result.error("监测运行中，请先停止监测后再修改机器配置。")

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

        # 调用机器服务更新记录。
        try:
            machine_data = self.machine_service.update_machine(
                machine_id,
                machine_name,
                camera_serial,
                frequency_meter_serial,
                enabled,
                remark,
            )
        except MachineDuplicateFieldError as error:
            return Result.error(str(error), data={"field": error.field})
        except MachineServiceError as error:
            return Result.error(str(error))

        # 返回已更新机器的业务数据。
        return Result.ok(machine_data)

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
            Result(
                success=False,  # 监测运行中禁止修改机器配置
                data=None,  # 拒绝操作没有返回数据
                message="监测运行中，请先停止监测后再修改机器配置。",  # 失败提示
            )
        """
        # 监测任务尚未结束时禁止修改机器配置。
        if self.is_monitoring_active():
            return Result.error("监测运行中，请先停止监测后再修改机器配置。")

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
                data={  # 页面业务数据
                    "machines": [],  # 历史机器选项为空
                },
                message="",  # 失败提示
            )
        """
        # 读取历史机器并转换预期服务故障。
        try:
            machine_data = self.measurement_record_service.list_record_machines()
            return Result.ok(machine_data)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def list_measurement_records(
        self,
        review_status: str | None = None,
        machine_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        start_date: date | None = None,
        end_date: date | None = None,
        *,
        text_query: str | None = None,
        text_match_mode: str = "contains",
        text_length: int | None = None,
    ) -> Result:
        """检查筛选参数并读取测量记录。

        Args:
            review_status: None 或 normal、pending、reviewed。
            machine_id: 可选机器编号。
            page: 当前页码，从 1 开始。
            page_size: 每页最多显示的记录数。
            start_date: 可选的本地开始日期。
            end_date: 可选的本地结束日期。
            text_query: 待转交 Service 标准化的查询文字。
            text_match_mode: contains 表示包含，exact 表示整行相等。
            text_length: None 或 20、8、3、2，限制被查询行的完整长度。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data={  # 页面业务数据
                    "records": [],  # 测量记录列表为空
                    "page": 1,  # 当前页码
                    "page_size": 20,  # 每页记录数
                    "total": 0,  # 筛选后的记录总数
                    "total_pages": 1,  # 筛选后的总页数
                },
                message="",  # 失败提示
            )
        """
        if review_status not in (None, "normal", "pending", "reviewed"):
            return Result.error("复核状态无效。")
        if page < 1 or page_size < 1:
            return Result.error("分页参数无效。")
        if start_date is not None and end_date is not None and start_date > end_date:
            return Result.error("开始日期不能晚于结束日期。")

        # 检查文字匹配方式。
        if text_match_mode not in ("contains", "exact"):
            return Result.error("文字匹配方式无效，请选择包含或精确。")

        # 检查被查询行的完整长度选项。
        if text_length not in (None, 20, 8, 3, 2):
            return Result.error("文字位数无效，请选择全部、20 位、8 位、3 位或 2 位。")

        # 读取筛选记录并转换预期服务故障。
        try:
            record_data = self.measurement_record_service.list_records(
                review_status,
                machine_id,
                page,
                page_size,
                start_date,
                end_date,
                text_query=text_query,
                text_match_mode=text_match_mode,
                text_length=text_length,
            )
            return Result.ok(record_data)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def get_today_measurement_summary(self) -> Result:
        """读取今天的已入库识别数量和待复核数量。

        Args:
            无外部参数。

        Returns:
            返回示例：
                Result(
                    success=True,  # 查询是否成功
                    data={  # 今日检测统计
                        "recognition_count": 128,  # 今日全部已入库记录数
                        "pending_review_count": 6,  # 今日未完成复核的记录数
                    },
                    message="",  # 失败提示
                )
        """
        # 读取本地今天的统计并转换预期服务故障。
        try:
            summary_data = self.measurement_record_service.get_daily_summary(
                date.today()
            )
            return Result.ok(summary_data)
        except MeasurementRecordServiceError as error:
            return Result.error(str(error))

    def get_measurement_record(self, session_id: str) -> Result:
        """检查周期编号并读取测量详情。

        Args:
            session_id: 测量周期编号。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data={  # 页面业务数据
                    "record": None,  # 周期编号没有对应记录
                },
                message="",  # 失败提示
            )
        """
        session_id = session_id.strip()
        if not session_id:
            return Result.error("Session ID 不能为空。")

        # 读取测量详情并转换预期服务故障。
        try:
            record_data = self.measurement_record_service.get_record(session_id)
            return Result.ok(record_data)
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
                data={  # 页面业务数据
                    "machine_ids": [],  # 机器编号列表为空
                },
                message="",  # 失败提示
            )
        """
        # 读取异常机器编号并转换预期服务故障。
        try:
            machine_data = self.abnormal_event_service.list_machine_ids()
            return Result.ok(machine_data)
        except AbnormalEventServiceError as error:
            return Result.error(str(error))

    def list_abnormal_events(
        self,
        machine_id: str | None = None,
        session_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        *,
        page: int | None = None,
        page_size: int = 20,
    ) -> Result:
        """整理可选筛选值并读取异常事件。

        Args:
            machine_id: 可选机器编号。
            session_id: 可选完整周期编号。
            start_date: 可选的本地记录开始日期。
            end_date: 可选的本地记录结束日期，包含整天。
            page: 可选的分页页码，省略时兼容原有全部查询。
            page_size: 每页最多显示的事件数量。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data={  # 页面业务数据
                    "events": [],  # 异常事件列表为空
                },
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
            event_data = self.abnormal_event_service.list_events(
                machine_id,
                session_id,
                start_date=start_date,
                end_date=end_date,
                page=page,
                page_size=page_size,
            )
            return Result.ok(event_data)
        except AbnormalEventServiceError as error:
            return Result.error(str(error))

    def get_abnormal_event(self, abnormal_event_id: int) -> Result:
        """读取指定异常事件详情。

        Args:
            abnormal_event_id: 异常事件主键。

        Returns:
            Result(
                success=True,  # 查询是否成功
                data={  # 页面业务数据
                    "event": None,  # 主键没有对应事件
                },
                message="",  # 失败提示
            )
        """
        # 读取异常详情并转换预期服务故障。
        try:
            event_data = self.abnormal_event_service.get_event(abnormal_event_id)
            return Result.ok(event_data)
        except AbnormalEventServiceError as error:
            return Result.error(str(error))
