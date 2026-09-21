# 项目概述与进度

> 本文档是项目结构、数据流和进度的入口，更新于 2026-09-21。第一节分别说明 GUI 与后端，第二节集中记录项目进度，第三节列出当前状态与后续工作。

## 一、项目概述

### 1.1 GUI：桌面界面

#### 1.1.1 运行方式与界面结构

GUI 使用纯 PySide6 编写，代码位于 `ui/`，打开界面无需相机或 MVS SDK；点击“启动监测”后才加载 SDK 并连接相机。在项目根目录执行：

```powershell
uv run python -m ui
uv run python -X utf8 -m pytest tests/test_ui_shell.py -q
```

BeltVision 主窗口默认 1600 × 900、最小 1280 × 720，支持标题栏拖动、双击最大化/还原、边缘缩放和窗口控制。顶部为独立 TitleBar，下方为固定宽度 Sidebar 与可伸缩 PageContainer，PageContainer 内使用 QStackedWidget 承载实时监测、历史记录、图片管理、系统配置、机器管理、日志查看六个页面。所有页面启动时一次创建，切换时复用原有实例。

#### 1.1.2 GUI 数据流与后端边界

窗口外轮廓采用 10px 轻微圆角，最大化时切换为直角，还原时恢复圆角；窗口状态事件同步更新控制按钮和外轮廓样式，导航、时钟与状态接口的数据流保持不变。

用户点击侧栏导航或顶部设置入口后，MainWindow 同步更新 QStackedWidget、导航高亮和标题栏；QTimer 每秒刷新本地时间；后续业务层可通过 `set_system_status(text, status)` 和 `set_connection_status(connected)` 更新状态文案与圆点。机器管理页支持数据库新增、编辑和软删除，实时监测页点击“启动监测”后读取启用机器并显示真实相机连接结果，停止监测或关闭窗口会等待后台释放资源；其他四页仍为占位页面。顶部系统状态仍是演示文案，频率、画面、测量进度和日志尚未接入真实数据，本次不自动发送测量启停信号。

#### 1.1.3 GUI 模块职责

| 文件 | 职责 |
|---|---|
| `ui/__main__.py` | 初始化 QApplication、显示主窗口和运行事件循环 |
| `ui/main_window.py` | 主窗口、标题栏、导航、占位页面、时钟和状态接口 |
| `ui/theme.py` | 统一颜色和矢量图标 |
| `ui/pages/realtime_page.py` | 实时监测页、复用机器卡片、步骤进度和本地日志交互 |
| `ui/pages/machines_page.py` | 机器列表读取、新增弹窗、持久化与重复提示 |
| `ui/demo_data.py` | 机器记录与固定演示日志 |
| `ui/styles/main_window.qss` | 主窗口、导航、页面和状态样式 |
| `tests/test_ui_shell.py` | 导航、状态接口、布局伸缩和窗口控制验证 |

### 1.2 后端：采集、识别与存储

#### 1.2.1 项目目标与运行方式

一台工控机管理三台皮带机，每台机器绑定海康 MVS 相机、启停输入和频率来源。一次 START → CLOSE 对应一个 BeltSession。仅将 OCR、频率和图片保存均成功的正常测量写入结果库；失败和中断打印日志，不写异常测量记录。运行库保存异常事件审计并提供进程级实例锁。

后端使用 Python 3.10 及以上，运行时使用标准库和 MVS 官方绑定，pytest 用于测试。后端 Python 文件集中在 `src/`，保持扁平模块结构，采用单进程、每机一个当前周期和串行事件处理、每轮一个相机采集线程、共享串行 OCR 和共享存储队列。

```powershell
uv run python -X utf8 src/main.py --config config
uv run python -X utf8 -m pytest -q
```

命令行演示运行两轮启停，需要先通过机器管理页录入并启用机器，并准备可用相机及 MVS SDK。GUI 与命令行均从数据库读取机器；IO 和真实频率协议尚未接入，数据库机器默认没有模拟频率。真实 OCR 模型和最终文字图片选择尚未实现，因此当前真实运行会明确失败，不会伪造空文字的正常记录；端到端成功链路由测试替身验证。

#### 1.2.2 数据流动逻辑

入口 `src/main.py` 读取配置文件中的公共参数，`src/app.py` 构造时只保存配置、创建共享存储与运行状态，`start()` 再按顺序初始化图片目录与双库、按业务库 `machine` 表读取启用机器并逐台建立采集器、频率接收、共享 OCR 和存储任务；采集帧与频率读数按周期汇入对应机器，OCR 结果完成后保存图片与 SQLite 测量记录，退出时统一释放资源。每台机器只保留一个 current_session；空闲时 START 创建周期并同时开启相机采集和频率接收，上一轮未结束时的新 START 只记录日志并跳过；`src/camera.py` 的 `Camera.start_capture()` 创建异步采集主流程 capture_and_deliver_result()，通过 run_blocking_operation() 在线程中执行采集，线程在固定窗口内收集全部独立内存帧，窗口到期或 CLOSE 后结束循环并停止取流，允许保留当前读取返回的尾帧，由采集线程直接生成统一的 CaptureResult（全部帧和统计），run_capture() 直接返回结果，异步主流程随后通过事件的 session_id 将整轮结果交付原周期，采集接口和结果不再透传 capture_id，周期保留该编号供 OCR 生成图片编号；不再手动创建线程或通过 completion_future 传递结果；CLOSE 仅等待采集完成，退出等待交付结束。OCR 后台任务等待共享锁，在线程中按顺序执行内存 BMP 编码、筛帧黑盒、字符识别和文字图片终选黑盒，将最终文字、选中内存图片及对应关系返回原周期；CLOSE 封闭频率列表并选取最后收到的有效读数。机器运行对象在正常关闭且 OCR、频率均成功后冻结内容，交给存储队列先保存选中图片、再幂等写入 SQLite，最后释放周期与图片引用，清空 current_session 后才允许下一轮。任一业务失败清理本轮，未关闭周期保留身份直到真实 CLOSE，已关闭周期等待后台任务释放后清空；三台机器可独立测量，机器故障停止整个应用。

#### 1.2.3 处理阶段与职责

1. **启动**：校验配置，初始化双库并获取实例锁，加载 SDK、打开全部相机，根据初始机器状态决定是否等待关闭复位，启动机器事件、频率和存储任务。任一机器打开失败则整体启动失败并释放已打开机器。
2. **采集**：START 的单调时间是窗口起点，默认 1000 ms；唯一采集线程顺序执行启动、取帧、复制独立内存、归还 SDK Buffer、停止和释放相机占用，移除这条串行链路中无竞争者的 buffer_lock；机器关闭继续通过 capture_lock 等待采集结束。相机只保留 current_capture 和一个结果交付任务，不维护 windows 字典；取消前 5 帧限制，不设置应用层帧队列，不执行质量筛选和图片编码。单次等帧固定使用配置超时，默认 50 ms，不按窗口剩余时间缩短；读取返回后检查是否继续采集。CLOSE 发出停止信号，当前读取结束后退出；采集和机器运行对象均不再按帧时间筛除尾帧。
3. **一次性交付**：CAPTURE_COMPLETED 携带原始帧和统计；机器采集失败在捕获处记录日志并抛出异常，由任务结束回调安排全局退出，不再生成失败采集结果。统计只包含采集耗时、接收帧数和保留帧数。
4. **OCR**：每台机器最多一个识别任务，三台机器共用处理锁。`process_session_frames` 顺序完成编码 → `filter_qualified_frames` → `recognize_images` → `generate_final_text_and_images`。锁覆盖整轮处理，无批次队列和消费者。成功只发送一次 OCR_COMPLETED，普通识别失败发送 OCR_FAILED；SDK 编码异常在编码处记录日志，再沿后台任务传播并触发全局退出，不转换为普通识别失败。
5. **关闭与结算**：CLOSE 停止本轮采集，封闭频率接收并选取最后一条有效读数；没有读数或周期中断则失败。关闭后当前周期继续占用本机，直到图片、数据库保存或失败清理全部完成，才接收下一轮。机器运行对象不参与 OCR 中间结果整理。
6. **提交**：正常关闭、OCR 成功和频率成功后，生成 `evidence_directory / machine_id / session_id / frame_id.bmp` 路径，生成包含业务字段和选中图片的存储请求，进入 WAITING_COMMIT_DB。存储线程先原子保存图片，再写数据库，完成后返回 COMMIT_SUCCEEDED 或 COMMIT_FAILED，不自动重试。
7. **失败与退出**：整轮 OCR 超时从 START 计时，包含采集、排队和处理。等待锁的任务取消后不执行模型；已开始的阻塞操作等线程实际结束后再释放锁，迟到结果丢弃。退出时关闭入口、排空事件、停止采集、收尾后台处理，最后关闭相机、SDK 和数据库。退出等待期限不能强制终止已经运行的线程。

#### 1.2.4 两个黑盒与结果契约

| 接口 | 输入 | 输出与当前行为 |
|---|---|---|
| `filter_qualified_frames` | 按接收顺序排列的 CapturedFrame 内存图片 | 返回合格图片；当前原样返回，纯黑和截断规则待实现 |
| `recognize_images` | 合格图片 BMP 字节列表 | 返回与输入等长的原始 blocks 列表；当前抛出 OCR_MODEL_NOT_IMPLEMENTED |
| `generate_final_text_and_images` | 带 frame_id 的识别结果和合格图片 | 返回 OCRResult；当前抛出 OCR_FINAL_SELECTION_NOT_IMPLEMENTED |

两个黑盒不修改 Session、不发送事件、不保存文件、不访问数据库。无帧、无合格帧、无最终文字、无选中图片和模型返回数量不匹配均立即按整轮失败处理。

OCRResult 只包含 `ordered_lines`（有序文字）、`selected_frames`（按 frame_id 唯一的选中图片）和 `line_frame_ids`（与文字逐项对应的来源帧编号集合）。没有选中的中间图片不进入 Session，最终图片在提交时转交存储请求。

测量表仅保存周期编号、机器编号、起止时间、`ordered_lines`、`evidence_refs`、最终频率和频率明细，不保存整包 JSON、内容哈希及文字与图片对应关系。同一周期重复提交时直接比较这些业务字段，相同则成功，不同则报错。OCR 内存结果仍保留来源帧关系，不写入数据库。图片保存失败不写数据库；确认没有提交时只清理本次新建图片，已存在的图片不覆盖或删除；提交结果未知时保留图片并记录日志。文件与 SQLite 不构成跨资源原子事务，本次未增加崩溃恢复或孤立图片清理。

#### 1.2.5 模块职责

| 文件 | 职责 |
|---|---|
| `src/main.py` | 演示启停、故障等待和退出码 |
| `src/app.py` | 读取启用机器、初始化、信号路由、容量维护、全局故障与资源释放 |
| `src/machine.py` | 每机周期状态、启停、频率、整轮 OCR 调度及提交条件 |
| `src/camera.py` | 创建采集任务、启动单线程收集整轮帧、停止采集并一次性交付结果 |
| `src/mvs_sdk.py` | SDK 加载、相机打开、取帧、内存 BMP 编码和关闭 |
| `src/text_recognition.py` | 共享处理锁、OCR 主流程、筛帧与终选黑盒 |
| `src/frequency_adapter.py` | 联调频率监听与当前周期归属 |
| `src/database.py` | 双库初始化、实例锁、事件整理、图片保存与存储队列调度 |
| `src/repo/` | 按表封装建表 SQL、异常事件插入及测量记录幂等写入和查询 |
| `src/models.py` / `src/enums.py` | 事件、帧、结果和周期状态 |
| `src/config_util.py` / `src/async_utils.py` | 配置解析、取消期间等待阻塞操作结束 |

#### 1.2.6 关键配置

| 配置 | 含义 |
|---|---|
| `machine.camera_serial`（数据库） | 相机序列号，由机器管理页登记，后台按序列号连接 |
| 相机像素格式、曝光和增益 | 数据库机器暂不提供覆盖值，连接时保留相机设置 |
| `capture_window_ms` / `camera_timeout_ms` | 采集窗口 1000 ms / 单次读取超时 50 ms |
| `ocr_result_timeout_ms` / `max_cycle_open_ms` | 从 START 起的 OCR 期限 30 s / 等待 CLOSE 期限 60 s |
| `event_queue_capacity` / `storage_queue_capacity` | 单机事件容量 128 / 共享存储容量 32 |
| `frequency_interval_ms` | 全局读数间隔配置；数据库机器不生成模拟频率 |
| `minimum_frequency_hz` / `maximum_frequency_hz` | 有效频率范围，默认 0.01～10000 Hz |
| `shutdown_timeout_ms` | 正常退出排空业务的期限，默认 10000 ms |
| `initial_machine_state` | 默认 CLOSED；OPEN、UNKNOWN 需要关闭或状态同步 |

已删除 `camera_queue_capacity`、`max_frames_per_session`、`ocr_queue_capacity` 、`ocr_job_timeout_ms` 和 `max_pending_sessions_per_machine`，以及容量监控的 `minimum_free_disk_bytes`、`maintenance_interval_ms`、`max_persistent_records`。使用旧配置文件时需移除这些键。窗口内帧全部保存在内存，内存占用取决于分辨率和帧率，每台机器最多保留一个未完成周期。

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
- 文档同步：本文件后端处理阶段段落补全记录字段说明。

**OCR 降级路径收敛**

- `FRAME_BATCH_SELECTED` 入队被拒与 `RECOGNITION_BATCH_FAILED` 不再是"只记错误、周期继续"：置 `ocr_state = FAILED` 后进入 `handle_measurement_failure`，与 OCR 失败、采集失败、超时同一出口，本轮不生成记录。
- 拒收按原因分开处理：队列满说明识别服务已经积压，按整轮失败处理；`accepting_batches` 已停止接收只出现在退出阶段，本轮交给退出流程中断，不在 OCR 侧定因（否则正常退出会被记成批次拒收）。
- 两个分支本地重复的 logger.error 删除，失败明细统一由 `handle_measurement_failure` 打印；失败后迟到的批次结果由 `state != RUNNING` 检查丢弃。
- 至此 OCR 侧所有残缺路径都终结本轮，"入库即完整成功"成立。
- 本次未改测试：全量测试由 91 通过 / 40 失败变为 87 通过 / 44 失败，新增的 4 个失败用例断言的都是被推翻的旧行为（test_full_queue_rejects_tail_and_preserves_first_batch、test_failed_batch_does_not_stop_next_batch、test_failed_and_rejected_batches_preserve_successful_results、test_no_usable_text_skips_postprocessing[failed]），待与存量用例一起对齐（见 3.2 第 4 条）。

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

**采集尾帧简化**

- 窗口到期或 CLOSE 时允许保留当前读取返回的尾帧，删除采集线程和机器管理器中的帧时间筛选。
- 删除采集任务的 capture_stop_time 和全链路 skipped_frame_count 统计；周期关闭时间仍用于状态及频率处理。
- 更新尾帧保留测试，全量 pytest：93 通过、11 个既有失败，失败名单不变。

### 2026-09-20

#### GUI：设备管理演示页

设备表头与单元格统一垂直居中，文字列、状态标签和操作按钮左对齐，编号居中；业务字段使用紧凑固定宽度，操作列承接末尾空白，按钮靠左排列，避免机器名称列被过度拉宽。状态列加宽以完整显示圆角标签。固定数据仍进入页面内存，经新增或编辑弹窗保存后刷新列表，不接入后端。UI 专项 pytest：8 项通过。

设备管理页从 `ui/demo_data.py` 复制三条固定设备记录到页面内存，使用 Qt 标准全宽表格和新增、编辑共用的 `QDialog`，沿用主窗口、主题图标与页面样式。单击机器行仅高亮且不显示单元格焦点框；点击编辑将记录填入弹窗，点击新增清空表单，表单仅包含机器名称、相机序列号、频率仪序列号、启用状态和备注，创建时间与更新时间保留在列表。保存校验三个必填字段后更新内存与表格、选中目标记录并关闭弹窗；取消、窗口关闭或 Esc 放弃未保存内容，删除仍经确认后移除记录。新增编号在页面生命周期内递增，演示时间使用本地时间；切换页面保留数据，重启恢复固定记录，不调用数据库或采集服务，也不修改实时监测演示数据。UI 专项 pytest：8 项通过，覆盖行选中、弹窗复用、保存关闭、取消、关闭与 Esc、必填校验、删除及数据隔离；已检查 Windows 下 1600×900 与 1280×720 的布局和编辑弹窗。

#### GUI：主窗口外壳与代码规范

- 新增纯 PySide6 的 BeltVision 主窗口、六个占位页面、导航切换、时钟、状态接口和窗口控制；已使用 Windows 平台实际渲染检查主窗口。
- 按 AGENTS.md 整理 UI 代码格式和中文分步骤注释，补全函数返回示例及测试 fixture 的产出说明，保持界面行为不变。
- UI 专项 pytest：3 项通过；主窗口实现阶段全量 pytest：112 项通过、10 项失败，失败涉及历史频率字段、结果字段、初始状态与实例锁文案。格式与注释调整后 UI 专项仍为 3 项通过。

#### GUI：实时监测静态演示页

实时监测页读取 `ui/demo_data.py` 的固定设备和日志数据，以及 `ui/assets/` 中从参考图截取的三张皮带图片，通过复用的 MachineCard 和 StepProgress 展示三台设备的画面、状态、频率、进度和最近事件，日志进入只读表格；刷新将演示数据重新填入已有控件，清空日志仅移除表格内容，自动滚动决定追加日志时是否定位末尾。启动全部、停止全部和更多入口禁用并提示暂未接入，图片区域标注演示画面；窗口顶部时钟继续独立刷新，页面不启动相机、OCR 或数据库服务。默认尺寸展示完整页面，小窗口通过纵向滚动访问全部内容。

UI 专项 pytest：5 项通过；全量 pytest：114 项通过、10 项失败，失败涉及文档已记录的后端历史字段、初始状态及实例锁文案。已在 Windows 平台渲染检查 1600×900 和 1280×720 两种尺寸。

#### GUI：实时监测视觉细节调整

演示数据继续通过设备卡片更新状态、频率、步骤和最近事件；状态图标与频率波形统一使用主题 SVG，步骤连接线根据相邻圆点位置随窗口伸缩，当前步骤显示勾选图标与高亮边框。固定相机图片等比例铺满展示区并居中裁切，画面标识与时间叠放在图片上方；相机名称和序列号分列对齐，事件使用独立的时间与事件颜色标记，刷新和日志数据流保持不变。

#### GUI：实时监测信息精简

实时监测页从固定演示数据读取机器状态、频率、步骤和事件，并更新复用卡片；删除相机名称与序列号、频率范围、Session 与上次测量编号、百分比进度条及已处理帧数，同步移除对应控件、演示字段和进度条样式。本轮进度仅展示步骤，图片展示、刷新恢复及日志交互保持不变。顶部操作区已移除“演示数据”标识；页面仍读取固定演示数据，经刷新填入卡片和日志表格，尚未接入后端。

#### GUI：页面目录整理

实时监测页移入 `ui/pages/realtime_page.py`，主窗口导入页面并放入页面栈；页面继续读取固定演示数据和 `ui/assets/` 图片，更新设备卡片与日志表格。资源路径已随目录调整，数据流与界面行为不变。

#### GUI：移除画面固定时间

设备卡片继续读取固定图片并展示画面标识，移除三张图片上写死的时间；设备状态、频率、步骤和事件仍由演示数据填入，顶部系统时钟及事件、日志时间保持原有更新与展示逻辑。

#### 后端：设备基础表

`Database.initialize()` 在初始化双库时，通过 `initialize_result_database()` 在业务库中幂等创建 `machine` 表，与 `measurements` 共用数据库；设备表包含自增主键 `id`、唯一机器编号 `machine_id`、名称 `machine_name`、相机序列号 `camera_serial`、频率仪序列号 `frequency_meter_serial`、启用状态 `enabled`、创建时间 `created_at`、修改时间 `updated_at` 和可空备注 `remark`。启用状态默认 1，两个时间字段默认写入 UTC 时间（YYYY-MM-DD HH:MM:SS）；后续修改接口需显式更新 `updated_at`，当前未创建自动更新时间触发器。本次只创建表结构，不插入设备数据；后端仍从原配置读取设备，实时监测页仍使用固定展示数据，采集、识别、图片保存和测量提交的数据流不变。

设备表与测量存储专项 pytest：18 项通过，覆盖首次建表、重复初始化保留记录、默认值、字段约束及原有测量存储行为。

#### 后端：按表拆分 Repo

设备表改名为 `machine`，由 `src/repo/machine_repo.py` 中的 `MachineRepo` 管理；测量表和异常事件表分别由 `measurement_repo.py` 中的 `MeasurementRepo`、`abnormal_event_repo.py` 中的 `AbnormalEventRepo` 管理。`Database.initialize()` 调用各 Repo 建表；测量结果进入共享队列后，由 Database 先保存图片，再调用测量 Repo 在事务中完成幂等写入，失败时通过 Repo 查询提交状态后决定是否清理图片；异常事件由 Database 整理为 JSON 后交给异常事件 Repo 插入。本次迁移已有表操作，未额外增加尚未使用的更新、删除或查询接口，设备表暂时只有建表逻辑。未发现本地旧数据库，本次不增加旧 `machines` 表迁移。

全量 pytest：116 项通过、10 项既有失败，与修改前的通过数量及失败名单一致。

#### 后端：统一 Repo 命名

数据访问目录统一为 `src/repo/`，按表使用 `MachineRepo`、`MeasurementRepo` 和 `AbnormalEventRepo`；Database 调用 Repo 初始化表结构、插入异常事件及幂等保存测量结果，仍按先保存图片、再提交测量记录、失败时查询提交结果的顺序处理，业务行为不变。

#### 后端：取流顺序修正

START 创建周期后，相机先启动取流，再清空 SDK 缓存，随后在采集窗口内读取并复制全部帧；即使清缓存失败，已启动的取流也会在采集收尾时停止。采集结果交给 OCR 编码、筛帧、识别和终选，CLOSE 确定最后有效频率，结果完整后由存储队列先保存图片，再写入 SQLite。

#### 后端：测量存储字段精简

删除 measurements 表的 payload_json、payload_hash 以及周期中的整包 JSON 和哈希字段，不新增文字与图片对应关系列。START 创建周期后采集图片和频率，采集结果依次编码、筛帧、识别和终选；CLOSE 选取最后有效频率，结果完整后直接组装业务字段及选中图片的存储请求，由存储队列先保存图片，再幂等写入测量表，最后释放周期。运行库 abnormal_events 的 payload_json 继续用于异常审计。本地未发现 SQLite 库文件，本次只更新建表结构，未添加旧库迁移；已有旧 measurements 表的部署环境需先移除这两个旧列才能使用新写入逻辑。

本次全量 pytest：110 通过、10 个既有失败，与修改前 103 通过、10 个失败相比无新增失败；新增 7 项业务字段冲突验证，原有图片保存、失败清理、三机隔离和重复提交测试继续通过。

#### 文档：分离 GUI 与后端说明

- 概述分别说明 GUI 与后端的运行入口、数据流和模块职责；当前状态与后续工作也按 GUI、后端分开。
- 将散落在文档开头和后续工作中的变更、测试记录统一移入本节。未标注日期的原记录保留为历史补记，不推定具体日期。

### 历史补记（原记录未标注日期）

以下均为后端变更，保留原记录顺序和阶段性测试结果，不代表当前最新接口或测试结果。

#### 采集调度、异常处理与字段精简

本次采集调度简化：启动采集成功后直接向 delivery_task 注册周期释放回调，移除多余的空值判断；复用 run_blocking_operation() 等待后台采集，取消时仍等待真实线程结束；采集结束后在同一处释放相机占用、通知 CLOSE 并清空 current_capture，再发布整轮结果，移除结果交付后的重复清空。采集完成信号仅供 CLOSE 等待停流，不参与结果传递；采集结果交给 OCR，正常关闭且 OCR 和频率成功后，由存储队列先保存图片再写入数据库。

本次全量 pytest：95 通过、11 个既有失败，失败名单不变；新增取消等待实际采集结束、CLOSE 不等待阻塞交付两项验证均通过。

采集停止流程：机器关闭或本轮测量失败时，机器管理器调用 inform_capture_workflow_stop()，通知采集线程停止并等待采集结束；采集结果仍通过事件交给 OCR，正常关闭且 OCR、频率成功后，由存储队列先保存图片再写入数据库。

本次异常处理简化：START 启动采集，成功停流后将全部帧交给串行 OCR，正常关闭且 OCR、频率成功后先保存图片再写入数据库；采集和编码设备故障在捕获处打印日志并抛出，外层只保存故障和释放资源。删除采集线程故障回调、失败采集事件、结果错误字段及重复异常日志；取消仍等待实际线程结束，真实异常继续传播，捕获异常的存储消费者响应保留的取消请求后退出。普通识别失败仍只结束本轮。

本次全量 pytest：100 通过、11 个历史失败；新增取消后编码失败、取消期间存储失败、取消期间启动失败、采集与停流同时失败、异步任务创建失败五项回归测试均通过。验收测试的采集替身已同步任务创建契约，此前由空任务引用引起的新增失败已消除。

本次频率字段精简：删除监听中的 source_epoch、source_sequence 以及 FrequencyMeasurement 中的 measurement_id、source_sequence 字段。START 建立周期后，频率适配器过滤无效读数，并携带周期编号、设备来源、频率值和时间信息交给机器事件队列；周期按接收顺序保存明细，CLOSE 时取最后一条有效读数作为最终频率，OCR 成功后由存储队列先保存图片再写入数据库。新写入的频率明细不再包含测量编号和来源序号，已有记录不改写。

本次全量 pytest：100 通过、11 个既有失败，失败名单与修改前一致；频率测试继续验证接收顺序、最后有效读数、周期隔离，并确认入库明细不再包含已删除字段。

本次无用字段清理：删除未参与事件处理的 event_max_age_ms 配置、只赋值不读取的 Database.available 和仅测试使用的 BeltSession.finished 属性；图片编码接口改为直接返回 BMP 字节，OCR 不再接收未使用的扩展名。START 建立周期并采集图片和频率，采集帧编码为 BMP 后依次筛选、识别和终选；CLOSE 确定最后有效频率，结果完整后先保存 BMP 图片再写入数据库，提交状态统一读取 SessionState。

本次全量 pytest：100 通过、11 个既有失败，失败名单不变；编码测试已同步仅返回 BMP 字节的接口，并核对识别阶段实际收到的图片字节。

本次频率时间精简：FrequencyMeasurement 仅保留 session_id、frequency_source_id 和 value_hz，删除 measured_at、received_at、measured_monotonic。START 建立周期后，有效频率按接收顺序进入本机事件队列并追加至明细列表，相同值也完整保留；CLOSE 封闭列表并选取最后一条，OCR 成功后由存储队列先保存图片再写入数据库。新入库明细不再包含读数时间，已有记录不改写，周期起止时间和通用事件时间保持不变。

本次全量 pytest：101 通过、10 个既有失败；原先依赖读数时间的历史验收用例已改为验证旧周期读数隔离及无有效频率不入库，其余失败名单不变。频率测试同时核对重复值保留、接收顺序、最终读数和仅含三个字段的入库明细。

#### 频率事件处理精简

本次清理频率事件的重复结算检查：START 建立周期并启动图片采集和频率接收，有效频率事件仅追加读数后返回，迟到频率保留审计；CLOSE 封闭频率列表并确定最后有效读数，再检查结算条件。采集图片经编码、筛帧、OCR 和终选后交付最终结果，正常关闭且 OCR、频率均成功时，由存储队列先保存图片再写数据库。

本次删除未使用的频率失败事件及处理分支：START 建立周期后，频率适配器将有效读数交给机器事件队列，CLOSE 选取最后一条有效读数；采集图片经过编码、筛帧、OCR 和终选后，与频率结果一起交给存储队列，先保存图片再写数据库。频率监听异常直接触发应用退出和资源释放；无有效读数、周期中断和失败清理仍使用 FrequencyState.FAILED。

#### 三机图片隔离验证

三机图片隔离测试：`tests/test_mvs_session.py` 中的 `test_three_simultaneous_cameras_keep_image_contents_isolated` 使用三个 SDK 替身和线程屏障让三台相机同时进入首帧读取，各自生成不同像素的图片；测试沿 START、独立采集、共享串行 OCR、CLOSE、图片保存和 SQLite 提交的完整数据流，逐帧核对像素与周期身份，并检查最终图片文件和数据库引用，验证程序侧三机图片隔离。该测试不替代三台真实相机的现场联调。

## 三、当前状态与后续工作

### 3.1 GUI

1. 主窗口外壳、六个导航入口、机器管理的新增/编辑/软删除和实时监测页的机器卡片（读取 `machine` 表）已完成，其他四页仍为占位页面。
2. GUI 已接入后台相机连接与退出流程；后续需接入真实 IO、频率、采集画面和测量进度，替换其余演示状态。

### 3.2 后端

1. 实现筛帧黑盒中的纯黑、截断等规则，接入真实 OCR 模型，实现文字去重、排序与图片选择黑盒。规则未确认前不自行猜测。
2. IOAdapter 和真实频率协议仍待接入。频率间隔当前使用 `/ 10000` 换算，是否为联调加速尚待确认，本次未修改。
3. 尚未进行真机验证：MVS 采集、BMP 编码缓冲容量、GigE 稳定性及全部帧驻留内存的现场占用需验证。
4. 更新剩余历史测试中的旧频率字段、旧异常入库规则和初始机器状态期望；不为迎合旧测试恢复已删除业务。
5. 机器故障不自动重启或重连，修复后手动重启程序。

### 机器信息插入

调用 `MachineRepo(database_path).insert(machine_name, camera_serial, frequency_meter_serial, enabled=True, remark=None)` 将单台机器信息写入业务库的 `machine` 表；方法打开连接、执行参数化 INSERT、提交事务并关闭连接，返回新增记录的自增主键。创建时间和修改时间由表默认值生成 UTC 时间，机器身份使用数据库自增 `id`，不再维护独立的 `machine_id` 字段；Database 初始化时提供业务库路径并负责建表，界面和采集配置的数据来源暂不变。

机器表身份字段精简为自增 `id` 和显示名称 `machine_name`，相机与频率仪序列号、启用状态、时间及备注保持不变；调用方提交机器信息后，Repo 在事务中插入并返回 `id`。本次仅调整机器表和插入接口，采集周期及测量记录中的现有 `machine_id` 暂不调整。

### GUI：新建机器持久化

GUI 启动入口通过 `read_configuration_settings()` 读取 `config/` 下 YAML 的 `database_path`，初始化业务库的机器表，将 `MachineRepo` 经主窗口传给机器页；机器列表直接读取数据库，提交新增表单时校验三个必填字段，再调用 `insert()` 保存，机器名称、相机序列号和频率仪序列号分别受唯一约束限制。重复时提示对应字段并保留输入，其他插入错误提示保存失败；保存成功后关闭弹窗、重新读取列表并选中新机器，刷新失败则明确提示机器已保存。编号和 UTC 时间由数据库生成，空备注读取为空字符串，重启保留机器；编辑和删除按钮暂时禁用，实时监测仍使用演示数据，后端采集仍读取原配置。本次只更新建表定义，不增加旧表迁移。机器页和 Repo 专项 pytest：16 项通过。

本次全量 pytest：125 项通过、10 项失败，失败均涉及文档已有的后端历史字段、初始状态和实例锁文案；GUI 启动入口已使用临时数据库完成离屏启动与退出验证。

### GUI：机器业务分层

机器管理采用 Page → MachineService → MachineRepo：GUI 启动时初始化机器表并创建 Repo 和 Service，经主窗口将 Service 传给机器页；页面读取表单、校验必填信息并调用 `create_machine()`，Service 调用 Repo 插入记录，将重复字段和其他数据库异常转换为 `MachineServiceError`，页面负责显示提示和定位字段。新增成功后页面通过 `list_machines()` 重新读取并展示机器，Repo 继续负责 SQL、事务和唯一约束；数据库路径、编号、时间、编辑删除禁用状态以及后端采集数据来源均沿用上一版。

本次分层调整专项 pytest：19 项通过，覆盖持久化新增、三个独立重复字段提示、插入与刷新失败，以及 Service 对数据库异常的转换。

### 新建机器：重复使用返回值

机器页校验表单后调用 Service，再由 Repo 插入数据库；Service 的 `create_machine()` 在成功或字段重复时返回 `success`、`machine_id`、`field`、`message`，页面根据返回值刷新列表或提示重复并定位输入框。数据库访问失败及其他非重复约束错误继续转换为 `MachineServiceError`，由页面提示保存失败；保存后的列表查询仍通过 Service 完成。

### 新建机器：业务流程可读性整理

页面将表单交给 `create_machine()`，Service 调用 Repo 插入机器；数据库错误统一进入一个处理分支，通过明确的错误映射取得重复字段和提示，重复时返回失败结果，其他数据库故障抛出业务异常。插入成功后返回机器编号，页面重新查询并刷新列表。本次仅整理错误分支和命名，数据流与返回契约不变。

### 新建机器：先查询重复字段

机器页提交表单后，Service 先调用 Repo 的 `find_duplicate_field()`，一次查询机器名称、相机序列号和频率仪序列号是否已存在，按表单顺序返回首个重复字段；有重复直接返回中文提示，没有重复再插入并返回机器编号，页面随后刷新列表。Service 不再匹配 SQLite 英文错误文本；数据库唯一约束继续保留，预查询后的并发重复及其他数据库故障统一走保存失败异常处理。

### GUI：机器编辑与软删除

机器列表的编辑、删除按钮按机器编号接线：点击编辑后页面用 `list_machines()` 的结果回填表单，并把编号记入 `editing_machine_id`；点击新增时该字段置空。保存时页面按该字段选择 `create_machine()` 或 `update_machine()`，两个接口都返回 `success`、`machine_id`、`field`（重复失败时另带中文 `message`），页面据此提示重复字段、关闭弹窗、重新读取列表并选中该机器，编辑与新增共用同一个弹窗和同一段刷新逻辑。点击删除先弹确认框，确认后调用 `delete_machine()`，Repo 执行 `UPDATE machine SET is_deleted = 1, updated_at = CURRENT_TIMESTAMP`，列表查询只读取 `is_deleted = 0` 的记录，因此机器从界面消失但原记录保留在表中，删除后的名称和序列号可以由新机器重新使用。

机器表的三个唯一约束改为 `WHERE is_deleted = 0` 的部分唯一索引，`find_duplicate_field()` 只查询未删除记录并在编辑时排除自身编号（`id IS NOT ?`），编辑时保持自身字段不变不会被判为重复。建表只在初始化时执行，已有旧库需要删除后按新结构重建。

本次全量 pytest：138 项通过、10 项既有失败，失败名单与修改前完全一致（修改前为 132 项通过、同一 10 项失败）；机器页、机器表与机器业务专项 pytest：29 项通过，新增 6 项用例覆盖编辑回填与保存、编辑保持自身字段、删除确认与取消、软删除后列表与字段复用。

### GUI：实时监测读取机器表

实时监测页不再使用固定演示机器：页面在构造时调用 `MachineService.list_enabled_machines()` 读取业务库 `machine` 表，只展示未删除且已启用的机器（停用机器不再出现在实时监测页，机器管理页仍能看到它并重新启用），`populate_cards()` 先移除上一批卡片，再按记录逐台重建，卡片数量、标题和顺序完全跟随数据库；没有机器时卡片区显示空态提示。"刷新"按钮改为重新读库并重建卡片，读库失败弹 `QMessageBox` 提示并把卡片区置空。机器业务服务由主窗口在创建页面栈时传入，页面不直接访问 Repo。

卡片上除机器名称以外的字段暂时是占位内容：状态徽标显示"未接入"、当前状态"空闲"、实时频率"--"、本轮进度停在起点、最近事件留空；画面区统一显示灰色占位块（`QLabel#capturePreview` 改为浅灰底浅边框，右上角标识改为"暂无画面"），`CapturePreview` 不再加载演示图片，`ui/assets/` 下的三张皮带图连同目录已删除；卡片区改为网格布局，每行固定 `CARDS_PER_ROW`（3）台，超出的机器自动换到下一行，空态提示横跨整行。`MachineCard` 的高度策略设为按内容最小（`QSizePolicy.Minimum`），重建卡片后调用 `updateGeometry()` 通知滚动区重算内容高度，因此第二行出现时卡片保持自身高度、由页面纵向滚动，不会被压扁；只有一行时卡片仍会被拉高填满。系统日志表格仍是固定演示日志，下一轮换成异常事件。`demo_data.MACHINES` 常量已删除，`ui/demo_data.py` 只保留机器记录和演示日志。

本次全量 pytest：138 项通过、10 项既有失败，失败名单与修改前一致；实时监测的两条用例重写为"卡片跟随机器表"（含空态提示、占位字段、灰色画面、软删除后卡片消失、最小窗口无横向滚动条）和"演示日志填充、追加滚动与清空"。

### 机器业务服务目录迁移

机器业务服务迁入 `src/service/machine_service.py`，GUI 启动入口创建 MachineService 并经主窗口传给机器管理页和实时监测页；页面将机器新增、编辑、软删除及列表读取请求交给 Service，Service 调用 MachineRepo 读写 SQLite，再将结果返回页面用于刷新展示。界面和测试统一使用新模块路径。

### 2026-09-21：数据库机器接入与相机连接状态

点击“启动监测”后，后台线程从配置文件读取公共参数，从同一业务库的 `machine` 表读取启用且未删除的机器，以 `str(id)` 作为后台机器编号、相机序列号作为相机身份、频率仪序列号作为频率来源；现有 `App.start()` 调用 MVS SDK 枚举并按序列号打开相机，将相机交给对应机器处理器，通过 Qt 信号将连接中、相机已连接及失败原因送到对应卡片。任一相机失败仍整体退出并释放已连接机器，停止监测或关闭窗口也等待后台和相机资源释放后结束。机器修改在下一次启动监测时生效，JSON 中旧 `machines` 清单不再参与机器绑定；本次不自动发送 START/CLOSE，不提供真实频率、OCR、实时画面或自动重连。没有启用机器时提示先添加并启用机器；相机连接成功仅表示相机可用，不代表 IO 或频率仪已经接入。

新增 `src/service/monitoring_service.py` 管理后台线程与监测生命周期，GUI 入口为后端现有扁平模块加入搜索路径。相机连接结果按机器编号缓存，刷新卡片后恢复，完整错误原因可在卡片提示中查看。专项 pytest 为 49 项通过；全量 pytest 为 144 项通过、10 项既有失败，失败名单未新增。测试使用 SDK 替身覆盖机器过滤、序列号绑定、连接失败释放、刷新保留状态和连接过程中关闭窗口；真实 MVS 硬件尚待现场验证。

### 统一相机与频率仪序列号字段

后台从机器表读取启用机器后，以数据库 `id` 作为 `machine_id`，相机配置、测量周期、OCR 图片及日志统一使用 `camera_serial`，删除独立的 `camera_id`；频率配置、监听事件、周期明细和新入库的频率 JSON 统一使用 `frequency_meter_serial`，不再使用 `frequency_source_id`。START 建立周期并绑定机器序列号，采集图片与频率读数交给对应周期，OCR 和频率成功后仍先保存图片再写入测量记录。序列号非空及唯一性统一在配置校验中处理，删除相机序列号重复校验及启动时的重复空值检查（后续精简中唯一性改由数据库未删除记录的部分唯一索引保证，配置校验只保留非空检查，见后文“配置校验精简”）；数据库表结构不变，已有频率 JSON 历史记录不改写。

### 相机状态通知命名调整

相机状态通知按层命名：`MonitoringService` 保留 Qt 信号 `camera_state_changed_signal`，`App.start()` 的通知参数为 `notify_camera_state`，类型是 `Callable[[str, str, str], None] | None`，后端核心不导入 PySide6；监测服务把信号的 `emit` 作为回调传入，App 逐台打开相机时依次上报“连接中”、“相机已连接”（附“IO、频率仪尚未接入”）或“连接失败”及异常原因，经 Qt 队列连接送到实时监测页更新对应机器卡片；连接与退出流程保持不变。

### 统一机器命名

皮带机这一业务实体此前存在 `machine` 与 `device` 两种叫法，本次统一为 `machine`／「机器」，改动只涉及命名，配置解析、Repo、Service、界面页面与样式、测试和文档的数据流完全不变：`src/config_util.py` 的校验文案与读取启用机器的局部变量改名；`src/repo/machine_repo.py` 与 `src/service/machine_service.py` 的中文注释、方法参数和 SQL 参数改名，Service 返回字典键由 `device_id` 改为 `machine_id`；界面侧 `ui/pages/devices_page.py` 更名为 `ui/pages/machines_page.py`、类名 `DevicesPage` 改为 `MachinesPage`，页面标识 `devices` 改为 `machines`，表格、表单、启用复选框和状态标签的 objectName 以及 `ui/styles/main_window.qss` 中的选择器同步改为 `machineTable`、`machineEditor`、`machineFields`、`machineEnabled`、`machineStatus`，`ui/main_window.py` 的导入、PAGES 键和导航标题、`ui/theme.py` 的图标名、`ui/pages/realtime_page.py` 的 `cards_by_machine_id` 与 `reload_machines()`、用户可见文案与测试断言一并更新。相机侧的 `device` 保持原义不变：`src/mvs_sdk.py` 的 SDK 设备枚举与 `src/camera.py` 的 `self.device` 仍指打开的相机句柄，`ui/theme.py` 的 devicePixelRatio 仍指显示像素比例。本次改名不涉及数据流动逻辑，数据仍从配置解析读取启用机器，经 Repo 读写 `machine` 表、Service 返回 `machine_id`，再由机器管理页与实时监测页展示，后台监测与采集流程保持原样。

### 统一设备用词

中文「设备」此前同时指皮带机、相机和频率仪，本次按对象拆分：皮带机统一写「机器」，相机硬件与相机句柄、取流、采集、编码流程统一写「相机」，频率仪统一写「频率仪」，`src/mvs_sdk.py` 的 SDK 术语（设备枚举、设备列表、设备时间戳）保留原词。频率监听日志的字段标签由 `device_id` 改为 `frequency_meter_serial`，与库中频率明细字段一致。本次只调整中文注释、文档字符串和日志标签，采集、OCR、频率、存储与数据库写入的数据流保持不变。


### 配置按业务拆分为 YAML

公共配置集中于单一文件 `config/config.yaml`，按业务分为五个顶层段落：`application` 段保存数据库、图片路径、存储和退出参数；`camera` 段保存 SDK 路径及采集参数；`ocr` 段保存识别期限；`frequency` 段保存读取间隔和有效范围；`machine` 段保存公共初始状态、周期期限和事件队列容量。配置键保持原名，可选项省略时使用配置类默认值；暂时不使用某一组可选参数时，段落内容写为 `{}` 或整段省略。相对路径以 `config.yaml` 所在目录为基准，例如 `../runtime/measurements.sqlite3`，不受进程工作目录影响。已移除旧 JSON 配置入口和五个分文件格式，命令行 `--config` 仍接收配置目录，默认使用项目根目录下的 `config/`；运行命令为 `uv run python -X utf8 src/main.py --config config`。YAML 解析使用 PyYAML。

五个段落目前只做人工分组，读取时 `read_configuration_settings()` 按固定段落顺序把各段合并成同一份扁平参数，再构造一份 `AppConfig`，因此代码里仍统一写 `config.xxx`，段落归属靠键名搜索回溯。每个配置键在五个段落之间全局唯一（`read_configuration_settings()` 遇到跨段重名会直接报错并报出两个来源段，并由测试用例守住），所以查一个配置项从哪来、谁在用，直接按键名搜索即可：`grep -rn capture_window_ms config/ src/` 会依次给出 YAML 定义、`AppConfig` 字段和全部使用点。另外用 `fields(AppConfig)` 与配置键集合的一致性测试，保证新增或改名键时不会漏改配置类。

数据流：GUI 和后端通过 `src/config_util.py` 的 `read_configuration_settings()` 读取 `config/config.yaml` 的五个段落、合并公共参数并转换路径和初始状态；GUI 使用业务库路径初始化设备表，允许空设备列表；后端通过 `load_config()` 读取公共参数、组装并校验配置对象，再由 `App` 从业务库读取启用机器，然后连接相机、组织测量。START 创建周期并采集图片与频率，OCR 完成编码、筛帧、识别及终选，CLOSE 选取最后有效频率，完整成功后由存储队列先保存图片、再写 SQLite，结束时释放资源。机器清单仍由数据库管理，曝光、增益和像素格式仍保留相机自身设置。

本次配置、机器状态与监测服务专项 pytest：13 项通过；全量 pytest：147 项通过、10 项既有失败，失败名单与修改前的 144 项通过、10 项失败一致。新增验证覆盖配置目录路径解析、默认参数、跨文件重复键和无机器时桌面启动。

### 配置类型命名调整

配置类型 `MeasurementConfiguration` 更名为 `AppConfig`，定义仍在 `src/config_util.py`；配置对象作为实例属性时统一改名为 `self.config`，涉及 `src/app.py`、`src/database.py`、`src/machine_manager.py`、`src/camera.py`、`src/frequency_adapter.py`、`src/main.py` 的 `app.config`，以及测试中的 `app.config`、`self.app.config`、`machine_database.config`、`adapter.config`。构造函数形参当时保持 `configuration` 不变，与 `configuration_directory`、`load_configuration()`、`read_configuration_settings()` 同组命名，`load_configuration()` 和测试中的局部变量 `configuration` 也不改名，该命名约定已由下一节 `配置变量命名统一` 取代；`App.__init__` 按项目现有格式补齐 docstring（一句话总结、Args、Returns 返回示例）。本次只做改名和补文档，无行为变化：YAML 配置键、数据库表结构、函数名和文件名均未改动，GUI 展示与后端从配置解析、采集、OCR、频率、存储到 SQLite 写入的数据流保持原样。

全量 pytest：147 项通过、10 项既有失败，与本次改动前的 147 项通过、10 项失败完全一致，失败名单未变（`tests/test_acceptance_scenarios.py` 6 项旧频率字段、旧结果字段与历史频率冲突规则断言，`tests/test_recovery_and_faults.py` 4 项旧频率字段、未等待读数即期望入库、UNKNOWN 状态期望与实例锁异常文案）。自查确认全仓库已无 `MeasurementConfiguration` 与 `self.configuration` 残留，`configuration_directory` 保持原样。

### 配置变量命名统一

子配置类型 `MachineConfiguration` 更名为 `MachineConfig`，与公共配置类型 `AppConfig` 配对，定义仍在 `src/config_util.py`；`AppConfig.machines` 的字段标注、`load_configuration()` 返回示例中的逐机构造、`load_configuration()` 组装机器配置的调用，以及 `src/camera.py`、`src/frequency_adapter.py`、`src/machine_manager.py` 的形参标注和测试中的构造调用同步改名。

表示配置内容的形参、局部变量和测试固件统一改名为 `config`：`App.__init__`、`Database.__init__`、`SessionCamera.__init__`、`FrequencyAdapter.__init__`、`MachineManager.__init__` 的形参及其内部的 `self.config = config`，`load_configuration()` 末尾的局部变量与 `machine_configurations`（改为 `machine_configs`）、打开 YAML 的文件句柄（`configuration_file` 改为 `config_file`），`src/main.py`、`src/service/monitoring_service.py` 的局部变量，以及 `tests/test_fatal_shutdown.py` 等测试固件和局部变量都改为 `config`。表示配置目录或配置路径的 `configuration_directory`、`configuration_path` 保持不变，`load_configuration()`、`read_configuration_settings()`、`write_configuration_files()` 等函数名、相关测试函数名也不改名（配置键 `configuration_version` 已在后续清理中删除），命名按「内容 vs 目录」区分：装配置对象的叫 `config`，装目录或路径的保留 `configuration_` 前缀。

本次只做改名，无行为变化：YAML 配置键、`config/` 目录、命令行 `--config`、数据库表结构、`self.config` 语义和文件名均未改动；数据流动逻辑也不变，`config/` 下五个 YAML 仍由 `read_configuration_settings()` 与 `load_configuration()` 组装成 `AppConfig`，`App` 按其中的启用机器逐台创建相机与频率适配器，START 建立周期并采集图片与频率，OCR 完成编码、筛帧、识别和终选，CLOSE 选取最后有效频率，成功后由存储队列先保存图片再写 SQLite；GUI 展示与后端数据流保持原样。全量 pytest：147 项通过、10 项既有失败，与本次改动前的 147 项通过、10 项失败完全一致，失败名单未变（`tests/test_acceptance_scenarios.py` 6 项旧频率字段、旧结果字段与历史频率冲突规则断言，`tests/test_recovery_and_faults.py` 4 项旧频率字段、未等待读数即期望入库、UNKNOWN 状态期望与实例锁异常文案）。自查确认全仓库已无 `MachineConfiguration` 残留，`configuration` 只剩 `configuration_directory`、`configuration_path`、上述函数名与测试名。

### 相机适配器只接收机器编号

### 相机适配器只接收用到的参数

`SessionCamera.__init__` 不再接收整份配置：机器绑定改为 `machine_id: str`，采集参数改为 `capture_window_ms: int` 与 `camera_timeout_ms: int`，属性同步改为 `self.machine_id`、`self.capture_window_ms`、`self.camera_timeout_ms`，`src/camera.py` 不再导入 `MachineConfig` 和 `AppConfig`。采集失败日志、CAPTURE_COMPLETED 事件的机器归属直接读取机器编号，采集任务的两个参数直接读取毫秒数；构造由 `src/app.py` 传入 `machine.machine_id` 与 `config.capture_window_ms`、`config.camera_timeout_ms`，测试替身 `tests/test_mvs_capture.py` 两处构造直接传字符串与毫秒数。采集流程不变：START 后由唯一采集线程收集整轮帧并一次性交付结果，CLOSE 等待交付结束，OCR 与频率成功后由存储队列先保存图片再写 SQLite。全量 pytest：147 项通过、10 项既有失败，与改动前一致，失败名单未变。

### 配置合并为单一 YAML（配置改造阶段 1）

`config/` 下原 `application.yaml`、`camera.yaml`、`ocr.yaml`、`frequency.yaml`、`machine.yaml` 五个文件合并为单一 `config.yaml`，按业务分为 `application`、`camera`、`ocr`、`frequency`、`machine` 五个顶层段落，键名和取值不变；`read_configuration_settings()` 改为读取该文件并按固定段落顺序把各段合并成与之前完全相同的扁平参数，相对路径仍以文件所在目录为基准，跨段出现同名键时直接报错并报出两个来源段，`AppConfig`、全部 `config.xxx` 调用点、GUI 启动入口和命令行 `--config` 接收配置目录的契约均保持不变。测试支撑 `write_configuration_files()` 改为按项目示例的段落结构拆分参数写单文件，重复键用例改为跨段落重复验证。本阶段只合并文件与调整加载器，代码侧的嵌套子 dataclass 与 `config.camera.capture_window_ms` 式带组访问留待配置改造阶段 2。全量 pytest：148 项通过、10 项既有失败，失败名单与改动前实测基线（同为 148 项通过）完全一致；此前文档记录的"147 项通过"为过期数字，改动前实测即为 148 项通过。

### 配置校验精简

`AppConfig.validate()` 删除两处重复检查：机器绑定不再逐字段比较唯一性，只保留 `camera_serial` 与 `frequency_meter_serial` 非空检查（`machine_id` 由数据库主键保证唯一，两个序列号由 `machine` 表未删除记录的部分唯一索引保证，机器清单经 `MachineRepo.list_enabled()` 读取，重复不可能出现）；删除 `initial_machine_state` 成员检查，非法状态仍由 `read_configuration_settings()` 中的 `MachineState(...)` 转换直接抛出 `ValueError`，`tests/test_machine_state.py` 的非法状态用例继续通过。启动期其余校验保持不变：至少一台启用机器、期限与容量为正数、频率范围递增、磁盘保留空间非负、恢复库与结果库不同文件。数据流不变：`read_configuration_settings()` 读取 `config/config.yaml` 五个段落并转换路径与初始状态，`load_configuration()` 从数据库读取启用机器、组装并校验 `AppConfig`，`App` 按配置逐台创建相机与频率适配器，采集、OCR、频率、存储与 SQLite 写入流程不受影响。全量 pytest：148 项通过、10 项既有失败，与本次改动前实测完全一致，失败名单未变。

### 机器清单改由 App 读取数据库

`AppConfig` 删除 `machines` 字段，`src/config_util.py` 的 `load_configuration()` 更名为 `load_config()`，只读取 `config/config.yaml` 的公共参数并校验运行参数，不再导入 `sqlite3` 与 `MachineRepo`；`App.__init__` 改为先按 `config.database_path` 建好机器表，再 `MachineRepo.list_enabled()` 读取启用机器，逐台构造 `MachineConfig(machine_id=str(row["id"]), camera_serial=..., frequency_meter_serial=...)` 与采集器、频率适配器、机器处理器，机器编号因此是数据库自增编号的字符串。`AppConfig.validate()` 删除机器相关的两条检查：没有启用机器时由 `App.__init__` 抛出「没有启用的机器，请先在机器管理页添加并启用机器。」，GUI 沿用的仍是这句提示；`camera_serial`、`frequency_meter_serial` 非空检查删除，序列号在写入前已由机器管理页与服务层保证。数据流：`load_config()` 读 YAML 公共参数 → `App.__init__` 读业务库启用机器 → 逐台建立处理器 → START 采集图片与频率 → OCR 编码、筛帧、识别、终选 → CLOSE 选取最后有效频率 → 存储队列先保存图片再写 SQLite。测试侧新增 `tests/configuration_support.py` 的 `create_machine_database()`（建表并按顺序插入机器），原先在内存里构造机器清单的 12 处 `App(config)`、全部旧机器编号（`M01` 等）以及 `main.load_configuration` 替身一并改为数据库自增编号与新函数名，`tests/test_fatal_shutdown.py` 的启动失败阶段由「相机序列号为空」改为「机器全部停用」。全量 pytest：148 项通过、10 项既有失败，失败名单与改动前一致。

### 删除容量监控

删除磁盘空间与存储积压的周期性检查：`src/app.py` 移除维护循环 `maintain_system()`、磁盘检查 `check_disk_capacity()`、`import shutil`，启动阶段不再给机器写入容量状态，也不再注册"容量检查"后台任务，退出时不再清空积压计数；`src/machine_manager.py` 移除 `capacity_available` 属性、受理门禁中的容量项和 `CAPACITY_CHANGED` 处理分支，受理 START 只检查相机可用性与采集占用，`acceptance_state` 的 `DEGRADED` 只剩"相机仍被上一轮占用"，`FAULT` 只由相机不可用触发；`src/enums.py` 删除 `EventType.CAPACITY_CHANGED`；`src/database.py` 删除 `runtime_available` 标志（唯一写入者是该维护循环）。配置侧删除 `minimum_free_disk_bytes`、`maintenance_interval_ms`、`max_persistent_records` 三个键与 `AppConfig.validate()` 中的磁盘保留空间检查，`read_configuration_settings()` 与 `load_config()` 的返回示例同步收缩；共享存储队列容量 `storage_queue_capacity` 与队列满失败路径保留，`Database.queued_records` 继续用于同一周期重复入队判断，只是不再被读取长度。数据流：`load_config()` 读 YAML 公共参数 → `App.__init__` 读业务库启用机器 → 逐台建立处理器 → START 采集图片与频率 → OCR 编码、筛帧、识别、终选 → CLOSE 选取最后有效频率 → 存储队列先保存图片再写 SQLite；启动和运行期间不再有容量计算与容量事件。测试侧删除 `tests/test_recovery_and_faults.py::test_disk_capacity_blocks_new_cycles_and_recovers`（专测被删功能），`tests/test_fatal_shutdown.py` 改用 `EventType.MACHINE_STARTED` 作为填满事件队列的占位事件，并注明该用例取消的是最后注册的共享存储任务。全量 pytest：147 项通过、10 项既有失败，与改动前实测的 148 项通过、同一 10 项失败名单一致，减少的 1 项即被删除的容量用例。

### App 构造只组装依赖，机器与相机在启动阶段准备

`App.__init__` 不再访问业务库，只保存 `config`、创建共享存储与运行状态（`Database`、`TextRecognizer`、`asyncio.Event` 与故障、退出标志）；业务库目录、机器表、启用机器读取与逐台处理器建立移入新增的 `App.create_machine_managers()`：确保业务库目录与机器表存在 → `MachineRepo.list_enabled()` 读取启用机器 → 逐台构造 `MachineConfig`、`SessionCamera`、`FrequencyAdapter`、`MachineManager`，由 `start()` 在初始化图片目录与双库之后用 `run_blocking_operation()` 在后台线程调用。没有启用机器时改由该方法抛出「没有启用的机器，请先在机器管理页添加并启用机器。」，`start()` 的启动失败路径照旧记录日志、释放实例锁后向上抛出，GUI 与命令行拿到的仍是同一句提示；机器清单因此以启动时刻为准，构造之后新增的启用机器同样生效。数据流：`load_config()` 读 YAML 公共参数 → `App(config)` 只组装共享依赖 → `start()` 初始化图片目录与双库、按业务库机器表建立逐机处理器、加载 SDK 并打开相机 → START 采集图片与频率 → OCR 编码、筛帧、识别、终选 → CLOSE 选取最后有效频率 → 存储队列先保存图片再写 SQLite。测试侧改为自写用例：`tests/local_test_support.py` 提供独立的配置组装、机器库写入与相机替身，`tests/test_app_start_assembly.py`（4 项）覆盖构造不创建业务库目录与文件、`create_machine_managers()` 只登记启用机器并绑定序列号、没有启用机器时 `start()` 抛出业务异常且不加载 SDK、启动读取构造之后新增的启用机器并打开相机；`tests/test_app_measurement_cycle.py`（1 项）用固定读数频率仪替身替换尚未实现的识别黑盒，跑完整轮闭环，核对两台机器各自落盘图片、SQLite 记录、最终频率等于最后交付读数、相机退出后关闭与实例锁释放。工作区现状：`tests/` 下既有用例已从磁盘删除（`git status` 显示为 `D`，`git restore tests/` 可恢复），当前全量 pytest 即自写 5 项、全部通过；删除前实测全量为 102 项通过、27 项失败、32 项错误，改动前基线为 147 项通过、10 项失败（`runtime/baseline-before-plan-b.log`），新增红全部来自「在 `start()` 之前读取或改写 `app.machine_managers`」的用例，适配方式为注入前调用 `app.create_machine_managers()`，或把替身替换改为对类打补丁（如 `app.MachineConfig`、`FrequencyAdapter.listen_measurements`）。现场相机验证：`runtime/plan-b-check/check_real_camera.py` 配 `runtime/plan-b-check/config.yaml` 与独立业务库实测——构造不读机器 → `start()` 读取机器并打开现场相机 DB2107795 → 一轮 START 真实取帧 87 帧、采集耗时 1.047 s → 因 OCR 模型未实现（`OCR_MODEL_NOT_IMPLEMENTED`）本轮按预期失败且不触发应用退出 → `stop()` 后相机关闭、实例锁释放、无遗留故障；改用项目配置启动会在机器 2（序列号 212）处整体拒绝启动，业务库中另有两条占位机器记录需停用或删除。

### 启动阶段的阻塞调用改为同步执行

`App.start()` 中并发任务创建之前的五处阻塞调用不再经过 `run_blocking_operation()`：`self.config.evidence_directory.mkdir(parents=True, exist_ok=True)`、`self.database.initialize()`、`self.initialize_machines()`、`self.camera_sdk = load_mvs_sdk(...)` 与 `machine.camera.sdk_camera = self.camera_sdk.open_camera(...)` 全部改为直接同步调用，并在这几处各留一行注释说明按同步方式执行。理由是这些调用都发生在并发任务注册之前（机器事件、频率监听与共享存储任务在本函数末尾才创建），事件循环上没有其他协程在等，同步执行不耽误并发；另外取消落在包装调用上时，包装函数是"等线程结束再抛 `CancelledError`"，抛出的那一刻赋值语句不会执行——取消落在加载 SDK 时 `self.camera_sdk` 保持 `None`，这次 `MV_CC_Initialize()` 就没有对应的 `MV_CC_Finalize()`；取消落在打开相机时 `machine.camera.sdk_camera` 保持 `None`（这一条由 `camera_sdk.close()` 兜住，它会关闭 SDK 内部登记的全部相机）。同步调用一定返回并完成赋值，后续清理路径能正常关闭 SDK 与相机。代价是启动期间的取消不再逐台中断：`start()` 会把 SDK 加载和全部相机打开做完，取消在调用方的第一个 await 处才生效并接着走清理；响应快慢本身不变，因为包装函数本来也要等线程跑完才抛取消。`start()` 里另外两处包装保留不动：`publish_event()` 中未知机器事件写审计（运行期间全流程并发，运行库是 WAL 加 `synchronous=FULL`，写入带 fsync），以及 `release_resources()` 中的 `camera_sdk.close()`（清理任务由 `report_failure()` 与 `stop()` 各自 `asyncio.create_task` 独立运行，可能与其他协程同时存活）。`src/async_utils.py` 与 `run_blocking_operation()` 本身未改动，`src/app.py` 仍保留该导入，`src/app.py` 里 `self.database.close()` 继续在 `finally` 中同步执行，保证关闭连接与释放实例锁不被取消打断。数据流：`load_config()` 读 YAML 公共参数 → `App(config)` 只组装共享依赖 → `start()` 同步建图片目录与双库、同步读业务库机器表并逐台建立处理器、同步加载 SDK 并逐台打开相机 → START 采集图片与频率 → OCR 编码、筛帧、识别、终选 → CLOSE 选取最后有效频率 → 存储队列先保存图片再写 SQLite → 退出时关闭相机并释放实例锁。全量 pytest：5 项通过（`tests/test_app_start_assembly.py` 4 项与 `tests/test_app_measurement_cycle.py` 1 项，启动装配用例覆盖没有启用机器时抛出业务异常、启动读取构造之后新增的启用机器并打开相机）。

### 每机运行对象改名为 Machine

`src/machine_manager.py` 更名并移到 `src/machine.py`，类 `MachineManager` 改名为 `Machine`：每台机器只有一个实例，它本身就是这台机器在程序里的运行对象（唯一事件队列、当前周期、相机、频率适配器与受理状态都在它身上），原来的 `-Manager` 后缀在全仓库独一无二，和 `FrequencyAdapter`、`TextRecognizer`、`MachineRepo` 这类具体名词不一致。配套改名避免出现 `machine.machine`：构造参数与属性 `self.machine`（`MachineConfig`）统一改为 `machine_config`，`App.machine_managers` 改为 `App.machines`，`App.create_machine_managers()` 改为 `App.initialize_machines()`（保留 `机器管理页新增机器` 与启动期建立运行对象的区分），`src/app.py` 中循环变量与局部变量统一为 `machine`（运行对象）与 `machine_config`（配置），原先 `machine = manager.machine`、`machine_manager.machine.machine_id` 这类读法随之消除；注释措辞统一为「机器运行对象」，不再混用机器管理器/机器管理员/逐机处理器，`src/camera.py` 的交付注释改为「交回所属机器」。`src/repo/machine_repo.py`、`machine` 表、`MachineConfig`、`MachineState` 与 `FrequencyAdapter` 内部持有配置的 `self.machine` 均未改动，`ui/` 不引用该类。数据流不变：`load_config()` 读 YAML 公共参数 → `App(config)` 只组装共享依赖 → `start()` 同步建图片目录与双库、`initialize_machines()` 读业务库机器表并逐台建立 `Machine`、同步加载 SDK 并逐台打开相机 → START 采集图片与频率 → OCR 编码、筛帧、识别、终选 → CLOSE 选取最后有效频率 → 存储队列先保存图片再写 SQLite → 退出时关闭相机并释放实例锁。全量 pytest：5 项通过（原 `test_create_machine_managers_binds_enabled_machines` 更名为 `test_initialize_machines_binds_enabled_machines`，断言改为 `application.machines` 与 `machine_config`）。

### 采集适配器改名为 Camera

`src/camera.py` 的类 `SessionCamera` 改名为 `Camera`：`Session` 在本项目专指测量周期（`BeltSession`、`SessionState`、`session_id`、`current_session`），而这个对象是启动时按机器创建、跨所有周期长期存在的采集适配器，前缀把生命周期说反了；改名后它与 SDK 原始句柄的关系和 `Machine` 与 `MachineConfig` 一致。调用点不用动：`machine.camera.*`（`src/machine.py` 16 处、`src/app.py` 2 处、测试 3 处）本来就按属性名访问，只改了类定义、`src/app.py` 与 `src/machine.py` 的导入、`App.initialize_machines()` 中的构造和 `Machine.__init__` 的类型标注，文件名 `src/camera.py` 保持不变。同一文件内 `CaptureTask` 持有的 SDK 相机对象字段 `camera` 与 `Camera` 适配器同名易混，连同适配器上的同名属性一起改名为 `sdk_camera`：字段名显式标出这是厂商那一层的对象，避免与 `Camera` 适配器、`MvsCamera.handle`（真正的连接句柄）混淆，也没有采用会重复类型名的 `mvs_camera`。改名涉及 `CaptureTask` 的字段定义、`run_capture()` 内 9 处 `self.sdk_camera`、`start_capture()` 的局部变量与构造参数、结果交付处的 3 处引用，以及 `Camera.__init__` 的属性定义、`available` 与 `is_capturing` 属性、`src/app.py` 的赋值、`src/machine.py` 的 2 处和测试的 3 处断言；`camera_timeout_ms` 配置参数名未动。数据流不变：`load_config()` 读 YAML 公共参数 → `App(config)` 只组装共享依赖 → `start()` 同步建图片目录与双库、`initialize_machines()` 读业务库机器表并逐台建立 `Machine`、同步加载 SDK 并逐台把 `MvsCamera` 交给 `Camera.sdk_camera` → START 由 `Camera.start_capture()` 建采集任务并在线程中收集整轮帧 → OCR 编码、筛帧、识别、终选 → CLOSE 选取最后有效频率 → 存储队列先保存图片再写 SQLite → 退出时先 `Camera.stop()` 停流，再由 `camera_sdk.close()` 关闭全部相机。全量 pytest：5 项通过。
