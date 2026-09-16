"""处理皮带机的测量流程。"""

from datetime import datetime, timezone
from uuid import uuid4

class MeasurementExecutor:
    """管理各台皮带机的活动测量和测量记录。"""

    def __init__(self) -> None:
        """初始化当前执行器在内存中保存的活动测量索引和测量记录。"""
        # 保存每台机器当前尚未停止的测量编号。
        # 键为机器编号 machine_id，值为本轮测量编号 session_id。
        # 每台机器最多对应一轮活动测量，无活动测量时不保留该机器的键。
        self.active_session_ids_by_machine: dict[str, str] = {}

        # 保存当前执行器创建的各轮测量记录。
        # 键为测量编号 session_id，值为该轮测量的详细信息字典。
        # 当前记录包含 session_id、machine_id 和 UTC 启动时间 start_time。
        self.measurement_records: dict[str, dict[str, str | datetime]] = {}

    async def handle_start(self, machine_id: str) -> None:
        """处理某台皮带机启动，创建本轮测量记录。"""
        # 已有活动测量时，忽略本次重复启动。
        if machine_id in self.active_session_ids_by_machine:
            return

        # 生成本轮测量编号，记录机器编号和启动时间。
        session_id = str(uuid4())
        self.measurement_records[session_id] = {
            "session_id": session_id,
            "machine_id": machine_id,
            "start_time": datetime.now(timezone.utc),
        }

        # 将本轮测量设为该机器的活动测量。
        self.active_session_ids_by_machine[machine_id] = session_id
