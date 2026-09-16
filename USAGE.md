# 核心流程运行说明

当前版本先验证三台机器独立测量、旧轮后台收尾、新轮继续开始，以及正常和异常结果保存。
信号统一从 `MeasurementExecutor.handle_start()` 和 `handle_close()` 进入，尚未接入现场 IO。

## 运行演示

项目仅使用 Python 3.10 及以上版本的标准库，没有新增第三方依赖。

```powershell
uv run python -X utf8 main.py
```

也可以使用已有虚拟环境：

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py
```

演示先同时启动三台机器，关闭第一轮后立即启动 M01 的第二轮，等待四条记录保存后退出。
默认数据库为 `runtime/measurements.sqlite3`，图像副本位于 `runtime/evidence/<session_id>/`。
重复执行演示会增加四条新记录。`runtime/` 已加入 Git 忽略列表。

## 修改模拟输入

复制并编辑 `config.example.json`，然后指定配置文件：

```powershell
uv run python -X utf8 main.py --config config.example.json
```

所有相对路径以配置文件所在目录为基准。

| 配置 | 含义 |
|---|---|
| `machines[].image_directory` | 当前机器的图片文件夹，可以为三台机器设置不同文件夹 |
| `machines[].simulated_lines` | 模拟筛选后的文字行，保留行顺序和重复文字 |
| `machines[].simulated_frequencies_hz` | 连续循环产生的模拟新测量；空列表表示没有有效测量 |
| `capture_window_ms` | 启动后的最长图像采集时长，默认 1000 毫秒 |
| `frame_interval_ms` | 模拟取帧间隔，默认 100 毫秒 |
| `max_frames_per_session` | 每轮最多选择的帧数，默认 5；采用先到先选策略 |
| `simulated_ocr_delay_ms` | 每轮模拟识别耗时；示例为 800 毫秒 |
| `frequency_interval_ms` | 仪器产生一次新测量的间隔，默认 100 毫秒 |
| `frequency_delivery_delay_ms` | 测量产生到事件送达的模拟延迟；示例为 50 毫秒 |
| `frequency_drain_timeout_ms` | 关闭后等待已归属在途测量的上限，默认 2000 毫秒 |
| `ocr_result_timeout_ms` | 从启动到本轮 OCR 完成的期限，默认 30000 毫秒 |
| `max_cycle_open_ms` | 等待正常关闭的最大时长，默认 60000 毫秒 |
| `minimum_frequency_hz` / `maximum_frequency_hz` | 有效频率范围，默认 0.01～10000 Hz |
| `max_pending_sessions_per_machine` | 每台机器未完成记录上限，默认 20 |
| `ocr_queue_capacity` | 全局 OCR 待处理及处理中任务总上限，默认 32 |
| `event_queue_capacity` / `storage_queue_capacity` | 单机事件队列和存储队列上限，默认 128 / 32 |
| `storage_retry_attempts` / `storage_retry_delay_ms` | 一次提交的最大尝试次数和间隔，默认 3 次 / 100 毫秒 |
| `shutdown_timeout_ms` | 正常退出等待后台收尾的上限，默认 10000 毫秒 |

相机按文件名排序循环读图，每次模拟取帧都会产生新的帧编号和取帧时间。
每轮选中的图片复制到独立证据目录；源文件的修改时间不作为采集时间。
支持 PNG、JPG、JPEG、BMP、TIF、TIFF 和 PPM 文件。
目录不存在、没有图片、图片为空或复制失败，都会形成采集异常。

仓库附带的小型 PPM 仅用于演示取流。当前不执行图像解码、清晰度判断、文字检测或跨帧融合，
`simulated_lines` 直接代表这些步骤完成后的输出，并不保证与图片内容一致。
所有数据库记录都有 `is_simulated = 1`，不可作为真实设备测量结果使用。

## 调用业务入口

```python
import asyncio
from pathlib import Path

from configuration import load_configuration
from measurement_executor import MeasurementExecutor


async def run_measurement() -> None:
    # 读取配置并初始化执行器。
    configuration = load_configuration(Path("config.example.json"))
    executor = MeasurementExecutor(configuration)
    await executor.start()
    try:
        # 接收一次启动和正常关闭，等待本轮结果保存。
        await executor.handle_start(machine_id="M01")
        await asyncio.sleep(1.2)
        await executor.handle_close(machine_id="M01")
        await executor.wait_until_idle()
    finally:
        await executor.stop()


asyncio.run(run_measurement())
```

两个入口返回 `None`，仅等待本次信号被业务处理器处理，不等待 OCR 或数据库提交。
示例中的 `sleep` 只用于模拟机器运行时间，未来由真实的启动、关闭信号替换。
调用方需要使用执行器所在的异步事件循环；现场线程接入、信号去抖、边沿识别和通信重连留待适配层实现。
本版假设调用方提供按实际顺序确认的 START/CLOSE，不接受未经确认的电平变化。
无活动周期时重复关闭、活动周期内重复启动不会产生新测量。
仅有 `machine_id` 的入口无法辨别跨周期重放的旧关闭信号，真实接入层必须先完成去重和顺序确认。

## 业务处理顺序

1. START 创建全局唯一 Session，绑定机器、相机和频率来源。
2. 文件夹相机开始取流，频率适配器登记本轮接收窗口。
3. 图像窗口到时或提前 CLOSE 后封口，完整帧清单交给共享 OCR。
4. 一个 OCR Worker 按机器轮转，同一机器内按提交顺序处理。
5. CLOSE 释放当前活动位置；新一轮可以开始，旧一轮继续后台收尾。
6. 频率适配器等待已绑定旧轮的在途读数，再封口并按测量序号取最后一次有效值。
7. 正常关闭、OCR 成功、有效频率三项齐全，且证据可读取，才冻结完整结果。
8. SQLite 写入成功并确认后完成；旧轮回调始终不修改新轮活动位置。

频率模拟器持续运行，不会在每次 START 时重新连接或重置测量序号。
相同数值的新测量拥有不同身份；无活动窗口的测量直接忽略，不会补给下一轮。
传输延迟不延长现场窗口，测量归属在产生时固定。

## 保存与异常

`measurements` 每个 `session_id` 只有一条记录。常用字段可直接查询，
`payload_json` 保存完整内容，包括文字行、频率候选、测量身份、帧清单、证据引用、错误码和版本。

```sql
SELECT machine_id, session_id, outcome, ordered_lines, final_frequency_hz
FROM measurements
ORDER BY start_time;
```

- `COMPLETE`：本轮模拟业务信息完整，正常关闭后保存。
- `REVIEW_REQUIRED`：OCR、频率、采集或证据异常，正常关闭后保存待复核记录。
- `INTERRUPTED`：周期超时或程序主动退出；不伪造正常关闭时间。

达到积压上限或存储不可用时，不受理新的正常测量，并等待该轮明确关闭后重新同步。
未受理事件尝试写入 `rejected_cycles`，存储不可用或队列满时输出报警日志。
OCR 队列满直接将对应轮次标记为待复核，不阻塞其他机器的关闭处理。

数据库写入失败时会有限重试，失败后在进程内保留冻结记录，状态为 `RETRY_PENDING`。
恢复存储后可调用 `await executor.retry_pending_records()`，再等待结果保存。
重试使用同一份内容；已有相同记录视为成功，内容冲突记录错误且不覆盖。
未确认保存成功的记录不会标记完成。

## 文件职责

| 文件 | 职责 |
|---|---|
| `main.py` | 串联初始化、模拟启动关闭、结果等待和退出 |
| `measurement_executor.py` | 信号入口、事件路由和整体任务生命周期 |
| `machine_actor.py` | 每台机器的唯一业务状态修改入口 |
| `models.py` | Session、不可变事件和采集结果 |
| `configuration.py` | 配置读取、绑定及参数检查 |
| `camera.py` | 文件夹模拟取流、图片副本和图像窗口封口 |
| `ocr.py` | 共享有界调度和模拟有序文字行输出 |
| `frequency.py` | 持续模拟新测量、窗口归属和在途数据收尾 |
| `storage.py` | SQLite 建表、幂等写入和有限重试 |
| `tests/test_measurement_flow.py` | 并行、跨轮次、重复、失败和超时测试 |

## 当前边界

本版尚未实现真实 IO/相机/频率协议、真实 OCR、重启恢复和本地持久化待提交区。
退出时会尝试保存中断及已关闭的结果；超出等待期限会记录错误并释放任务。
未提交状态目前仅在进程内，强制终止或持久写入失败后退出可能丢失这部分状态。
后续恢复能力需要独立实现，不能把本次模拟流程视为已经满足 README 全部生产验收条件。

## 测试

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

测试使用临时图片和独立 SQLite 文件，不修改演示数据库。
