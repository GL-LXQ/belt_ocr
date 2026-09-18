# 多皮带机并行采集与 OCR 系统：Codex 开发规格

> 当前已接入 MVS 相机与 Session 流程；OCR 支持内存图片批次及终选占位入口；频率采用设备黑盒接口，接收及关闭结算与数据库存储已接通，设备协议内部待实现。支持正常结果 SQLite 保存和设备审计；异常、中断及提交失败只打印日志，不自动补交；重启不恢复旧 Session。
> 启动方法、配置说明、信号入口及当前边界见 [运行说明](USAGE.md)。
> 下文保留完整开发规格，不表示所有生产能力均已交付。

海康 MVS 模块已接入 App，使用方法见 [运行说明](USAGE.md) 和 [MVS 采集说明](MVS_CAPTURE.md)。文件夹模拟采集已移除；测试通过假 SDK 验证真实适配器。任一已配置机器缺少相机序列号或无法连接相机时，整个程序启动失败。

当前系统的数据流：START 创建独立 Session，登记 frequency_adapter.active_session_id 并启动相机内存图片组批；OCR 按批次回传原始文字块，采集封口且批次结算后只触发一次文字和图片终选占位入口，当前不保存图片。频率监听黑盒负责设备连接、新有效测量识别和固定接收时的周期归属，按接收顺序将 FrequencyMeasured 与 START/CLOSE 送入同一机器 FIFO 队列；业务层仅追加 measurement_frequencies，不按设备测量时间重排，也不重复校验黑盒已保证的数据。处理 CLOSE 时立即清空适配器的活动周期、封闭列表并取最后一条作为最终频率；频率状态统一使用 FrequencyState 枚举，区分 RUNNING（采集中）、SUCCESS（成功）和 FAILED（失败）；没有有效测量、频率故障或周期中断时保留明细且最终值为空。CLOSE 前入队的测量先处理，CLOSE 后不等待设备、不补收旧轮数据。频率明细 JSON、final_frequency_hz 和完整 payload 在同一 SQLite 事务中写入，整轮记录仍等待 OCR 等原有完成条件。当前 listen_measurements 按 frequency_interval_ms 循环读取 simulated_frequencies_hz 产生联调测量，每次分配独立身份，无活动 Session 时不交付；真实设备协议仍待替换，读取异常报告故障；旧库升级保留历史冻结内容和哈希，重启不恢复旧 Session。

结算数据流：START 创建 `SessionState.RUNNING` 的 Session，机器独立登记 `active_session_id`；START 受理时记录本轮相机采图起始时间 `capture_start_time`；CLOSE 记录停止截止时间 `capture_stop_time`、封闭频率列表并停止相机生产（两个时间均为主机单调时钟秒数），释放机器活动位置，Session 仍为 RUNNING。相机采图及图片批次交付结束（is_capture_finished 为 True）、OCR 批次结算、OCR 与频率成功且证据验证通过后，冻结正常结果并进入 WAITING_COMMIT_DB；数据库确认成功后置 COMMITTED 并移除档案。整轮处理失败、中断、队列满或提交失败均置 FAILED，只打印日志并释放资源，不生成异常测量记录、不暂存或自动补交。关闭前失败保留本轮活动身份与关闭期限，真实 CLOSE 或周期中断后再移除；旧轮事件不改变新轮身份。

本轮采集汇总保存在 `capture_summary`；供文字和图片终选使用的原图统一保存在 `images_for_final_selection`，批次结果保存在 `recognition_results`；已移除未填充的 `selected_frames` 容器及其提交字段。OCR 与周期超时继续由 `schedule_timeout` 创建的期限任务触发，不再在 Session 中保存未读取的 `ocr_deadline`、`cycle_deadline` 时间字符串。

测量数据由 START 创建 Session，经相机内存组批、OCR 终选和 CLOSE 频率结算，在证据验证通过后冻结并写入 SQLite。Session 不再保存 `process_epoch`、`configuration_snapshot`，新写入的 `payload_json` 不再保存这两个键及 `software_version`、`model_version`、`outcome`、`is_simulated`，配置版本直接从应用配置读取；初始化旧库时删除 `outcome`、`is_simulated` 独立列，历史记录的 JSON 和哈希保持不变。图片以 `frame_id` 关联内存内容，已删除未使用的图片路径、来源批次、接收时间和 OCR 结果帧编号列表。本次仅清理字段与废弃配置，频率监听和自动启停入口另行处理。

采集完成事件 CaptureSealed 由 MachineManager.handle_capture_finished 处理，登记采集结束状态、统计和错误，再进入批次结算与文字终选检查。事件类型统一由 enums.EventType 定义，发送端构造带枚举类型的 MeasurementEvent，处理端按枚举分派；枚举值保留原事件字符串，审计与序列化格式保持兼容。事件处理入口：MachineManager.listen_events 持续读取本机 FIFO 队列，将事件交给 handle_event 并反馈处理结果；MachineManager.handle_event 按 FIFO 顺序处理机器及 Session 事件并检查测量完成条件，START/CLOSE 不按事件创建时间过滤，关闭事件通过 is_close_event_for_active_session 核对当前活动或已中断周期的身份，无周期编号时允许关闭或复位，不匹配时记录审计并忽略。启停处理入口：MachineManager.handle_machine_start 接收启动处理请求，创建 Session 并启动相机采集、登记频率归属和超时任务；MachineManager.handle_machine_close 处理正常关闭或 interrupted=True 的异常中断，停止现场采集并结算频率，正常关闭后继续等待 OCR 和证据处理，结果完整后提交 SQLite，失败则记录日志并清理资源。

异常处理：仅将原本全英文的 SDK 报错补充为简明中文，保留接口名和错误码；已有中文及中英混合提示保持不变。设备异常仍沿原有事件流程交付本机处理器，更新测量状态并清理资源。

初版故障数据流：启动时任一已配置相机、驱动或数据库初始化失败即记录日志并释放已打开资源；运行中相机采集、编码、频率监听或后台任务异常通过 App.report_failure 记录模块、机器、设备身份和原始异常堆栈，关闭信号入口并统一停止全部机器。主流程同时等待测量完成与故障通知，故障打断等待后清理 Session、队列、线程、相机 SDK 和实例锁，命令行以退出码 1 结束。已删除设备故障/恢复事件、健康恢复接口及后台自动重启配置；正常取帧超时、暂时无频率读数不退出，单轮 OCR 失败及单次入库失败仍按原规则清理本轮。修复设备后手动重启，不续办旧 Session。

## 1. 项目目标与边界

在 Python 工程中实现一套正式生产系统：一台工控机同时管理三台皮带机，每台机器绑定自己的工业相机、启动/关闭输入通道和可唯一识别的频率采集通道。

一次正常的“启动 → 测量 → 关闭”对应一个 BeltSession。启动信号创建 Session；相机/OCR、频率采集、IO 监听独立工作；关闭信号结束本次现场采集；数据完整后生成一条最终测量记录。

核心约束：

- 全文业务事件统一使用 START（启动）和 CLOSE（关闭）。CLOSE 专指正常测量周期结束，不代表程序退出、通信断开、设备掉电或故障。
- 本软件负责观察、采集、识别、汇总和存储，不负责驱动皮带机启停，不替代设备自身的安全回路。
- 一台机器同时最多有一个现场活动 Session，但可以有多个已经关闭、仍在后台处理的 Session。
- 三台机器互不等待业务结果；共用 OCR 或存储资源时允许排队，但必须有容量限制和超限处理。
- machine_id 决定机器归属；session_id 决定本次测量归属。所有异步结果必须携带明确的 session_id。
- 设备驱动和工作线程不得直接修改 Session，只发送事件；Session 状态由所属机器的业务处理器串行修改。
- 正常结果完整后统一入库。失败、漏采和中断只打印包含机器与周期身份的日志，不写入测量数据库。

## 2. 首版明确采用的业务规则

### 2.1 图像采集窗口

START 后，为本次 Session 连续采集配置时长的图像。首版默认 `capture_window_ms = 1000`，可通过配置调整。

采集在以下两个条件中先发生者处结束：

1. 配置的采集时长到达；
2. 收到本 Session 的 CLOSE。

采集时长到达只结束图像窗口，不结束机器测量周期。OCR 可以在 CLOSE 前完成，也可以在 CLOSE 后完成。不得因为识别到一个看起来正确的字符串就提前把整个 OCR 任务判定成功。

1000 ms 是本规格采用的工程初值，不是对现场可识别性、设备帧率或处理能力的承诺。

### 2.2 频率选择

频率接收器持续运行。START 到 CLOSE 之间按程序接收顺序保存本轮所有新有效测量，最终采用最后收到的一条有效测量，不平均，也不按设备测量时间重排。相同频率值的新测量分别保留，同一测量的重发由设备黑盒过滤。

关闭前的频率是候选值，可用于界面显示；处理 CLOSE 时立即封闭列表并冻结最终值，频率有效且本轮无频率故障时置 `frequency_state = FrequencyState.SUCCESS`，不设置关闭后的等待期。

### 2.3 正常完成

以下两个模块状态均成功，且采集封口、批次结算、证据验证完成后才能提交：

```text
ocr_state == OCRState.SUCCESS
frequency_state == FrequencyState.SUCCESS
```

其中：

- `ocr_state == OCRState.SUCCESS`：图像窗口已封口，选中的图像任务全部结算，后处理成功，最终 OCR 与证据已确认。
- `frequency_state == FrequencyState.SUCCESS`：频率窗口已封口，最终频率已选定并通过有效性检查。
频率只在正常 CLOSE 且存在有效测量时设为 SUCCESS，结算不再重复检查关闭时间。

“线程已经返回”“OCR 返回空字符串”“仪器仍显示上一次读数”均不等于业务完成。

## 3. 总体架构

```text
现场设备
│
├─ 数字 IO 模块 ── IOAdapter ── 启动/关闭/输入异常事件
├─ 三台工业相机 ── CameraWorker ── 图像/采集封口事件
└─ 频率采集通道 ── FrequencyAdapter ── 频率测量/失败事件
                                      │
                                      ▼
                                EventRouter
                                      │
                  ┌───────────────────┼───────────────────┐
                  ▼                   ▼                   ▼
             MachineManager 1      MachineManager 2      MachineManager 3
                  │                   │                   │
             本机 Session        本机 Session        本机 Session
                  └───────────────────┼───────────────────┘
                                      │
                         有界 OCR 调度器 / OCR Worker
                                      │
                          行级识别 / 跨帧融合 / 证据选择
                                      │
                             结果事件返回所属 机器管理员
                                      │
                              完成检查 / 冻结记录
                                      │
                                DBWriter
                                      │
                         正常测量记录 / 失败日志
```

`MachineManager` 负责机器注册、路由和健康状态；每台 `MachineManager` 是本机所有 Session 状态的唯一修改者；`SessionManager` 是创建、状态更新、完成检查等业务规则的实现，可由各 MachineManager 调用，不再另设一套并发修改入口。

## 4. 设备绑定与配置

### 4.1 示例映射

| 机器 | 相机 | 启动输入 | 关闭输入 | 频率通道 |
|---|---|---|---|---|
| M01 | CAM01 | DI0 | DI1 | FREQ01 |
| M02 | CAM02 | DI2 | DI3 | FREQ02 |
| M03 | CAM03 | DI4 | DI5 | FREQ03 |

以上是软件配置示例，不是要求改变现场接线。实际地址、输入有效电平、设备序列号及协议参数使用项目实际配置。

一块 IO 模块可以承载多台机器的输入。频率通道可以来自独立仪器，也可以来自能明确区分通道的设备；不能将无来源标识的一路频率数据靠猜测分给三台机器。

### 4.2 必需配置

```text
machine_id
camera_device_id / camera_connection / camera_parameters
io_device_id / start_channel / close_channel / input_mode / active_level
frequency_source_id / frequency_connection / measurement_identity_rule
capture_window_ms
max_frames_per_session / frame_selection_policy
ocr_quality_thresholds / matching_thresholds
frequency_validity_rules
io_poll_interval_ms / io_debounce_ms
ocr_result_timeout_ms / max_cycle_open_ms
max_pending_sessions_per_machine / queue_capacities
storage_paths / database_connection
retry_policy
```

不要在业务代码里写死 IP、端口、串口名或设备序号。启动时校验机器 ID 唯一、相机绑定唯一、频率路由不冲突、IO 映射合法、路径可写以及所有必需配置齐全。具体 SDK 与设备协议必须使用已有驱动或厂商正式接口，不能凭空定义一个读取函数并当作设备已接通。

## 5. 系统启动流程

```text
启动程序
   ↓
读取与校验配置
   ↓
初始化日志、事件路由和数据库，获得独占锁后移除旧版本检查点表并清理待提交记录
   ↓
建立 M01 / M02 / M03 的 MachineContext 和 机器管理员
   ↓
连接 IO、相机、频率通道，加载 OCR 引擎
   ↓
检查设备状态、存储可用性和任务队列容量
   ↓
确认本次运行从空 Session 集合开始
   ↓
读取 IO 初始状态，建立基线
   ↓
分别确定各机器是否可接受新周期
   ↓
启动持续监听与处理任务
```

初始状态处理：

- 机器处于明确的关闭状态：进入可接收启动状态。
- 程序启动时机器已经处于启动状态：不凭空创建一条从中途开始的正常 Session；标记 `WAIT_CYCLE_RESET`，等待本轮关闭后再接收下一次启动。
- 输入不可判定或通信不正常：标记 `UNKNOWN` / `FAULT`，禁止创建正常采集 Session。

重启策略：放弃上次运行未完成的 Session、OCR 和待提交记录，不生成旧周期的中断结果；启动时运行中或状态未知的机器等待明确关闭或状态同步，再接收下一次启动。当前模拟实现使用 initial_machine_state 配置初始状态，硬件接入时需使用真实现场状态。

## 6. IO 输入如何变成业务事件

IOAdapter 负责读取输入、通信健康判断、有效电平解释、去抖、状态/边沿识别和事件去重。

```text
读取新鲜且完整的 IO 状态
   ↓
确认通信健康、输入组合有效
   ↓
根据配置解释启动与关闭信号
   ↓
去抖 / 确认稳定状态或有效脉冲
   ↓
与上一有效状态比较，保留来源顺序
   ↓
产生 MachineStarted 或 MachineClosed
   ↓
根据 machine_id 进入对应机器事件队列
```

规则：持续有效的启动输入不能不断创建 Session；持续有效的关闭输入不能反复关闭旧 Session。正常持续时间、脉冲宽度、轮询周期和去抖时长按设备配置匹配，不能假设任何短脉冲都一定能被轮询读到。

输入模式必须明确：保持型状态与瞬时脉冲采用各自的解释规则，不能混用。业务层不处理电平，只处理标准化 START/CLOSE。

现场 IO 尚未接入；接入后通信故障应直接通知 App.report_failure，记录日志并停止整个程序，不转换为正常 CLOSE，也不在本次运行中自动重连。

## 7. START：创建并启动一次测量

收到 `MachineStarted(machine_id)` 后，在该机器 机器管理员 中按顺序执行：

1. 按接收顺序处理启动事件，检查机器同步状态和采集能力。
2. 如果已有现场活动 Session，按重复/异常启动处理，不覆盖原 Session。
3. 如果设备不可用或积压超限，记录本轮未受理事件并报警，等待本轮关闭；不得假装已经采集成功。
4. 生成全局唯一 `session_id`，创建 Session，固定 machine_id、设备绑定和开始时间。
5. 注册到该机器的 Session 集合，将 `active_session_id` 指向新 Session。
6. 请求写入最小运行记录，建立本 Session 的证据目录/索引。
7. 向相机发出 `StartCapture(session_id, capture_id)`。
8. 将频率适配器的 `active_session_id` 设置为本轮编号。
9. 返回事件循环，继续处理事件，不等待 OCR 或频率结果。

```text
Machine START
     ↓
检查可接收性与重复事件
     ↓
创建 Session_A，绑定机器与设备
     ↓
设置 active_session_id = Session_A
     ↓
┌───────────────────┬─────────────────────┬────────────────────┐
│ Camera 采集窗口    │ Frequency 测量窗口 │ IO 持续监听        │
│ 采图 → OCR        │ 新测量 → 候选值    │ 等待本机 CLOSE     │
└───────────────────┴─────────────────────┴────────────────────┘
```

这里“打开频率窗口”指允许本周期接收测量，不是每次启动都重新打开串口或重连仪器。

## 8. 相机采集与图像归属

每台相机独立执行采集命令，不能因为共享 OCR 队列繁忙而延迟到错误的现场时刻才开始采图。

每帧至少携带：

```text
machine_id, session_id, capture_id
camera_id, frame_id
captured_at, captured_monotonic
image_data
```

`capture_id` 区分不同采集任务，`frame_id` 关联本轮原图与识别结果。归属必须在采集窗口内确定，后续 OCR 结果只沿用这个归属。

开始新窗口时建立帧序号/时间戳基线，按 SDK 能力清理或识别旧缓冲。不能把上一次留在缓冲区的图像当作本次第一帧。设备时间戳与主机时间不在同一时间基准时，必须先建立可验证映射，不能直接比较。

采集窗口结束条件：配置时长到达或本 Session CLOSE，先发生者生效。结束后发送 `CaptureSealed(session_id)`。该事件表示不再增加本窗口图像，不表示 OCR 已完成。

已明确属于旧窗口的延迟回调可完成处理；关闭边界以后产生的帧不计入旧 Session；归属无法证明的帧进入异常记录，不写入下一 Session。迟到的旧 `StopCapture(capture_id)` 不得关闭新 Session 的采集。

图像队列必须有上限。按固定、可配置且可追溯的策略选择帧，保留时间覆盖和质量信息，记录跳帧数量；禁止无限累积全帧内存。达到容量限制不能阻塞 IO 监听。

## 9. OCR 主流程与完成条件

```text
带 Session 归属的图像
   ↓
图像粗筛：空帧、曝光、清晰度、有效区域
   ↓
文字检测
   ↓
无文字区域 / 严重截断区域过滤
   ↓
按行识别
   ↓
保留 text + bbox + score + frame_id + 时间 + 图像引用
   ↓
跨帧后处理
   ↓
最终文字 / 行或字段结构 / 融合分数 / 证据图
   ↓
OCRCompleted(session_id, result)
```

“无文字”由文字检测结果判定，不能要求图像粗筛阶段凭空知道文字内容。文字被截断的判断结合检测框与有效图像边界，不应因一个检测框靠边就无条件删除整张图的其他有效文字。

后处理顺序：

1. 过滤低质量识别候选，保留原始结果供追溯。
2. 按位置、方向、时序建立候选行之间的对应关系。
3. 在可能对应同一实体文字的候选中做字符串相似度聚类。
4. 结合多帧支持度、识别分数和图像质量进行融合。
5. 去除同一实体的重复候选，不能把不同位置的合法重复文字全部删除。
6. 输出最终行/字段结构；字段规则不明确时保留有序行，不编造产品型号解析规则。
7. 选择清晰、完整、与结果相符的证据图，完成可读取的证据保存。

融合分数是工程分数，未校准时不得当作“正确概率”。不能默认全 Session 只有一行文字，也不能将所有识别行直接拼成一个字符串。

必须同时满足以下条件才发送成功的 OCRCompleted：

```text
CaptureSealed 已收到
本 Session 选中帧的所有 OCR 子任务已终态结算
没有未计入的在途子任务
最终后处理完成
存在满足质量条件的结果
证据已保存并能够引用
```

空结果、全部模糊、缺失证据、处理失败和超时均返回显式失败原因，不将 `ocr_state` 置为 `OCRState.SUCCESS`。

## 10. 共享 OCR 调度

首版使用一个共享 OCR 调度入口，默认一个 OCR 推理 Worker。每个 OCRJob 包含 `job_id, machine_id, session_id, frame_id, image_ref, attempt_id`。

按机器轮转并在机器内按 Session 顺序调度，避免一台机器大量帧长期占满队列。任务有界；OCR Worker 不持有可修改的 Session 引用，只返回结果事件。

同一 job/frame 的重试结果必须去重，避免在跨帧投票中重复计算。并行 Worker 数量只能作为配置扩展，不得默认启动三份模型或未经确认并发调用同一个模型实例。

设备采集并发不等于推理吞吐无限。达到积压阈值时执行明确的拒收/异常策略，不承诺任何负载下都不会等待。

## 11. 频率采集与归属

现场操作规则为操作员完成频率调整后再发送 CLOSE。软件按程序接收顺序划定测量窗口，CLOSE 后不等待设备，也不补收旧周期数据。

`FrequencyAdapter.listen_measurements()` 是设备黑盒，负责连接、持续读取、有效性检查、新测量去重、来源确认、旧缓冲处理和资源释放，当前内部按配置生成联调读数，真实协议仍待替换。黑盒在接收时固定 active_session_id，无活动周期则不交付；有效测量按接收顺序立即通过 publish_event 入队，不创建延迟交付任务。频率值有效性和测量身份仅在黑盒边界处理，业务层不重复检查。

频率事件与 START/CLOSE 在同一事件循环中按接收顺序进入同一机器 FIFO 队列。排在 CLOSE 前的 FrequencyMeasured 先追加到 measurement_frequencies；处理 CLOSE 时清空适配器活动周期、封闭列表，并取最后一条为最终频率。排在 CLOSE 后的旧轮测量进入迟到审计，不修改旧轮，也不改绑新轮。

每条 FrequencyMeasurement 保留测量身份、设备测量时间、接收时间和频率值。设备测量时间仅供追溯，不用于重新排序或关闭后补收。没有有效测量、读取故障或周期中断时保留已收到的明细，final_frequency_hz 为空并记录错误。

数据库 measurements.measurement_frequencies 为按接收顺序保存的完整测量对象 JSON 列表，与 final_frequency_hz 及 payload_json 一起事务写入。旧库升级继续沿用历史明细回填规则，不改变已有 payload、最终值和哈希。

## 12. CLOSE：关闭现场窗口，释放活动位置

收到 `MachineClosed(machine_id)` 后，在对应 机器管理员 中执行：

1. 找到本机 `active_session_id`。不存在时按重复关闭或同步事件处理，不随意关闭其他待完成 Session。
2. 检查事件属于当前周期；重复事件不重复执行。
3. 为本 Session 设置采集截止边界；未失败的 Session 保持 `RUNNING`。
4. 关闭本 Session 图像窗口；已自然结束则幂等处理。
5. 立即封闭本 Session 频率列表，按接收顺序确定最终频率。
6. 仅当 active_session_id 仍等于该 Session ID 时清空活动位置。
7. 未失败的 Session 继续后台收尾；已经 FAILED 的 Session 在关闭后移除。
8. 执行一次完成检查；后续结果到达时再次检查。

```text
CLOSE Session_A
     ↓
记录关闭边界，封闭图像与频率窗口
     ↓
Machine.active_session_id = None
     ↓
┌───────────────────────────────────┐
│ Session_A：继续后台 OCR / 结果收尾 │
│ Machine：可接收下一次正常 START   │
└───────────────────────────────────┘
```

正常关闭保留未失败 Session 的已入队 OCR，不能提前销毁仍需后台处理的旧 Session。允许新周期的前提仍包括相机已可接受下一采集命令、设备健康与积压未超限；不能因为旧 OCR 没结束而直接拒绝新周期。

特别禁止在旧 Session 入库回调中无条件执行 `machine.active_session_id = None`。释放活动位置由 CLOSE/明确中断路径负责，旧结果和旧提交回调不得改变新 Session 的绑定。

## 13. 数据模型与唯一状态来源

### 13.1 MachineContext

```text
machine_id
camera_binding / io_binding / frequency_binding
observed_machine_state
acceptance_state
active_session_id: Optional[str]
uncompleted_sessions: dict[session_id, BeltSession]
last_io_sequence / device_health / queue_load
```

机器可接收状态：

| 状态 | 含义 |
|---|---|
| INITIALIZING | 初始化中 |
| READY | 已同步、无活动周期、具备采集能力 |
| ACTIVE | 本机当前有现场活动 Session |
| WAIT_CYCLE_RESET | 当前周期不能完整接收，等待明确关闭后重新同步 |
| DEGRADED / FAULT | 设备、通信、存储或容量异常，按故障范围限制接收 |

`READY` 不要求本机旧 Session 全部入库，只要求新周期采集条件满足。

### 13.2 BeltSession

```text
session_id, machine_id
camera_id, frequency_source_id
start_time, finish_time
capture_start_time, capture_stop_time

state: SessionState.RUNNING | WAITING_COMMIT_DB | COMMITTED | FAILED
capture_id, is_capture_finished
selected_frame_ids, pending_ocr_job_ids
ocr_state: OCRState.WAITING | RUNNING | SUCCESS | FAILED | TIMED_OUT
ocr_result, evidence_refs

frequency_window_sealed
measurement_frequencies
frequency_state: FrequencyState.RUNNING | SUCCESS | FAILED
final_frequency_hz, final_measurement_id

frozen_payload, payload_hash
errors, timestamps, deadlines
```

Session 只维护 `state`，不再维护 `cycle_state`、`outcome` 和 `commit_state`。不再保存 Session 的关闭时间，正常关闭后才允许频率状态进入 SUCCESS；机器是否可接受新周期由 MachineManager 独立管理。OCR 和频率仍各自使用 OCRState、FrequencyState，不能用机器当前状态判断旧 Session 是否关闭。

| SessionState | 含义 |
|---|---|
| RUNNING | 现场采集中，或等待 OCR、频率与证据处理完成 |
| WAITING_COMMIT_DB | 正常结果完整，等待或正在提交数据库 |
| COMMITTED | 正常结果已确认入库 |
| FAILED | 整轮处理、中断或数据库提交失败，打印日志并清理 |

`finished` 为 `state == SessionState.COMMITTED` 的只读属性。数据库初始化时移除旧表的 `outcome`、`is_simulated` 列，新记录也不包含这两个键及 `model_version`；历史 payload 和哈希保持不变。新建测量表和 payload 均不包含 `close_time`；已有数据库不做删除该列的迁移。

业务时间用可追溯时间保存；进程内超时和排序使用单调时钟。进程重启后放弃旧任务和旧期限，不使用上一进程的单调时钟值继续计时。

## 14. 标准事件、命令与路由

### 14.1 事件公共字段

```text
event_id, event_type
machine_id
session_id: Optional[str]
occurred_at, received_at
payload
```

START 原始事件没有 session_id，由业务层创建。其余异步业务结果必须携带 session_id；设备级健康事件可以不携带。

### 14.2 最小事件集合

```text
MachineStarted / MachineClosed
FrameBatchSelected / CaptureSealed / CaptureFailed
OCRFrameCompleted / OCRFrameFailed
OCRCompleted / OCRFailed
FrequencyMeasured / FrequencyFailed
SessionTimeout
CommitSucceeded / CommitFailed
```

### 14.3 最小命令集合

```text
StartCapture(machine_id, session_id, capture_id, parameters)
SealCapture(machine_id, session_id, capture_id, capture_stop_time)
SubmitOCRJob(job_id, machine_id, session_id, frame_id, image_ref)
SubmitPostprocess(session_id, candidate_refs)
CommitSession(session_id, frozen_payload, payload_hash)
```

### 14.4 路由规则

先按 machine_id 进入对应 机器管理员，再按 session_id 找到对象。必须校验 Session 与 machine_id、来源设备相匹配。

未知 Session、机器不匹配、来源不匹配、过期重试和已冻结结果的迟到更新进入隔离/审计处理，不随意重新分配。不得按字符串相似、队列顺序或“当前只有一台在测量”猜测归属。

帧被选中后，先登记 frame_id 和待完成 job_id，再向 OCR 队列提交。必须保证 CaptureSealed 之后该采集窗口的图像清单已经完整，不能在任务未登记时误判待完成数量为零。

## 15. 并发模型

采用单进程业务协调加独立设备/计算工作单元，不为三台机器复制三套主程序。

- 每台机器拥有独立 FIFO 事件队列和一个串行业务 机器管理员。本机活动 Session 和旧 Session 的所有业务更新都在这里执行。
- CameraWorker 按相机独立运行；同一相机的采集命令按顺序、按 capture_id 执行。
- IO 接收与频率接收独立于 OCR；硬件 SDK 的阻塞调用放在专属执行单元，不能阻塞业务事件循环。
- OCR 使用有界调度器与受控数量的 Worker，工作单元只接收不可变任务参数/图像引用。
- DBWriter 独立执行一次事务写入，以结果事件通知 机器管理员。

控制事件与大图像数据分开传输。事件包含图像引用，不在公共控制队列里无限堆积大图像。队列满必须有明确的失败/背压分支；START/CLOSE 不得静默丢弃。

每一来源保留顺序与序号，但不要假定 IO、相机和频率三条独立来源在全局上天然有序；跨来源归属依靠窗口和不可变标识。

## 16. Session 完成检查

相关业务事件更新后调用 `try_finalize`，不循环等待。失败路径通过 `handle_measurement_failure` 标记 FAILED、打印错误、释放图片和排队批次；活动周期仍保留身份和 CycleTimeout，直到正常 CLOSE 或中断。正常关闭且仍在处理的周期才可能生成正常提交。

```text
RUNNING
  ├─ 整轮处理失败或中断 → FAILED → 日志与资源清理
  └─ 正常关闭、采集封口、批次结算、OCR 与频率成功
       ↓
     证据验证
       ├─ 失败 → FAILED → 日志与资源清理
       └─ 成功 → 冻结正常记录 → WAITING_COMMIT_DB
                                  ├─ 提交确认成功 → COMMITTED
                                  └─ 入队或提交失败 → FAILED
```

同一机器仍使用 FIFO 事件队列串行修改 Session。WAITING_COMMIT_DB 后忽略迟到采集结果；提交回调只处理原 Session，不改变新轮活动身份。失败记录不进入存储队列，不创建持久化待提交任务。

## 17. 提交、证据与恢复

### 17.1 最终记录

一次 Session 对应一条最终主记录，至少包含：

```text
session_id, machine_id
start_time, finish_time
camera_id, frequency_source_id
ocr_result / ordered_lines / fusion_score
final_frequency_hz
final_measurement_id
evidence_refs
error_codes
configuration_version
payload_hash
```

正常记录要求 OCR、频率、关闭信息及证据完整。异常和中断只打印日志，不写入测量表。

### 17.2 幂等与事务

以 session_id 建立唯一约束，数据库已有同一内容时视为成功，内容不一致时报告冲突，不覆盖历史记录。正常结果的频率明细、最终值和 payload 在同一事务中写入。

第一版每个请求仅尝试一次写入，不执行提交重试或后台补交。队列满、数据库异常或内容冲突时，Session 进入 FAILED，打印日志并清理。写入已完成但确认丢失时，历史结果保留，未确认的 Session 按失败处理，不承诺数据库中没有该记录。

### 17.3 运行持久化

Session 和正常结果待提交队列只保存在内存，不创建新的持久化待提交记录。原恢复库继续承担实例锁、设备及路由审计、旧版本启动清理；历史待提交和已提交身份表保留兼容，不参与当前测量自动补交。整轮失败和中断仅打印日志，不生成异常测量记录；未受理周期也仅打印日志并等待机器关闭复位。

### 17.4 重启

启动时处理：

- 后台任务启动前：在独占锁保护下移除旧版本检查点表并清理全部待提交记录，包括冲突及未受理周期记录。
- 历史数据：保留最终结果、提交身份、审计和证据图片。
- 旧周期：不恢复 Session、OCR、超时或提交任务，不生成旧周期的中断结果。
- 初始状态：已关闭则等待新启动；运行中或未知则等待有效关闭或明确的关闭状态同步。

不得因重启、重新连接或初始输入有效而再次为同一段运行过程自动创建正常记录。

## 18. 异常与超时

所有等待有明确期限；超时事件由定时调度投递，不在业务线程中 sleep。

| 情况 | 必须行为 |
|---|---|
| 重复 START，已有活动 Session | 忽略重复或记录协议异常，不覆盖原对象 |
| 重复 CLOSE，无活动 Session | 幂等处理，不关闭其他未完成 Session |
| 无有效图像/全部识别失败 | 本轮 FAILED，打印日志、释放资源；关闭前保留活动身份 |
| 关闭后没有有效频率 | 处理 CLOSE 时立即记录缺频率，不补 0、不额外等待 |
| 数据跨周期归属不明确 | 隔离并记录冲突，不写给“当前 Session” |
| 相机采集失败 | 记录设备身份及异常，停止全部测量并退出程序 |
| IO 通信故障 | 接入层报告故障，停止全部测量并退出；不生成正常 CLOSE |
| 长时间无 CLOSE | 超出 max_cycle_open_ms 后标记 FAILED 并记录中断日志，不伪造正常关闭 |
| OCR 超时/工作进程退出 | 标记本轮 FAILED 并清理，不写异常记录 |
| 数据库写失败 | 标记 FAILED，打印日志并清理，不自动重试或补交 |
| 旧结果在新 Session 期间返回 | 只更新旧 Session；旧记录已冻结则保留为迟到审计 |
| 队列/待处理 Session 超限 | 不再接收新的正常采集周期，记录本轮未受理并报警 |
| 磁盘不足/证据写失败 | 不提交缺证据的正常记录，限制受影响的新周期 |
| 程序退出 | 停止接收新周期、有限时间排空后台、未完成任务标记 FAILED 并打印日志；不伪造设备 CLOSE |

软件拒收只表示本系统不能保证本轮采集完整，不表示已经阻止实体机器启动。本轮未受理后必须跟踪到明确关闭，避免下一个事件被错接成新正常周期。

初版任一设备故障均停止整个程序并释放全部设备；不提供单机故障隔离或自动恢复。

## 19. 关键时间线

```text
M01 START
   ↓
创建 A，active_session_id = A
   ↓
A 采图；A 频率窗口打开；IO 持续监听
   ↓
A 图像窗口自然结束，OCR 仍在后台处理
   ↓
A 获得有效频率候选
   ↓
M01 CLOSE
   ↓
A.state 保持 RUNNING
A 频率窗口封口，冻结最后一个有效测量
active_session_id = None
   ↓
M01 再次 START
   ↓
创建 B，active_session_id = B
   ↓
A 的 OCR 完成事件到达，携带 session_id = A
   ↓
仅更新 A，A 三项条件齐全并提交
   ↓
A 提交成功，A.state = COMMITTED
   ↓
B 仍是现场活动 Session，继续采集/等待关闭
```

与此同时 M02、M03 可各自运行自己的周期。A 的后台事件、提交和清理都不得清空 B 的活动位置。

## 20. 工程模块建议

在已有项目中优先沿用目录与依赖，按职责映射，不为形式重构正常工作的驱动/OCR。新建工程可采用：

```text
app/
  main.py
  config/
    schema.py
    loader.py
  domain/
    machine.py
    session.py
    events.py
    results.py
  application/
    machine_manager.py
    machine_manager.py
    session_manager.py
    event_router.py
    deadline_scheduler.py
  adapters/
    io_adapter.py
    camera_adapter.py
    frequency_adapter.py
  workers/
    camera_worker.py
    ocr_scheduler.py
    ocr_worker.py
  processing/
    frame_filter.py
    line_matching.py
    text_fusion.py
    evidence_selector.py
  database/
    repository.py
    db_writer.py
    evidence_store.py
    recovery_store.py
  observability/
    logging.py
    health.py
    metrics.py
  tests/
```

不要创建单个全局 current_session；不要让硬件回调直接调用数据库提交；不要在 IO 回调里运行 OCR。不要为了增加机器数复制 main1.py、main2.py、main3.py。

## 21. 编码顺序与交付物

按下列顺序实现，每层提供明确接口与测试：

1. 配置模型、事件模型、MachineContext、BeltSession 与状态派生。
2. MachineManager、事件路由、Session 创建/关闭/完成检查与去重。
3. 设备适配器与真实设备读写接口集成，保留原有设备控制边界。
4. 独立图像采集、图像清单与封口机制。
5. OCR 有界调度、行级识别、跨帧后处理和证据保存。
6. 频率新测量识别、Session 归属、候选累计和关闭结算。
7. 正常记录幂等提交、失败日志清理、重启清理和超时处理。
8. 三机并行、长时运行与边界条件验收。

交付应包含实际代码、配置说明、事件/状态说明、数据库初始化或迁移、测试、启动与退出说明，以及可追踪 machine_id/session_id 的日志。不擅自追加 UI、联网业务、自动判定张力合格范围或反向控制机器等未定义功能。

## 22. 必须通过的验收场景

| 编号 | 场景 | 预期 |
|---|---|---|
| 1 | 三台机器同时启动 | 创建三个独立 Session，分别使用绑定相机/频率通道 |
| 2 | OCR 先结束，机器尚未关闭 | 保留结果，不提前提交 |
| 3 | 机器已关闭，OCR 尚未完成 | 旧 Session 后台等待，不占用活动位置 |
| 4 | A 关闭后 B 启动，A OCR 晚到 | A 结果只入 A，B 保持活动 |
| 5 | A 提交回调在 B 活动期间到达 | 不清空或修改 B 的活动绑定 |
| 6 | 一个启动输入持续有效 | 只生成一个正常 Session |
| 7 | 关闭事件重复 | 不重复结算、不误关其他 Session |
| 8 | 同一事件/任务结果重复 | 不重复计数、不重复投票、不重复最终记录 |
| 9 | 连续两次新测量数值相同 | 仍能识别为两次测量，按身份与窗口归属 |
| 10 | 上次显示值在新周期启动后仍存在 | 不当作新周期新频率 |
| 11 | 无法确认属于 A 还是 B 的延迟频率 | 进入异常处理，不猜测匹配 |
| 12 | 关闭后没有有效频率 | 关闭时标记 FAILED 并打印日志，不提交数据库 |
| 13 | OCR 没有有效文字 | 不置成功、不创建空 OCR 的正常记录 |
| 14 | 数据库实际写入成功但确认丢失 | 不自动重试；保留已写入结果，未确认任务按失败清理 |
| 15 | IO 掉线或程序重启时机器正在运行 | 不生成伪 CLOSE 或中途正常 Session |
| 16 | 一个机器 OCR 积压 | 其他机器仍有调度机会，容量超限有明确处理 |
| 17 | 旧相机停止命令/回调晚到 | 不停止新采集，不将旧帧分给新 Session |
| 18 | 异常退出后重启 | 清理旧待处理状态，不续办旧任务；等待本次有效启动 |
| 19 | 证据保存失败 | 不提交缺证据的正常结果 |
| 20 | 关闭提前于采集时长 | 提前封口，只处理属于关闭边界前的已接收图像 |

最终判定原则：任何一条正常结果，都能证明它对应哪台机器、哪次启动到关闭的测量、哪些图像、哪次频率测量及哪套处理配置；任何无法完成这种归属或完整性验证的数据，都不能混入正常结果。
