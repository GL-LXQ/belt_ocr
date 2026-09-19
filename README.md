# 项目概述与进度

> 本文档是项目结构、数据流和进度的入口，内容依据 2026-09-19 的代码整理。

## 一、项目概述

### 1.1 项目目标与运行方式

一台工控机管理三台皮带机，每台机器绑定海康 MVS 相机、启停输入和频率来源。一次 START → CLOSE 对应一个 BeltSession。仅将 OCR、频率和图片保存均成功的正常测量写入结果库；失败和中断打印日志，不写异常测量记录。运行库保存异常事件审计并提供进程级实例锁。

Python 3.10 及以上，运行时使用标准库和 MVS 官方绑定，pytest 用于测试。保持扁平模块结构，采用单进程、每机一个当前周期和串行事件处理、每轮一个相机采集线程、共享串行 OCR 和共享存储队列。

```powershell
uv run python -X utf8 main.py --config config.example.json
uv run python -X utf8 -m pytest -q
```

演示运行两轮启停，需要可用相机及 MVS SDK。IO 和真实频率协议尚未接入，频率来自联调配置。真实 OCR 模型和最终文字图片选择尚未实现，因此当前真实运行会明确失败，不会伪造空文字的正常记录；端到端成功链路由测试替身验证。

### 1.2 数据流动逻辑

每台机器只保留一个 current_session；空闲时 START 创建周期并同时开启相机采集和频率接收，上一轮未结束时的新 START 只记录日志并跳过；`camera.py` 的 `SessionCamera.start_capture()` 直接创建任务并启动采集线程，线程在固定窗口内收集全部独立内存帧，窗口到期或 CLOSE 后停止取流，只交付一次整轮结果。OCR 后台任务等待共享锁，在线程中按顺序执行内存 BMP 编码、筛帧黑盒、字符识别和文字图片终选黑盒，将最终文字、选中内存图片及对应关系返回原周期；CLOSE 封闭频率列表并选取最后收到的有效读数。机器管理器在正常关闭且 OCR、频率均成功后冻结内容，交给存储队列先保存选中图片、再幂等写入 SQLite，最后释放周期与图片引用，清空 current_session 后才允许下一轮。任一业务失败清理本轮，未关闭周期保留身份直到真实 CLOSE，已关闭周期等待后台任务释放后清空；三台机器可独立测量，设备故障停止整个应用。

### 1.3 处理阶段与职责

1. **启动**：校验配置，初始化双库并获取实例锁，加载 SDK、打开全部相机，检查磁盘容量，根据初始机器状态决定是否等待关闭复位，启动机器事件、频率和存储任务。任一设备打开失败则整体启动失败并释放已打开设备。
2. **采集**：START 的单调时间是窗口起点，默认 1000 ms；唯一采集线程执行启动、取帧、复制独立内存、停止和释放相机占用。相机只保留 current_capture 和一个结果交付任务，不维护 windows 字典；取消前 5 帧限制，不设置应用层帧队列，不执行质量筛选和图片编码。单次等帧默认 50 ms。CLOSE 事件携带接收时的单调时间，排队不延长业务采集边界。
3. **一次性交付**：CAPTURE_COMPLETED 携带原始帧和统计；设备采集失败交付 CAPTURE_FAILED 并触发全局故障退出。统计只包含采集耗时、接收帧数、保留帧数、边界排除数量、停止状态和错误。
4. **OCR**：每台机器最多一个识别任务，三台机器共用处理锁。`process_session_frames` 顺序完成编码 → `filter_qualified_frames` → `recognize_images` → `generate_final_text_and_images`。锁覆盖整轮处理，无批次队列和消费者。成功只发送一次 OCR_COMPLETED，普通识别失败发送 OCR_FAILED；SDK 编码异常仍属于设备故障。
5. **关闭与结算**：CLOSE 停止本轮采集，封闭频率接收并选取最后一条有效读数；没有读数或周期中断则失败。关闭后当前周期继续占用本机，直到图片、数据库保存或失败清理全部完成，才接收下一轮。机器管理器不参与 OCR 中间结果整理。
6. **提交**：正常关闭、OCR 成功和频率成功后，生成 `evidence_directory / machine_id / session_id / frame_id.bmp` 路径，冻结 JSON 和 SHA256，进入 WAITING_COMMIT_DB。存储线程先原子保存图片，再写数据库，完成后返回 COMMIT_SUCCEEDED 或 COMMIT_FAILED，不自动重试。
7. **失败与退出**：整轮 OCR 超时从 START 计时，包含采集、排队和处理。等待锁的任务取消后不执行模型；已开始的阻塞操作等线程实际结束后再释放锁，迟到结果丢弃。退出时关闭入口、排空事件、停止采集、收尾后台处理，最后关闭相机、SDK 和数据库。退出等待期限不能强制终止已经运行的线程。

### 1.4 两个黑盒与结果契约

| 接口 | 输入 | 输出与当前行为 |
|---|---|---|
| `filter_qualified_frames` | 按接收顺序排列的 CapturedFrame 内存图片 | 返回合格图片；当前原样返回，纯黑和截断规则待实现 |
| `recognize_images` | 合格图片 BMP 字节列表 | 返回与输入等长的原始 blocks 列表；当前抛出 OCR_MODEL_NOT_IMPLEMENTED |
| `generate_final_text_and_images` | 带 frame_id 的识别结果和合格图片 | 返回 OCRResult；当前抛出 OCR_FINAL_SELECTION_NOT_IMPLEMENTED |

两个黑盒不修改 Session、不发送事件、不保存文件、不访问数据库。无帧、无合格帧、无最终文字、无选中图片和模型返回数量不匹配均立即按整轮失败处理。

OCRResult 只包含 `ordered_lines`（有序文字）、`selected_frames`（按 frame_id 唯一的选中图片）和 `line_frame_ids`（与文字逐项对应的来源帧编号集合）。没有选中的中间图片不进入 Session，最终图片在提交时转交存储请求。

数据库继续保留 `ordered_lines`、`evidence_refs`、频率明细等查询列，完整 `payload_json` 增加 `line_evidence_refs`，按文字顺序保存对应图片路径。此次不增加表列，不提供旧 OCR 接口兼容层。图片保存失败不写数据库；确认没有提交时只清理本次新建图片，已存在的图片不覆盖或删除；提交结果未知时保留图片并记录日志。文件与 SQLite 不构成跨资源原子事务，本次未增加崩溃恢复或孤立图片清理。

### 1.5 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | 演示启停、故障等待和退出码 |
| `app.py` | 初始化、信号路由、容量维护、全局故障与资源释放 |
| `machine_manager.py` | 每机周期状态、启停、频率、整轮 OCR 调度及提交条件 |
| `camera.py` | 创建采集任务、启动单线程收集整轮帧、停止采集并一次性交付结果 |
| `mvs_sdk.py` | SDK 加载、相机打开、取帧、内存 BMP 编码和关闭 |
| `text_recognition.py` | 共享处理锁、OCR 主流程、筛帧与终选黑盒 |
| `frequency_adapter.py` | 联调频率监听与当前周期归属 |
| `database.py` | 双库、实例锁、审计、图片保存与幂等测量提交 |
| `models.py` / `enums.py` | 事件、帧、结果和周期状态 |
| `configuration.py` / `async_utils.py` | 配置解析、取消期间等待阻塞操作结束 |

### 1.6 关键配置

| 配置 | 含义 |
|---|---|
| `machines[].camera_serial` | 相机序列号，留空则整个应用启动失败 |
| `camera_pixel_format` / `camera_exposure_time_us` / `camera_gain` | 机器级可选相机参数，省略时保留设备设置 |
| `capture_window_ms` / `camera_timeout_ms` | 采集窗口 1000 ms / 单次读取超时 50 ms |
| `ocr_result_timeout_ms` / `max_cycle_open_ms` | 从 START 起的 OCR 期限 30 s / 等待 CLOSE 期限 60 s |
| `event_queue_capacity` / `storage_queue_capacity` | 单机事件容量 128 / 共享存储容量 32 |
| `simulated_frequencies_hz` / `frequency_interval_ms` | 机器级联调频率列表 / 全局读数间隔配置 |
| `minimum_frequency_hz` / `maximum_frequency_hz` | 有效频率范围，默认 0.01～10000 Hz |
| `shutdown_timeout_ms` | 正常退出排空业务的期限，默认 10000 ms |
| `minimum_free_disk_bytes` | 最低磁盘空间，默认 100 MiB |
| `maintenance_interval_ms` / `max_persistent_records` | 容量检查间隔 250 ms / 存储积压限制 1000 |
| `initial_machine_state` | 默认 CLOSED；OPEN、UNKNOWN 需要关闭或状态同步 |

已删除 `camera_queue_capacity`、`max_frames_per_session`、`ocr_queue_capacity` 、`ocr_job_timeout_ms` 和 `max_pending_sessions_per_machine`。使用旧配置文件时需移除这些键。窗口内帧全部保存在内存，内存占用取决于分辨率和帧率，每台机器最多保留一个未完成周期。

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

**OCR 顺序处理与精简**

- 采集由三线程和帧队列改为单线程收集全部帧；删除满批、尾批、批次事件和批次计数。
- 两个黑盒集中在 OCR 模块，新增唯一整轮主流程；识别和终选未实现时明确失败。
- 删除 Session 中间图片、识别结果及证据验证状态；只保留最终 OCR 结果。
- 图片保存并入已有存储工作流程，新增文字与图片路径对应关系；数据库表结构不变。
- 增加取消后等待工作线程、共享锁隔离、关闭信号接收时间边界及退出资源顺序测试。

**单机单周期精简**

- 将 sessions 字典与 active_session_id 合并成 current_session，删除同机多个周期并行收尾能力和周期积压配置。
- 相机只保留一个采集任务和一个交付任务，OCR 只保留一个任务；期限任务仅按事件类型登记。
- CLOSE 后继续保留当前周期直至保存或失败清理完成；忙时 START 只记录日志并跳过，不覆盖数据、不停止系统。
- 失败发生在 CLOSE 前时等待真实关闭；失败发生在 CLOSE 后时，等在途线程和图片引用释放后再允许下一轮。
- 演示先等待三台机器第一轮保存完成，再启动第一台机器的第二轮。不新增 4 秒期限，保留已有故障与超时处理。

**测试**

- 本次修改前基线：91 通过、11 失败。
- 最终全量：`uv run python -X utf8 -m pytest -q`，92 通过、11 失败。新增 4 项单周期集成验收，移除 3 个同机跨轮重叠用例，其他受影响测试改为读取唯一当前周期。
- 新增验收全部通过：采集/等待 OCR/保存阶段跳过 START、线程实际退出后才能重启、其他机器独立处理、演示顺序保存四条记录。已有 OCR/CLOSE 两种先后顺序、失败不入库和设备退出测试继续通过。
- 剩余 11 项失败名单与本次修改前完全一致：`test_acceptance_scenarios.py` 7 项涉及旧频率字段、旧结果字段和历史频率冲突规则；`test_recovery_and_faults.py` 4 项涉及旧频率字段、未等待读数即期望入库、UNKNOWN 状态期望和实例锁异常文案。未修改对应生产业务来迎合历史期望。

**相机采集模块合并**

- 将 CaptureTask、CaptureResult 和取帧循环迁入 camera.py，删除 mvs_capture.py。
- 将底层启动函数直接合并到 SessionCamera.start_capture()，统一完成相机占用、任务创建、线程启动和结果交付安排；SDK 操作继续保留在 mvs_sdk.py。
- 采集测试改用统一入口，增加线程启动失败后释放相机锁并可重新采集的验证。
- 本次全量 pytest：93 通过、11 失败；失败名单与上方已有 11 项一致。

## 三、当前状态与后续工作

1. 实现筛帧黑盒中的纯黑、截断等规则，接入真实 OCR 模型，实现文字去重、排序与图片选择黑盒。规则未确认前不自行猜测。
2. IOAdapter 和真实频率协议仍待接入。频率间隔当前使用 `/ 10000` 换算，是否为联调加速尚待确认，本次未修改。
3. 尚未进行真机验证：MVS 采集、BMP 编码缓冲容量、GigE 稳定性及全部帧驻留内存的现场占用需验证。
4. 更新剩余历史测试中的旧频率字段、旧异常入库规则和初始机器状态期望；不为迎合旧测试恢复已删除业务。
5. 设备故障不自动重启或重连，修复后手动重启程序。
