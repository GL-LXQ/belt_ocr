# 核心流程运行说明

> 当前已实现 TextRecognizer 的批次接收、手动消费和原始结果回传。recognize_batch 当前为每张图片返回空 blocks 作为联调占位结果，App 不自动启动消费者。模型未接入时不可进行真实识别；当前已实现按 Session 触发一次后处理，但不执行跨帧融合算法或正常结果入库，原有超时仍可能触发。

当前版本包含三机测量、跨轮后台收尾、内存机器状态、Session 四态、失败日志清理、重启清理和异常事件记录。
信号统一从 `App.handle_start()` 和 `handle_close()` 进入，尚未接入现场 IO。

海康 MVS 采集已接入 App，文件夹模拟相机已删除。设备配置及底层接口见 [MVS 采集说明](MVS_CAPTURE.md)。任一已配置机器没有相机、未填写序列号或 SDK 加载失败时，记录错误并终止整个程序，已打开的设备会被释放。

## 运行演示

项目仅使用 Python 3.10 及以上版本的标准库，没有新增第三方依赖。

```powershell
uv run python -X utf8 main.py
```

也可以使用已有虚拟环境：

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py
```

演示向所有可用机器发送启动和关闭信号，再向第一台可用机器发送第二轮信号并等待结果。当前频率使用配置列表产生联调读数，仍需要可用相机；这些读数不代表真实仪器测量。
默认数据库为 `runtime/measurements.sqlite3`，终选图片的预留目录为 `runtime/evidence/<session_id>/`，当前不写入采集图片。
独立运行库默认为 `runtime/measurements.recovery.sqlite3`。
重复执行演示会为可用机器增加新记录。`runtime/` 已加入 Git 忽略列表。

## 配置相机与测量输入

复制并编辑 `config.example.json`，然后指定配置文件：

```powershell
uv run python -X utf8 main.py --config config.example.json
```

所有相对路径以配置文件所在目录为基准。

| 配置 | 含义 |
|---|---|
| `mvs_development_directory` / `mvs_dll_directory` | 官方 Development 目录及可选 DLL 目录 |
| `machines[].camera_serial` | 真实相机序列号；示例留空，设备到货后填写 |
| `machines[].camera_pixel_format` | 可选像素格式，省略时保留设备设置 |
| `machines[].camera_exposure_time_us` / `camera_gain` | 可选手动曝光和增益 |
| `machines[].simulated_frequencies_hz` | 联调频率列表，循环产生新测量；空列表不产生有效读数 |
| `capture_window_ms` | 启动后的最长图像采集时长，默认 1000 毫秒 |
| `camera_queue_capacity` / `camera_timeout_ms` | 每轮帧队列容量与单次等帧超时，默认 32 帧 / 50 毫秒 |
| `max_frames_per_session` | 每轮最多选择的帧数，默认 5；采用先到先选策略 |
| `frequency_interval_ms` | 联调测量间隔，默认 100 毫秒 |
| `ocr_result_timeout_ms` | 从启动到本轮 OCR 完成的期限，默认 30000 毫秒 |
| `max_cycle_open_ms` | 等待正常关闭的最大时长，默认 60000 毫秒 |
| `minimum_frequency_hz` / `maximum_frequency_hz` | 有效频率范围，默认 0.01～10000 Hz |
| `max_pending_sessions_per_machine` | 每台机器未完成记录上限，默认 20 |
| `ocr_queue_capacity` | 当前 OCR 待处理批次队列上限，默认 32 批；本阶段没有消费者 |
| `event_queue_capacity` / `storage_queue_capacity` | 单机事件队列和存储队列上限，默认 128 / 32 |
| `shutdown_timeout_ms` | 正常退出等待后台收尾的上限，默认 10000 毫秒 |
| `recovery_database_path` | 独立运行库路径；省略时使用最终库同目录的 `.recovery.sqlite3` 文件 |
| `maintenance_interval_ms` | 容量检查间隔，默认 250 毫秒 |
| `max_persistent_records` | 正在排队或写入的记录数量达到该值时停止接收新周期，默认 1000 |
| `minimum_free_disk_bytes` | 本地运行库和证据所在磁盘的最低剩余空间，默认 100 MiB |
| `ocr_job_timeout_ms` | 证据文件读取期限 |
| `event_max_age_ms` | 兼容旧配置保留，当前不再用于过滤 START/CLOSE |
| `initial_machine_state` | 新机器的模拟初始状态，默认 `CLOSED`；也支持 `OPEN`、`UNKNOWN` |

相机使用 Continuous / Free Run 模式。图像复制到独立内存后立即归还 SDK Buffer，队满时丢弃新帧并统计；消费者同时处理已入队图片。原始帧由 MVS SDK 转换为内存 BMP，不在采集和批次识别阶段落盘，每轮保存采集和处理统计到 `capture_summary`。
当前按主机收到图像的单调时间校验 START/CLOSE 及窗口边界，不把设备时间戳直接作为主机时间；SDK 停止可能略晚于请求边界，越过业务边界的帧不进入选帧清单。
频率设备采用黑盒接口，当前监听按配置循环产生联调读数，真实协议读取待替换。本次仅清理废弃字段，频率监听和自动启停入口另行处理；删除 `is_simulated` 不代表真实频率协议已经接入。

## 调用业务入口

```python
import asyncio
from pathlib import Path

from configuration import load_configuration
from app import App


async def run_measurement() -> None:
    # 读取配置并初始化应用实例。
    configuration = load_configuration(Path("config.example.json"))
    app = App(configuration)
    await app.start()
    try:
        # 接收一次启动和正常关闭，等待本轮结果保存。
        await app.handle_start(machine_id="M01")
        await asyncio.sleep(1.2)
        await app.handle_close(machine_id="M01")
        await app.wait_until_idle()
    finally:
        await app.stop()


asyncio.run(run_measurement())
```

两个入口返回 `None`；START 等待业务受理，CLOSE 等待本轮停止生产再释放活动位置，不等待证据排空、OCR 或数据库提交。
示例中的 `sleep` 只用于模拟机器运行时间，未来由真实的启动、关闭信号替换。
调用方需要使用应用实例所在的异步事件循环；现场线程接入、信号去抖、边沿识别和通信重连留待适配层实现。
本版假设调用方提供按实际顺序确认的 START/CLOSE，不接受未经确认的电平变化。
无活动周期时重复关闭、活动周期内重复启动不会产生新测量。
仅有 `machine_id` 的入口无法辨别来自硬件的跨周期旧信号，真实接入层必须先确定周期身份。
业务事件使用 `event_type` 区分业务，以 `machine_id` 和 `session_id` 确定归属，保留 `event_id` 和时间；不再包含公共 `source_id`。
系统不按 event_id 持久化去重；START/CLOSE 按接收顺序处理，不按事件创建时间过滤；事件处理保留 Session 归属及采集数据中的设备身份检查，结果提交保留幂等写入。
带旧 Session 的关闭事件会写入异常事件记录，不关闭新周期。

## 业务处理顺序

1. START 创建全局唯一 Session，绑定机器、相机和频率来源。
2. MVS 相机开始连续取流，频率适配器登记本轮接收窗口。
3. 消费者在内存中编码图片，满 8 帧交付一批；窗口到时或提前 CLOSE 后交付尾批并封口。MachineManager 收到批次即直接提交 OCR 队列。
4. App 不自动启动 OCR 消费者；手动启动后按批次读取内存 BMP，结果通过 frame_id 关联 Session 持有的原图。
5. CLOSE 等待本轮 Grabber 停止后释放活动位置；新一轮可以开始，旧一轮继续后台编码和识别。
6. CLOSE 分支立即停止本轮频率接收、封闭列表，取最后收到的一条有效频率，不等待设备继续输出。
7. 正常关闭、OCR 成功、有效频率三项齐全，且证据可读取，才冻结完整结果。
8. SQLite 写入成功并确认后完成；旧轮回调始终不修改新轮活动位置。

频率设备监听接口持续运行，START 只登记窗口，不重新建立连接。
相同数值的新测量拥有不同身份；无活动窗口的测量直接忽略，不会补给下一轮。
测量按程序接收顺序归属，不根据设备测量时间重排；CLOSE 后不补收旧轮数据。

## 保存与异常

`measurements` 每个 `session_id` 只有一条记录。常用字段可直接查询，
`payload_json` 保存完整内容，包括文字行、频率候选、测量身份、帧清单、证据引用、错误码和版本。

```sql
SELECT machine_id, session_id, measurement_frequencies, final_frequency_hz
FROM measurements
ORDER BY start_time;
```

Session 状态统一为 `RUNNING / WAITING_COMMIT_DB / COMMITTED / FAILED`。正常 CLOSE 后仍为 RUNNING，结果与证据齐全后进入 WAITING_COMMIT_DB，数据库确认后进入 COMMITTED。数据库初始化移除旧表的 `outcome`、`is_simulated` 列，新记录不包含这两个键及 `model_version`；历史 JSON 和哈希保持不变。

整轮 OCR、频率或证据失败、中断、存储队列满、写入异常和内容冲突均进入 FAILED，只打印日志并清理资源。关闭前失败保留活动 Session 身份和关闭期限，真实 CLOSE 后再移除；失败不代表实体机器已经停止。局部 OCR 批次失败仍允许其他成功批次进入终选，由整轮状态决定是否失败。

未受理周期只打印日志并等待真实关闭，不再写 rejected_cycles。待提交内容只保存在内存，不持久暂存，不执行单次提交重试或后台自动补交，已删除主动补交入口。正常结果仍按 session_id 和冻结内容进行幂等写入，内容冲突不覆盖历史数据。

如果数据库写入成功但回调未被处理，历史结果仍可能已经存在；Session 的失败日志不能证明数据库里没有记录。

## 本地运行库与重启

本地运行库与最终结果库必须是不同文件；可以通过配置放在不同的可写目录。
本地运行库包含：

| 表 | 内容 |
|---|---|
| `pending_records` | 旧版兼容表，启动时清理，当前不新增待提交记录 |
| `committed_records` | 旧版已提交身份保留，当前幂等检查直接读取最终库 |
| `abnormal_events` | 来源冲突、迟到结果等异常事件；旧 `audit_entries` 数据在启动时迁入此表 |

`Database` 统一管理单实例锁、旧表兼容清理、`abnormal_events` 写入和最终结果提交。旧版待提交记录的暂存、查询、完成登记、延迟重试及计数方法已删除；正常结果仅通过本次运行的内存队列提交最终数据库。

机器状态和未完成 Session 只保留在内存；业务事件不再持久登记去重身份；新库不创建 event_receipts 表，旧库已有的该表保留但不再读写。本地运行库使用 SQLite 事务和 WAL；进程锁禁止两个实例同时操作同一运行库。
每次启动从空的 Session 集合开始，按以下顺序处理：

- 获得运行库独占锁后，在同一事务中迁移旧异常事件表、移除旧版本检查点表并清理全部待提交记录，包括内容冲突和未受理周期记录。
- 清理完成后才检查容量、启动存储及维护任务；不恢复旧 OCR、超时或提交任务。
- 保留最终数据库的历史结果、已提交身份、异常事件和证据图片。
- 初始状态为 CLOSED 时等待新启动；OPEN 或 UNKNOWN 时等待有效关闭或明确的关闭状态同步，再接收下一次启动。

当前模拟实现从 initial_machine_state 配置读取初始状态，尚未直接读取硬件状态。初始运行中或未知时等待关闭或现场状态同步；设备故障则退出程序，修复后手动重启。
退出等待到期时放弃内存中的未完成测量；尚未完成的 Session 标记 FAILED，释放内存与排队请求，不保存待补交记录。已写入最终库但尚未收到确认的历史结果仍保留。
请保留运行库及 SQLite 的配套文件，不要在程序运行时手工删除或只复制其中一个文件。

## 故障退出

任一已配置设备启动失败，或运行期间相机取流、图片编码、频率监听发生异常，程序立即记录错误和原始异常堆栈，停止接收信号并清理全部机器。主流程收到故障后中断等待，资源释放完成后以退出码 1 结束。后台任务意外返回、取消或抛出异常同样退出，不自动重启。

设备故障/恢复事件、report_device_health 接口、storage_retry_interval_ms 和 worker_restart_attempts 配置已删除，旧配置文件需删除这两个配置键。新增设备适配器可调用 `app.report_failure(error, component)`，component 应包含机器和设备身份；线程中通过事件循环的 call_soon_threadsafe 调用。使用 App 的其他主流程也应同时等待 `app.wait_for_failure()`，并在 finally 中调用 `app.stop()`。

正常取帧超时、暂时无频率读数不是设备故障。单轮 OCR 无结果或超时、单次数据库提交失败仍按原规则失败清理，不自动补交。容量不足仍限制新周期；容量恢复后需确认被拒收周期已关闭。初始现场状态同步入口 synchronize_machine 保留，不用于设备故障恢复。

修复设备后重新启动程序，不恢复旧 Session。退出会等待相机线程归还 SDK 资源；SDK 调用本身长期不返回时，资源释放仍可能延后。

## 文件职责

| 文件 | 职责 |
|---|---|
| `main.py` | 串联初始化、模拟启动关闭、结果等待和退出 |
| `app.py` | 信号入口、事件路由和整体任务生命周期 |
| `machine_manager.py` | 每台机器的唯一业务状态修改入口 |
| `models.py` | Session、不可变事件和采集结果 |
| `configuration.py` | 配置读取、绑定及参数检查 |
| `camera.py` | MVS 与 Session 适配、内存图片组批、线程事件桥接和封口 |
| `mvs_sdk.py` | 官方 MVS 绑定、设备管理和独立帧内存复制 |
| `mvs_capture.py` | 固定窗口流式采集、独立有界队列、逐帧回调和统计 |
| `text_recognition.py` | 批次接收、监听、整批模型调用和原始结果回传；模型接口待实现 |
| `frequency_adapter.py` | 持续监听黑盒、当前 Session 归属和读取故障交付 |
| `database.py` | SQLite 建表、实例锁、历史兼容清理、异常事件记录和结果提交 |
| `async_utils.py` | 在线程中安全执行文件、设备和数据库阻塞操作 |
| `tests/test_measurement_flow.py` | 并行、跨轮次、重复、失败和超时测试 |
| `tests/test_recovery_and_faults.py` | 历史恢复与故障测试，部分场景仍基于旧版自动补交规则 |
| `tests/test_acceptance_scenarios.py` | 验收事件注入、跨轮回调、调度公平性、证据失败和强制崩溃测试 |

## 当前边界

App 已接入官方 MVS 相机；IO、频率、图像质量评估和 OCR 融合尚未完成真实设备或算法接入。没有真实相机时仅能执行假 SDK 自动化验证。
现场初始电平、脉冲去抖、设备时间映射和重连基线必须由真实设备适配层提供，不能用模拟结果替代现场验证。
有限多轮及故障注入测试不代表已经完成工控机现场的持续运行和吞吐验收。
最终库写入失败时只打印日志并清理本轮，不暂存补交。路由异常事件写入本地运行库，该库初始化或后台检查异常时退出。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

完整测试需要 pytest。现有业务测试使用临时图片和独立 SQLite 文件，不修改演示数据库；MVS 测试使用假 SDK，不需要连接相机。
README 第 22 节的逐项测试和模拟边界见 [验收测试对照](ACCEPTANCE.md)。

退出时业务等待受 shutdown_timeout_ms 限制，但已持有原始图像的消费线程仍须排空后才能释放 SDK，不能强行销毁它正在使用的设备句柄。证据写盘或 SDK 操作长期不返回时，资源退出也可能延后。

## 当前阶段验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_fatal_shutdown.py tests/test_session_state.py tests/test_frequency_pipeline.py tests/test_text_postprocessing.py tests/test_text_recognition.py tests/test_ocr_batch_delivery.py tests/test_mvs_capture.py tests/test_machine_state.py -q
```

该命令验证满批与尾批交付、队列拒收、封口不重复提交、底层采集和配置。直接依赖旧逐帧模拟识别实现的测试已删除；独立的采集、频率、存储和重启测试保留。其余依赖完整识别结果的历史场景仍待后续接通，当前未宣称全套测试通过。

## 批次识别接口

`recognize_batch(images)` 接收有序 `list[bytes]`，每项为完整 BMP 文件字节，返回等长、同序的 `list[dict]`，每项为 `{"blocks": [...]}`。内存解码、预处理和真实模型调用待实现；当前仅返回独立空 blocks，不代表真实无文字。

调用方手动启动 `listen_and_recognize_batches(app.publish_event)`，并在退出前取消和等待该任务；App 仍不自动管理消费者。取消或超时不会强行释放同步模型正在使用的图片，调用结束后才释放消费者引用。成功事件 `RecognitionBatchCompleted` 的逐图 payload 只含 frame_id 和 blocks，不再含 image_path；机器和周期身份仍在公共事件字段中。失败事件为 `RecognitionBatchFailed`，没有自动重试。

## Session 文字和图片后处理

成功入队的原图由 `Session.images_for_final_selection` 按 frame_id 持有，批次完成后继续保留；批次拒收不登记原图。每轮采集封口且 pending_recognition_batches 归零时，设置 text_postprocessing_started 并调用一次 `select_final_text_and_img(frame_results, frames)`，传入全部成功识别结果和本轮内存图片映射。部分批次失败不阻止其余成功结果进入终选入口。

文字终选、按保留文字信息量和置信度选图以及保存选中图片均待实现。占位函数返回 None 不表示终选完成，也不将 OCR 标记成功；当前采集和 OCR 阶段都不落盘，因此本阶段没有新的图片证据文件。目标规则为无最终文字、整轮 OCR 失败或超时时不保存图片。

整轮失败、超时、中断、结果冻结和退出时释放 Session 持有的原图并移除本轮未消费批次；正在推理的图片由消费者持有至同步调用结束。正常 CLOSE 不提前释放原图，下一周期可独立采集。每轮帧数、待处理周期数及队列容量仍受配置限制；队满丢帧保留统计。断电、进程崩溃和强制退出会丢失未保存的内存图片。

## 频率黑盒与存储

适配器仅保留 active_session_id。MachineManager 在 START 时登记本轮编号，在 CLOSE 时立即清空编号、封闭本轮频率列表并确定最终值，不使用窗口集合、设备收尾接口或额外等待任务。

设备黑盒 listen_measurements 负责连接、有效性判断、测量身份去重、旧缓冲识别和资源释放。它在接收时固定当前周期，按接收顺序立即将 FrequencyMeasured 与 START/CLOSE 交给同一机器 FIFO 队列；无活动周期时不交付数据，已绑定旧周期的数据不得改绑下一周期。

measurement_frequencies 只按接收顺序追加。处理 CLOSE 前已入队的数据先处理，CLOSE 后的旧轮数据不再追加。最终频率取列表最后一条；列表为空、频率读取失败或周期中断时最终频率为空，已有明细保留。设备测量时间仅用于追溯，不重新排序。

measurements 表的 measurement_frequencies 为 JSON 文本列，final_frequency_hz 为最终频率数值；两个字段与 payload_json 一起事务写入。历史库升级保留冻结内容和哈希。废弃的关闭等待、延迟交付和重试配置已删除，旧配置文件需移除对应键后加载。

listen_measurements 当前按 frequency_interval_ms 循环读取 simulated_frequencies_hz，并过滤无读数、非有限值及范围外数值；相同有效值每次生成新的测量身份。真实协议尚未接入。监听异常记录日志并向应用抛出，应用停止全部测量并退出。程序退出只取消并等待持续监听任务，无频率收尾任务。测试使用独立设备替身，不代表设备协议已实现。
