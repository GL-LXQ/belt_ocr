"""实时监测与设备管理页面的固定演示数据。"""

# 设备管理使用独立的基础信息记录。
DEVICES = (
    {
        "id": 1,
        "machine_name": "1号皮带机",
        "camera_serial": "MV-CA013456",
        "frequency_meter_serial": "FM-1001",
        "enabled": True,
        "created_at": "2026-09-18 10:12:34",
        "updated_at": "2026-09-20 09:21:11",
        "remark": "1号产线检测设备",
    },
    {
        "id": 2,
        "machine_name": "2号皮带机",
        "camera_serial": "MV-CA013457",
        "frequency_meter_serial": "FM-1002",
        "enabled": True,
        "created_at": "2026-09-18 10:15:20",
        "updated_at": "2026-09-19 16:33:05",
        "remark": "2号产线检测设备",
    },
    {
        "id": 3,
        "machine_name": "3号皮带机",
        "camera_serial": "MV-CA013458",
        "frequency_meter_serial": "FM-1003",
        "enabled": False,
        "created_at": "2026-09-18 11:02:18",
        "updated_at": "2026-09-18 11:02:18",
        "remark": "3号产线检测设备",
    },
)

# 三台设备分别展示测量、空闲和等待关闭状态。
MACHINES = (
    {
        "number": 1,
        "status": "运行中",
        "state": "测量中",
        "symbol": "▶",
        "tone": "running",
        "frequency": "12.35 Hz",
        "completed_steps": 2,
        "events": (
            ("14:32:18", "采集到新帧 (frame_id: 320)"),
            ("14:32:15", "频率监听: 12.35 Hz"),
            ("14:32:10", "开始采集图像"),
            ("14:32:09", "START 信号触发，创建新周期"),
        ),
    },
    {
        "number": 2,
        "status": "待机中",
        "state": "空闲",
        "symbol": "Ⅱ",
        "tone": "idle",
        "frequency": "--",
        "completed_steps": 0,
        "events": (
            ("14:28:35", "本轮测量完成"),
            ("14:28:32", "CLOSE 信号触发"),
            ("14:28:30", "字符识别完成 (3 行)"),
            ("14:28:05", "准备字符识别"),
        ),
    },
    {
        "number": 3,
        "status": "等待关闭",
        "state": "等待关闭",
        "symbol": "⌛",
        "tone": "waiting",
        "frequency": "8.72 Hz",
        "completed_steps": 4,
        "events": (
            ("14:31:50", "正在进行字符识别 …"),
            ("14:31:48", "准备字符识别 (5 帧)"),
            ("14:31:20", "采集完成，共 5 帧"),
            ("14:31:20", "CLOSE 信号触发，停止采集"),
        ),
    },
)

# 日志按时间顺序排列，新日志显示在表格末尾。
LOG_ROWS = (
    ("14:32:09", "INFO", "Manager", "机器1 START 信号触发"),
    ("14:32:10", "INFO", "Machine-1", "开始采集图像"),
    ("14:32:15", "INFO", "Frequency", "机器1 频率更新: 12.35 Hz"),
    ("14:32:18", "INFO", "Machine-1", "采集到新帧 (frame_id: 320)"),
)
