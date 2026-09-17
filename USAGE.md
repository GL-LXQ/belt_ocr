# 核心流程运行说明

当前版本包含三机测量、跨轮后台收尾、运行检查点、本次运行内自动补交、重启清理和异常审计。
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
独立恢复库默认为 `runtime/measurements.recovery.sqlite3`。
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
| `simulated_ocr_delay_ms` | 每帧每次模拟识别耗时；示例为 800 毫秒 |
| `frequency_interval_ms` | 仪器产生一次新测量的间隔，默认 100 毫秒 |
| `frequency_delivery_delay_ms` | 测量产生到事件送达的模拟延迟；示例为 50 毫秒 |
| `frequency_drain_timeout_ms` | 关闭后等待已归属在途测量的上限，默认 2000 毫秒 |
| `ocr_result_timeout_ms` | 从启动到本轮 OCR 完成的期限，默认 30000 毫秒 |
| `max_cycle_open_ms` | 等待正常关闭的最大时长，默认 60000 毫秒 |
| `minimum_frequency_hz` / `maximum_frequency_hz` | 有效频率范围，默认 0.01～10000 Hz |
| `max_pending_sessions_per_machine` | 每台机器未完成记录上限，默认 20 |
| `ocr_queue_capacity` | 全局 OCR 待处理及处理中帧任务总上限，默认 32 |
| `event_queue_capacity` / `storage_queue_capacity` | 单机事件队列和存储队列上限，默认 128 / 32 |
| `storage_retry_attempts` / `storage_retry_delay_ms` | 一次提交的最大尝试次数和间隔，默认 3 次 / 100 毫秒 |
| `shutdown_timeout_ms` | 正常退出等待后台收尾的上限，默认 10000 毫秒 |
| `recovery_database_path` | 独立恢复库路径；省略时使用最终库同目录的 `.recovery.sqlite3` 文件 |
| `storage_retry_interval_ms` | 一批提交失败后的自动补交间隔，默认 1000 毫秒 |
| `maintenance_interval_ms` | 补交和容量检查间隔，默认 250 毫秒 |
| `max_persistent_records` | 待提交记录数量达到该值时停止接收新周期，默认 1000 |
| `minimum_free_disk_bytes` | 恢复库和证据所在磁盘的最低剩余空间，默认 100 MiB |
| `ocr_retry_attempts` / `ocr_job_timeout_ms` | 单帧最大尝试次数和每次处理期限，默认 3 次 / 5000 毫秒 |
| `worker_restart_attempts` | 共享工作单元最多启动次数，默认 3 次 |
| `event_max_age_ms` | START/CLOSE 允许的最大时间偏差，默认 30000 毫秒 |
| `initial_machine_state` | 新机器的模拟初始状态，默认 `CLOSED`；也支持 `OPEN`、`UNKNOWN` |

相机按文件名排序循环读图，每次模拟取帧都会产生新的帧编号和取帧时间。
每轮选中的图片先写入临时文件并同步，再原子发布到独立证据目录。
源文件的修改时间不作为采集时间。
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
仅有 `machine_id` 的入口无法辨别来自硬件的跨周期旧信号，真实接入层必须先确定周期身份。
业务事件支持 `event_id`、`source_id`、`source_epoch`、`source_sequence`、时间及 `session_id`；
这些身份不会因重试而改变。已处理事件身份保存在恢复库，重启后仍会去重。
带旧 Session 的关闭事件、旧来源序号、未经同步的新批次会进入审计，不关闭新周期。

## 业务处理顺序

1. START 创建全局唯一 Session，绑定机器、相机和频率来源。
2. 文件夹相机开始取流，频率适配器登记本轮接收窗口。
3. 图像窗口到时或提前 CLOSE 后封口，先持久登记逐帧任务，再交给共享 OCR。
4. 一个 OCR Worker 按机器轮转，同一机器内按提交顺序处理；每帧有独立任务和尝试编号。
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
未受理事件先进入本地待提交区，再写入 `rejected_cycles`；日志同步记录报警。
OCR 队列满直接将对应轮次标记为待复核，不阻塞其他机器的关闭处理。

数据库写入前，冻结记录先持久保存到恢复库。内存提交队列满不会丢失这份记录。
本次运行中，一批提交失败后状态为 `RETRY_PENDING`，维护任务按间隔自动补交；不必手工触发。项目重启后放弃旧待提交记录。
`await executor.retry_pending_records()` 仍可用于主动重试。
已有相同记录视为成功；内容冲突记录为 `CONFLICT`，保留原内容并停止自动覆盖或重试。
未确认保存成功的记录不会标记完成。最终数据库在程序启动时不可用，也可以启动本地采集与暂存。

## 恢复库与重启

恢复库与最终结果库必须是不同文件；可以通过配置放在不同的可写目录。
恢复库包含：

| 表 | 内容 |
|---|---|
| `machine_checkpoints` | 活动周期、未完成 Session、证据清单、原配置、期限和逐帧处理进度 |
| `pending_records` | 冻结提交内容、下一次重试时间及完整性冲突状态 |
| `committed_records` | 已确认写入最终库的记录身份与内容哈希 |
| `event_receipts` | 已处理的事件编号与内容哈希 |
| `audit_entries` | 来源冲突、重复事件、迟到结果、设备故障等审计内容 |

每个已确认的业务事件都有检查点。恢复库使用 SQLite 事务和 WAL；进程锁禁止两个实例同时操作同一恢复库。
每次启动从空的 Session 集合开始，按以下顺序处理：

- 获得恢复库独占锁后，在同一事务中清理旧检查点和全部待提交记录，包括内容冲突和未受理周期记录。
- 清理完成后才检查容量、启动存储及维护任务；不恢复旧 OCR、超时或提交任务。
- 保留最终数据库的历史结果、已提交身份、事件去重记录、审计和证据图片。
- 初始状态为 CLOSED 时等待新启动；OPEN 或 UNKNOWN 时等待有效关闭或明确的关闭状态同步，再接收下一次启动。

当前模拟实现从 initial_machine_state 配置读取初始状态，尚未直接读取硬件状态。有效关闭仅清除初始状态未知故障，其他设备故障仍按设备恢复流程处理。
退出等待到期时，未完成数据暂留本地；下一次启动将清理旧检查点和待提交记录，不继续处理。已写入最终库但尚未收到确认的历史结果仍保留。
请保留恢复库及 SQLite 的配套文件，不要在程序运行时手工删除或只复制其中一个文件。

## 故障与重新同步

机器状态由当前业务数据派生为 `INITIALIZING`、`READY`、`ACTIVE`、
`WAIT_CYCLE_RESET`、`DEGRADED` 或 `FAULT`。
磁盘空间或待提交容量不足时停止接收新周期；恢复容量后仍需确认被拒收周期已经关闭。

```python
# 报告相机故障及恢复，仅影响绑定机器。
await executor.report_device_health("CAM01", healthy=False)
await executor.report_device_health("CAM01", healthy=True)

# 确认现场已经关闭后恢复接收。
await executor.synchronize_machine("M01", observed_state="CLOSED")

# 确认外部来源重连后的新批次和序号基线。
await executor.synchronize_source("M01", "external-input", "connection-2", 0)
await executor.synchronize_machine("M01", observed_state="CLOSED")
```

`IO`、`OCR`、`STORAGE` 作为共享来源时影响所有机器；也可显式指定 `machine_id`。
设备报告恢复不代表已确认机器关闭，重新同步须使用真实可确认的现场状态。
OCR/存储工作任务意外退出时会记录故障并有限重启；OCR 在途帧保留任务身份继续尝试。
单帧超时和处理失败有有限重试；尝试次数、终态和有序文字行进入最终可追溯记录。

## 文件职责

| 文件 | 职责 |
|---|---|
| `main.py` | 串联初始化、模拟启动关闭、结果等待和退出 |
| `measurement_executor.py` | 信号入口、事件路由和整体任务生命周期 |
| `machine_manager.py` | 每台机器的唯一业务状态修改入口 |
| `models.py` | Session、不可变事件和采集结果 |
| `configuration.py` | 配置读取、绑定及参数检查 |
| `camera.py` | 文件夹模拟取流、图片副本和图像窗口封口 |
| `ocr.py` | 共享有界调度和模拟有序文字行输出 |
| `frequency.py` | 持续模拟新测量、窗口归属和在途数据收尾 |
| `database.py` | SQLite 建表、幂等写入和有限重试 |
| `recovery.py` | 本地运行检查点、待提交记录、事件去重、审计和实例锁 |
| `tests/test_measurement_flow.py` | 并行、跨轮次、重复、失败和超时测试 |
| `tests/test_recovery_and_faults.py` | 异常退出、重启清理、本次运行内自动补交、故障隔离和多轮运行测试 |
| `tests/test_acceptance_scenarios.py` | 验收事件注入、跨轮回调、调度公平性、证据失败和强制崩溃测试 |

## 当前边界

真实 IO/相机/频率协议、实际图像质量评估和 OCR 融合仍按约定保留为模拟实现。
现场初始电平、脉冲去抖、设备时间映射和重连基线必须由真实设备适配层提供，不能用模拟结果替代现场验证。
有限多轮及故障注入测试不代表已经完成工控机现场的持续运行和吞吐验收。
最终库不可用时可暂存；如果本地恢复存储也不可写，则暂停接收并报警，无法承诺保存尚未确认持久化的数据。

## 测试

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

测试使用临时图片和独立 SQLite 文件，不修改演示数据库。
README 第 22 节的逐项测试和模拟边界见 [验收测试对照](ACCEPTANCE.md)。
