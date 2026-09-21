"""实时监测与机器管理页面的固定演示数据。"""

# 机器管理使用独立的基础信息记录。
DEVICES = (
    {
        "id": 1,
        "machine_name": "1号皮带机",
        "camera_serial": "MV-CA013456",
        "frequency_meter_serial": "FM-1001",
        "enabled": True,
        "created_at": "2026-09-18 10:12:34",
        "updated_at": "2026-09-20 09:21:11",
        "remark": "1号产线检测机器",
    },
    {
        "id": 2,
        "machine_name": "2号皮带机",
        "camera_serial": "MV-CA013457",
        "frequency_meter_serial": "FM-1002",
        "enabled": True,
        "created_at": "2026-09-18 10:15:20",
        "updated_at": "2026-09-19 16:33:05",
        "remark": "2号产线检测机器",
    },
    {
        "id": 3,
        "machine_name": "3号皮带机",
        "camera_serial": "MV-CA013458",
        "frequency_meter_serial": "FM-1003",
        "enabled": False,
        "created_at": "2026-09-18 11:02:18",
        "updated_at": "2026-09-18 11:02:18",
        "remark": "3号产线检测机器",
    },
)

# 日志按时间顺序排列，新日志显示在表格末尾。
LOG_ROWS = (
    ("14:32:09", "INFO", "Manager", "机器1 START 信号触发"),
    ("14:32:10", "INFO", "Machine-1", "开始采集图像"),
    ("14:32:15", "INFO", "Frequency", "机器1 频率更新: 12.35 Hz"),
    ("14:32:18", "INFO", "Machine-1", "采集到新帧 (frame_id: 320)"),
)
