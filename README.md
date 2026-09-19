# 项目概述与进度

> 本文档是了解项目情况、结构、数据流和进度的唯一入口，内容基于 2026-09-19 的代码实际状态撰写，项目进度从 2026-09-19 起按天汇总。

## 一、项目概述

### 1.1 项目目标

一台工控机同时管理三台皮带机，每台机器绑定自己的海康 MVS 工业相机、启动/关闭输入通道和可唯一识别的频率采集通道。一次"START → 测量 → CLOSE"对应一个 BeltSession：启动信号创建 Session，相机/OCR、频率采集独立工作，关闭信号结束现场采集，数据完整后生成一条可追溯机器、周期、图像和频率归属的正常测量记录。失败、中断和漏采只打印日志，不写入测量数据库。

### 1.2 技术形态

- Python 3.10 及以上，仅使用标准库（asyncio、sqlite3、ctypes），无第三方运行时依赖；pytest 用于测试。
- 单进程多异步任务：每台机器一个 FIFO 事件队列和一个串行 MachineManager；相机采集使用独立线程，不阻塞业务事件循环。
- SQLite 双库：结果库存正常测量记录（幂等写入）；运行库保存异常事件审计并提供进程级实例锁。
- 相机通过海康 MVS 官方 Python 绑定接入（MvImport + Win64 DLL），Continuous 连续取流模式。
- 项目为扁平单目录结构，全部模块与测试直接位于项目根目录，没有分层包。
- 运行方式：`uv run python -X utf8 main.py --config config.example.json`；main.py 当前执行两轮演示启停（全部机器一轮，首台机器再来一轮），频率使用联调读数，需要可用相机。

### 1.3 实际数据流

1. **启动**：`load_configuration` 校验配置 → `App.start` 初始化运行库与结果库、获取实例锁 → 加载 MVS SDK 并按序列号逐台打开相机（任一机器缺序列号或打开失败，整个程序启动失败并释放已打开设备）→ 检查磁盘容量 → 按 `initial_machine_state` 确定机器状态（CLOSED 可直接接收；OPEN/UNKNOWN 等待有效关闭或状态同步）→ 启动各机事件处理、频率监听、共享存储和容量检查任务。
2. **START**（`App.handle_start` → MACHINE_STARTED）：MachineManager 检查可接收性（无活动周期、未等待复位、积压/容量/相机/运行库均正常），创建 RUNNING 状态的 BeltSession，启动固定窗口相机采集（默认 1000 ms），登记频率接收窗口，安排周期超时（默认 60 s）和 OCR 超时（默认 30 s）。
3. **相机采集**：生产线程取帧并复制独立内存 → 消费线程做业务边界检查（起始/截止时间、选帧上限，默认最多 5 帧）→ 帧质量筛选占位（当前全放行）→ 内存编码 BMP → 满 8 帧交付一批 FrameBatchSelected，队满丢帧只统计 → 生产与消费结束后交付尾批并发布 CaptureSealed（含帧数、耗时、丢帧和错误统计）。
4. **OCR**：图片批次进入 TextRecognizer 有界队列（默认 32 批），App 不自动启动消费者；调用方手动运行 `listen_and_recognize_batches`，`recognize_batch` 当前为每张图片返回空 blocks 的联调占位，结果按 frame_id 关联原图并以 RecognitionBatchCompleted/Failed 回传。队列满导致入队被拒或整批识别失败时，本轮识别文字残缺，登记错误后按整轮失败处理，不生成记录；退出阶段停止接收造成的拒收交给退出流程中断。
5. **频率**：FrequencyAdapter 是设备黑盒，`listen_measurements` 当前循环读取 `simulated_frequencies_hz` 产生联调读数（真实协议待接入），接收时固定当前周期，无活动周期不交付；有效测量按接收顺序追加到 `measurement_frequencies`，业务层不重复校验。
6. **CLOSE**（`App.handle_close` → MACHINE_CLOSED，携带当前活动 session_id）：记录 `capture_stop_time` → 封闭频率窗口并取最后一条测量为 `final_frequency`（无读数、故障或中断时最终值为空并置 FAILED）→ 停止相机生产并等待 → 释放活动位置 → 正常关闭后 Session 继续后台收尾；迟到/身份不匹配的关闭事件进入审计，不关闭新周期。
7. **完成检查**（`try_finalize`）：采集封口 + 批次全部结算 + `ocr_state == SUCCESS` + `frequency_state == SUCCESS` + 证据验证通过 → 冻结 payload 并计算 SHA256 → WAITING_COMMIT_DB → 单次幂等写入 SQLite → COMMITTED 并移除档案。写入内容为机器与周期身份、起止时间、`ordered_lines`、`final_frequency_hz`、`measurement_frequencies` 明细、`evidence_refs`、跳帧数、采集统计和配置版本，`payload_json` 是完整冻结内容，表列只是免解析 JSON 的查询副本。任一失败、超时、中断或提交失败 → FAILED，打印日志并清理资源；活动周期失败保留身份，等待真实 CLOSE。
8. **故障与退出**：相机采集、图片编码、频率监听或后台任务异常 → `report_failure` 关闭信号入口、记录机器/设备身份和原始异常堆栈，统一停止全部机器，命令行以退出码 1 结束，不自动重启。正常退出先中断活动周期，在 `shutdown_timeout_ms` 内排空，再释放相机、SDK 和实例锁。

### 1.4 模块与文件清单

| 文件 | 职责 |
|---|---|
| `main.py` | 演示主流程：读取配置、两轮启停演示、故障等待、退出码 |
| `app.py` | 组装各组件，信号入口与事件路由、容量维护、故障统一退出、资源释放 |
| `machine_manager.py` | 每台机器的唯一串行业务处理入口：START/CLOSE/中断/超时/结算/提交回调 |
| `models.py` | MeasurementEvent、CapturedFrame、FrequencyMeasurement、CaptureSummary、BeltSession |
| `enums.py` | MachineState、SessionState、OCRState、FrequencyState、EventType |
| `configuration.py` | 机器与全局配置数据类、校验、JSON 加载和相对路径解析 |
| `camera.py` | Session 相机适配：采集窗口、边界检查、组批、尾批、封口、统计与停止 |
| `mvs_capture.py` | 固定窗口流式采集：控制/生产/消费三线程、有界队列、丢帧统计 |
| `mvs_sdk.py` | MVS 官方绑定封装：SDK 加载、设备枚举与打开、取帧、BMP 内存编码、关闭 |
| `text_recognition.py` | 图片批次队列、整批识别占位、文字与图片终选占位、批次清理 |
| `frequency_adapter.py` | 频率设备黑盒：联调读数产生、当前周期归属 |
| `database.py` | 结果库建表与幂等写入、异常事件审计、实例锁、存储队列 |
| `async_utils.py` | 在线程中执行阻塞操作并防止取消中断资源处理 |

### 1.5 关键配置项

| 配置 | 含义 |
|---|---|
| `machines[].camera_serial` | 真实相机序列号；留空会导致整个程序启动失败 |
| `machines[].camera_pixel_format` / `camera_exposure_time_us` / `camera_gain` | 可选像素格式、手动曝光和增益，省略时保留设备设置 |
| `machines[].simulated_frequencies_hz` | 联调频率列表，循环产生新测量；空列表不产生有效读数 |
| `capture_window_ms` | 启动后的最长图像采集时长，默认 1000 ms |
| `camera_queue_capacity` / `camera_timeout_ms` | 每轮帧队列容量与单次等帧超时，默认 32 帧 / 50 ms |
| `max_frames_per_session` | 每轮最多选择的帧数，默认 5，先到先选 |
| `frequency_interval_ms` | 联调测量间隔，默认 100 ms |
| `ocr_result_timeout_ms` / `max_cycle_open_ms` | 本轮 OCR 完成期限与等待关闭的最大时长，默认 30 s / 60 s |
| `minimum_frequency_hz` / `maximum_frequency_hz` | 有效频率范围，默认 0.01～10000 Hz |
| `max_pending_sessions_per_machine` | 每台机器未完成记录上限，默认 20 |
| `ocr_queue_capacity` | OCR 待处理批次队列上限，默认 32 批；当前没有常驻消费者 |
| `event_queue_capacity` / `storage_queue_capacity` | 单机事件队列和存储队列上限，默认 128 / 32 |
| `shutdown_timeout_ms` | 正常退出等待后台收尾的上限，默认 10000 ms |
| `maintenance_interval_ms` | 容量检查间隔，默认 250 ms |
| `max_persistent_records` | 正在排队或写入的记录达到该值时停止接收新周期，默认 1000 |
| `minimum_free_disk_bytes` | 运行库和证据所在磁盘的最低剩余空间，默认 100 MiB |
| `initial_machine_state` | 新机器的模拟初始状态，默认 CLOSED；也支持 OPEN、UNKNOWN |

## 二、项目进度（按天汇总）

### 2026-09-19

**数据库与运行库简化**

- 去除 recovery.py 中的旧方法（待提交暂存、查询、完成登记、延迟重试等），不再提供持久化补交入口。
- 删除 recovery.py 文件，单实例锁、异常事件审计等职责全部并入 database.py；审计表由 audit_entries 改为 abnormal_events，写入方法统一为 save_abnormal_event()；新增 async_utils.py 在线程中执行阻塞操作。
- 删除旧表迁移逻辑，旧 audit_entries 数据不再迁移。
- 删除三张兼容表：pending_records、committed_records、rejected_cycles，以及 DatabaseRequest 的 record_type 字段。
- 删除其余旧库升级代码：machine_checkpoints 检查点表清理、PRAGMA user_version、outcome/is_simulated 旧列删除、measurement_frequencies 缺列回填。database.py 现仅管理两张业务表：abnormal_events（运行库审计）和 measurements（结果库）。
- 删除 runtime/ 下旧演示数据库文件（measurements.sqlite3、measurements.recovery.sqlite3、.lock），下次运行自动按新结构重建。

**测量记录字段精简**

- 删除 measurements 表的 `error_codes` 与 `final_measurement_id` 两列，payload 同步去掉这两个键：前者只在 OCR 批次拒收、整批识别失败两条不终止周期的降级路径上有值，其余情况恒为空数组；后者恒等于 `measurement_frequencies` 最后一条的 measurement_id，可从已存明细推导。
- 建表、INSERT 与 payload 组装同步收缩，`session.errors` 保留为内存错误清单，继续供失败日志使用。
- 本机没有历史库文件，无需迁移；`error_codes` 是 NOT NULL 且无默认值，若在已有库的机器上重复此改动，需先删除旧列或重建表。
- 文档同步：本文件 1.3 完成检查段落补全记录字段说明。

**OCR 降级路径收敛**

- `FRAME_BATCH_SELECTED` 入队被拒与 `RECOGNITION_BATCH_FAILED` 不再是"只记错误、周期继续"：置 `ocr_state = FAILED` 后进入 `handle_measurement_failure`，与 OCR 失败、采集失败、超时同一出口，本轮不生成记录。
- 拒收按原因分开处理：队列满说明识别服务已经积压，按整轮失败处理；`accepting_batches` 已停止接收只出现在退出阶段，本轮交给退出流程中断，不在 OCR 侧定因（否则正常退出会被记成批次拒收）。
- 两个分支本地重复的 logger.error 删除，失败明细统一由 `handle_measurement_failure` 打印；失败后迟到的批次结果由 `state != RUNNING` 检查丢弃。
- 至此 OCR 侧所有残缺路径都终结本轮，"入库即完整成功"成立。
- 本次未改测试：全量测试由 91 通过 / 40 失败变为 87 通过 / 44 失败，新增的 4 个失败用例断言的都是被推翻的旧行为（test_full_queue_rejects_tail_and_preserves_first_batch、test_failed_batch_does_not_stop_next_batch、test_failed_and_rejected_batches_preserve_successful_results、test_no_usable_text_skips_postprocessing[failed]），待与存量用例一起对齐（见 3.3 第 2 条）。

**文档**

- 同步更新项目规格与运行说明文档：本地数据库数据流段落、启动流程、保存与异常、本地运行库与重启、文件职责表。
- 新建本文档，用于汇总项目概述并按天记录进度。

**测试**

- 删除依赖旧表与旧库迁移的用例（test_startup_drops_old_checkpoint_table、test_old_database_migration_preserves_frozen_record），调整事件去重用例中的 machine_checkpoints 断言。
- 全量测试 91 通过 / 40 失败，失败用例主要因仍引用旧字段与旧行为，待对齐（见 3.2；本次 OCR 降级收敛后为 87 通过 / 44 失败）。

## 三、当前状态与计划

### 3.1 待实现清单

1. **IO 输入**：尚未实现 IOAdapter。START/CLOSE 目前由 `App.handle_start` / `handle_close` 编程入口进入；输入读取、去抖、边沿识别、电平解释、通信健康和现场重连均未实现。
2. **真实频率协议**：`listen_measurements` 仍循环读取 `simulated_frequencies_hz`；真实设备的连接、读数、测量身份去重和旧缓冲识别待接入。
3. **OCR 模型与成功路径（当前关键断点）**：`recognize_batch` 返回空 blocks 占位，且当前没有任何事件把 `ocr_state` 置为 SUCCESS，正常提交链路目前无法端到端完成——联调阶段正常记录实际无法入库。需要在文字终选实现中同时完成"确定保留文字、置 `ocr_state = SUCCESS`、回填 `ocr_result`"。
4. **文字与图片终选、证据保存**：`select_final_text_and_img` 仅占位（过滤空 blocks 并打日志）；跨帧融合、行结构整理、证据图选择与保存均未实现，当前不落盘任何图片。
5. **帧质量筛选**：`is_frame_qualified` 恒返回 True；空帧、纯黑、截断帧筛选待实现。
6. **真机验证**：本机未枚举到相机；SDK 取帧、BMP 编码、GigE 稳定性、现场内存容量和设备时间戳映射均待现场验证。
7. **断线重连**：按设计不自动重启、不自动重连，设备修复后手动重启程序。

### 3.2 当前已知问题（2026-09-19）

1. **测试失败**：全量测试 87 通过 / 44 失败。失败分两类：4 个是 OCR 降级路径收敛后仍断言旧行为的用例（名单见 2026-09-19 进度条目），其余仍因引用已改名的旧字段和旧行为：`BeltSession.frequency_candidates`（现为 `measurement_frequencies`）、已删除的 `commit_state`、`FakeFrequency.active_window`、`CapturedFrame` 旧构造参数、初始 UNKNOWN 状态期望 FAULT（现为 WAIT_CYCLE_RESET）、锁错误消息文案等，需要与当前代码对齐。
2. **OCR 成功路径断开**：见 3.1 第 3 条，依赖完整入库场景的测试会出现 OCR_TIMEOUT。
3. **频率间隔换算疑点**：`listen_measurements` 中 `frequency_interval_ms` 按 `/ 10000` 换算（配置名义 100 ms 实际约 10 ms 一条读数），待确认是联调加速还是笔误。
4. **MVS 编码容量**：BMP 输出缓冲区按 `width * height * 4 + 2048` 估算，Bayer 或大分辨率图像可能不足，需真机验证。

### 3.3 建议的下一步

1. 接通 OCR 成功路径：实现文字终选时确定保留文字、置 `ocr_state = SUCCESS` 并生成 `OCRResult`，让正常记录能端到端入库。
2. 更新存量测试，对齐当前字段与行为（`frequency_candidates` → `measurement_frequencies`，以及 OCR 降级收敛后断言旧行为的 4 个用例）。
3. 接入真实频率设备协议。
4. 实现 IO 适配层（去抖、边沿识别、状态同步）。
5. 真机验证 MVS 采集与编码。
