"""组织设备新增、修改、删除、查询和业务错误转换。"""

import sqlite3

from src.repo.machine_repo import MachineRepo


class MachineServiceError(Exception):
    """表示设备保存、删除或查询时发生的数据库故障。"""


# 设备字段重复时返回给界面的提示文案。
DUPLICATE_FIELD_MESSAGES = {
    "machine_name": "机器名称已存在，请修改。",
    "camera_serial": "相机序列号已被其他设备使用。",
    "frequency_meter_serial": "频率仪序列号已被其他设备使用。",
}


class MachineService:
    """处理设备新增、修改、删除与列表读取。"""

    def __init__(self, machine_repo: MachineRepo):
        """保存设备数据访问对象。

        Args:
            machine_repo: 负责设备表读写的 Repo。

        Returns:
            返回示例：
                None  # 完成设备业务服务初始化
        """
        self.machine_repo = machine_repo

    def create_machine(
        self,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> dict:
        """新增设备并返回成功或重复结果，数据库故障抛出异常。

        Args:
            machine_name: 已去除首尾空白的必填机器名称。
            camera_serial: 已去除首尾空白的必填相机序列号。
            frequency_meter_serial: 已去除首尾空白的必填频率仪序列号。
            enabled: 是否启用，默认启用。
            remark: 设备备注，无备注时为 None。

        Returns:
            返回示例：
                {
                    "success": True,  # 是否创建成功
                    "device_id": 1,  # 已保存设备编号
                    "field": None,  # 成功时没有重复字段
                }
                {
                    "success": False,  # 创建失败
                    "device_id": None,  # 未创建记录
                    "field": "machine_name",  # 重复字段名
                    "message": "机器名称已存在，请修改。",  # 重复提示
                }
        """
        try:
            # 查询表单中的重复字段，新增时不需要排除任何记录。
            duplicate_field = self.machine_repo.find_duplicate_field(machine_name, camera_serial, frequency_meter_serial)

            # 已有相同数据时返回重复字段和提示。
            if duplicate_field:
                return {
                    "success": False,
                    "device_id": None,
                    "field": duplicate_field,
                    "message": DUPLICATE_FIELD_MESSAGES[duplicate_field],
                }

            # 无重复字段时保存设备并取得编号。
            device_id = self.machine_repo.insert(machine_name, camera_serial, frequency_meter_serial, enabled, remark)
        except sqlite3.Error as error:
            raise MachineServiceError(f"设备保存失败：{error}") from error

        # 返回已创建设备的成功结果。
        return {
            "success": True,
            "device_id": device_id,
            "field": None,
        }

    def update_machine(
        self,
        device_id: int,
        machine_name: str,
        camera_serial: str,
        frequency_meter_serial: str,
        enabled: bool = True,
        remark: str | None = None,
    ) -> dict:
        """修改设备并返回成功或重复结果，数据库故障抛出异常。

        Args:
            device_id: 要修改的设备编号。
            machine_name: 已去除首尾空白的必填机器名称。
            camera_serial: 已去除首尾空白的必填相机序列号。
            frequency_meter_serial: 已去除首尾空白的必填频率仪序列号。
            enabled: 是否启用。
            remark: 设备备注，无备注时为 None。

        Returns:
            返回示例：
                {
                    "success": True,  # 是否保存成功
                    "device_id": 1,  # 已保存设备编号
                    "field": None,  # 成功时没有重复字段
                }
                {
                    "success": False,  # 保存失败
                    "device_id": 1,  # 未修改的设备编号
                    "field": "camera_serial",  # 重复字段名
                    "message": "相机序列号已被其他设备使用。",  # 重复提示
                }
        """
        try:
            # 查重时排除正在编辑的设备，原样保留自身字段不算重复。
            duplicate_field = self.machine_repo.find_duplicate_field(
                machine_name, camera_serial, frequency_meter_serial, exclude_id=device_id
            )

            # 与其他未删除设备重复时返回字段和提示。
            if duplicate_field:
                return {
                    "success": False,
                    "device_id": device_id,
                    "field": duplicate_field,
                    "message": DUPLICATE_FIELD_MESSAGES[duplicate_field],
                }

            # 无重复字段时更新设备记录。
            self.machine_repo.update(device_id, machine_name, camera_serial, frequency_meter_serial, enabled, remark)
        except sqlite3.Error as error:
            raise MachineServiceError(f"设备保存失败：{error}") from error

        # 返回已更新设备的成功结果。
        return {
            "success": True,
            "device_id": device_id,
            "field": None,
        }

    def delete_machine(self, device_id: int) -> None:
        """软删除设备并转换数据库故障。

        Args:
            device_id: 要删除的设备编号。

        Returns:
            返回示例：
                None  # 设备已标记删除，列表不再读取该记录
        """
        # 标记删除设备并转换数据库异常。
        try:
            self.machine_repo.soft_delete(device_id)
        except sqlite3.Error as error:
            raise MachineServiceError(f"设备删除失败：{error}") from error

    def list_machines(self) -> list[dict]:
        """按编号读取全部设备信息。

        Args:
            无。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 设备编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 读取设备列表并将数据库异常转换为业务提示。
        try:
            return self.machine_repo.list_all()
        except sqlite3.Error as error:
            raise MachineServiceError(f"设备列表读取失败：{error}") from error

    def list_enabled_machines(self) -> list[dict]:
        """按编号读取全部已启用设备信息。

        Args:
            无。

        Returns:
            返回示例：
                [{
                    "id": 1,  # 设备编号
                    "machine_name": "皮带机",  # 机器名称
                    "camera_serial": "CAM001",  # 相机序列号
                    "frequency_meter_serial": "FREQ001",  # 频率仪序列号
                    "enabled": True,  # 是否启用
                    "created_at": "2026-09-20 08:00:00",  # UTC 创建时间
                    "updated_at": "2026-09-20 08:00:00",  # UTC 修改时间
                    "remark": "",  # 备注，无备注时为空字符串
                }]
        """
        # 读取已启用设备列表并将数据库异常转换为业务提示。
        try:
            return self.machine_repo.list_enabled()
        except sqlite3.Error as error:
            raise MachineServiceError(f"设备列表读取失败：{error}") from error
