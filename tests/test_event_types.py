"""验证事件枚举与历史审计格式兼容。"""

import json

from enums import EventType
from models import MeasurementEvent
from recovery import serialize_value


def test_event_enum_preserves_audit_event_name():
    """验证枚举事件序列化后保留原字符串名称和周期身份。

    Args:
        无外部参数。

    Returns:
        None  # 完成历史事件名称与周期身份的兼容性断言
    """
    # 创建枚举事件并通过审计序列化流程生成 JSON 数据。
    event = MeasurementEvent(EventType.MACHINE_STARTED, "M01", "session-001")
    audit_payload = json.loads(json.dumps(serialize_value(event)))

    # 检查持久化名称、周期身份与原有字符串格式一致。
    assert audit_payload["event_type"] == "MachineStarted"
    assert audit_payload["machine_id"] == "M01"
    assert audit_payload["session_id"] == "session-001"
    assert "acknowledgement" not in audit_payload

    # 将历史字符串转换回枚举，确认可恢复原事件类型。
    assert EventType(audit_payload["event_type"]) is event.event_type
