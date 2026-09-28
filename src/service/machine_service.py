"""组织机器新增、修改、删除、查询和业务错误转换。"""

import logging
import sqlite3

from src.repo.machine_repo import MachineRepo


logger = logging.getLogger(__name__)


class MachineServiceError(Exception):
    """表示机器保存、删除或查询时发生的数据库故障。"""


# 机器字段重复时返回给界面的提示文案。
DUPLICATE_FIELD_MESSAGES = {
    "machine_name": "机器名称已存在，请修改。",
    "camera_serial": "相机序列号已被其他机器使用。",
    "frequency_meter_serial": "频率仪序列号已被其他机器使用。",
}


class MachineService:
    """处理机器新增、修改、删除与列表读取。"""

    def __init__(self, machine_repo: MachineRepo):
        """保存机器数据访问对象。

        Args:
            machine_repo: 负责机器表读写的 Repo。

        Returns:
            返回示例：
                None  # 完成机器业务服务初始化
        """
        # 保存机器表访问对象。
        self.machine_repo = machine_repo

    def create_machine(
        self,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> dict:
        """新增机器并返回成功或重复结果，数据库故障抛出异常。

        Args:
            machine_name: 已去除首尾空白的必填机器名称。
            camera_serial: 已去除首尾空白的必填相机序列号。
            frequency_meter_serial: 已去除首尾空白的必填频率仪序列号。
            enabled: 是否启用，默认启用。
            remark: 机器备注，无备注时为 None。

        Returns:
            返回示例：
                {
                    "success": True,  # 是否创建成功
                    "machine_id": 1,  # 已保存机器编号
                    "field": None,  # 成功时没有重复字段
                }
                {
                    "success": False,  # 创建失败
                    "machine_id": None,  # 未创建记录
                    "field": "machine_name",  # 重复字段名
                    "message": "机器名称已存在，请修改。",  # 重复提示
                }
        """
        try:
            # 查询表单中的重复字段，新增时不需要排除任何记录。
            duplicate_field = self.machine_repo.find_duplicate_field(
                machine_name,
                camera_serial,
                frequency_meter_serial,
            )

            # 已有相同数据时返回重复字段和提示。
            if duplicate_field:
                return {
                    "success": False,
                    "machine_id": None,
                    "field": duplicate_field,
                    "message": DUPLICATE_FIELD_MESSAGES[duplicate_field],
                }

            # 无重复字段时保存机器并取得编号。
            machine_id = self.machine_repo.insert(machine_name, camera_serial, frequency_meter_serial, enabled, remark)
        except sqlite3.Error as error:
            # 记录数据库故障详情。
            logger.exception("机器保存失败")

            # 抛出可直接展示的业务提示。
            raise MachineServiceError("机器保存失败。") from error

        # 返回已创建机器的成功结果。
        return {
            "success": True,
            "machine_id": machine_id,
            "field": None,
        }

    def update_machine(
        self,
        machine_id: int,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> dict:
        """修改机器并返回成功或重复结果，数据库故障抛出异常。

        Args:
            machine_id: 要修改的机器编号。
            machine_name: 已去除首尾空白的必填机器名称。
            camera_serial: 已去除首尾空白的必填相机序列号。
            frequency_meter_serial: 已去除首尾空白的必填频率仪序列号。
            enabled: 是否启用。
            remark: 机器备注，无备注时为 None。

        Returns:
            返回示例：
                {
                    "success": True,  # 是否保存成功
                    "machine_id": 1,  # 已保存机器编号
                    "field": None,  # 成功时没有重复字段
                }
                {
                    "success": False,  # 保存失败
                    "machine_id": 1,  # 未修改的机器编号
                    "field": "camera_serial",  # 重复字段名
                    "message": "相机序列号已被其他机器使用。",  # 重复提示
                }
        """
        try:
            # 查重时排除正在编辑的机器，原样保留自身字段不算重复。
            duplicate_field = self.machine_repo.find_duplicate_field(
                machine_name,
                camera_serial,
                frequency_meter_serial,
                exclude_id=machine_id,
            )

            # 与其他未删除机器重复时返回字段和提示。
            if duplicate_field:
                return {
                    "success": False,
                    "machine_id": machine_id,
                    "field": duplicate_field,
                    "message": DUPLICATE_FIELD_MESSAGES[duplicate_field],
                }

            # 无重复字段时更新机器记录。
            self.machine_repo.update(machine_id, machine_name, camera_serial, frequency_meter_serial, enabled, remark)
        except sqlite3.Error as error:
            # 记录数据库故障详情。
            logger.exception("机器保存失败")

            # 抛出可直接展示的业务提示。
            raise MachineServiceError("机器保存失败。") from error

        # 返回已更新机器的成功结果。
        return {
            "success": True,
            "machine_id": machine_id,
            "field": None,
        }

    def delete_machine(self, machine_id: int) -> None:
        """软删除机器并转换数据库故障。

        Args:
            machine_id: 要删除的机器编号。

        Returns:
            返回示例：
                None  # 机器已标记删除，列表不再读取该记录
        """
        # 标记删除机器。
        try:
            self.machine_repo.soft_delete(machine_id)
        except sqlite3.Error as error:
            # 记录数据库故障详情。
            logger.exception("机器删除失败")

            # 抛出可直接展示的业务提示。
            raise MachineServiceError("机器删除失败。") from error

    def list_machines(self) -> list[dict]:
        """按编号读取全部机器信息。

        Args:
            无外部参数。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 机器编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 读取全部机器列表。
        try:
            return self.machine_repo.list_all()
        except sqlite3.Error as error:
            # 记录数据库故障详情。
            logger.exception("机器列表读取失败")

            # 抛出可直接展示的业务提示。
            raise MachineServiceError("机器列表读取失败。") from error

    def list_enabled_machines(self) -> list[dict]:
        """按编号读取全部已启用机器信息。

        Args:
            无外部参数。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 机器编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 读取已启用机器列表。
        try:
            return self.machine_repo.list_enabled()
        except sqlite3.Error as error:
            # 记录数据库故障详情。
            logger.exception("机器列表读取失败")

            # 抛出可直接展示的业务提示。
            raise MachineServiceError("机器列表读取失败。") from error
