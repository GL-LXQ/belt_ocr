# 多皮带机并行采集与 OCR 系统：Codex 开发规格

> 当前已接入 MVS 相机与 Session 流程；OCR 支持内存图片批次及终选占位入口；频率采用设备黑盒接口，接收及关闭结算与数据库存储已接通，设备协议内部待实现。支持 SQLite 保存、本次运行内自动补交和故障审计；重启不恢复旧 Session。
> 启动方法、配置说明、信号入口及当前边界见 [运行说明](USAGE.md)。
> 下文保留完整开发规格，不表示所有生产能力均已交付。

海康 MVS 模块已接入 App，使用方法见 [运行说明](USAGE.md) 和 [MVS 采集说明](MVS_CAPTURE.md)。文件夹模拟采集已移除；测试通过假 SDK 验证真实适配器。没有配置序列号或没有可用相机时，对应机器不接受正常测量。

当前系统的数据流：START 创建独立 Session，登记 frequency_adapter.active_session_id 并启动相机内存图片组批；OCR 按批次回传原始文字块，采集封口且批次结算后只触发一次文字和图片终选占位入口，当前不保存图片。频率监听黑盒负责设备连接、新有效测量识别和固定接收时的周期归属，按接收顺序将 FrequencyMeasured 与 START/CLOSE 送入同一机器 FIFO 队列；业务层仅追加 measurement_frequencies，不按设备测量时间重排，也不重复校验黑盒已保证的数据。处理 CLOSE 时立即清空适配器的活动周期、封闭列表并取最后一条作为最终频率；频率状态统一使用 FrequencyState 枚举，区分 RUNNING（采集中）、SUCCESS（成功）和 FAILED（失败）；没有有效测量、频率故障或周期中断时保留明细且最终值为空。CLOSE 前入队的测量先处理，CLOSE 后不等待设备、不补收旧轮数据。频率明细 JSON、final_frequency_hz 和完整 payload 在同一 SQLite 事务中写入，整轮记录仍等待 OCR 等原有完成条件。当前 listen_measurements 按 frequency_interval_ms 循环读取 simulated_frequencies_hz 产生联调测量，每次分配独立身份，无活动 Session 时不交付；真实设备协议仍待替换，读取异常报告故障；旧库升级保留历史冻结内容和哈希，重启不恢复旧 Session。

结算数据流：`try_finalize` 先判断周期是否结束；中断周期确定为中断结果，正常关闭周期在 OCR 未超时时等待采集封口和批次结算，再直接根据 OCRState 枚举的 ocr_state 与 FrequencyState 枚举的 frequency_state 确定完整或待复核结果。完整结果通过证据验证后，与异常结果统一组装并冻结提交内容，释放内存图片和排队批次、取消剩余期限任务，最后提交存储并等待入库回调。

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
- 正常结果完整后统一入库。失败、漏采和中断也必须留下异常记录，但不能冒充完整的正常结果。

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

三个业务条件全部满足才能进入正常提交：

```text
ocr_state == OCRState.SUCCESS
frequency_state == FrequencyState.SUCCESS
cycle_closed = True
```

其中：

- `ocr_state == OCRState.SUCCESS`：图像窗口已封口，选中的图像任务全部结算，后处理成功，最终 OCR 与证据已确认。
- `frequency_state == FrequencyState.SUCCESS`：频率窗口已封口，最终频率已选定并通过有效性检查。
- `cycle_closed`：收到并确认了属于本 Session 的正常关闭事件。

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
                         最终测量记录 / 异常记录
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

通信超时、掉线和无效输入产生 `DeviceFault` / `InputInvalid`，绝不能转换为正常 CLOSE。重新连接后重新建立基线；无法确认期间是否发生周期变化时，进入重新同步流程，不沿用失效状态。

## 7. START：创建并启动一次测量

收到 `MachineStarted(machine_id)` 后，在该机器 机器管理员 中按顺序执行：

1. 校验事件新鲜性、来源顺序、机器同步状态和采集能力。
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
camera_id, source_epoch, frame_id
captured_at / received_at
image_ref, image_metadata
```

`source_epoch` 用于区分相机重连或计数器重置后的帧序号。归属必须在采集窗口内确定，后续 OCR 结果只沿用这个归属。

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
3. 为本 Session 记录 close_time，置 `cycle_closed = True`。
4. 关闭本 Session 图像窗口；已自然结束则幂等处理。
5. 立即封闭本 Session 频率列表，按接收顺序确定最终频率。
6. 仅当 active_session_id 仍等于该 Session ID 时清空活动位置。
7. Session 保留在未完成集合中，进入后台收尾。
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

正常关闭不能取消已入队 OCR，不能销毁旧 Session。允许新周期的前提仍包括相机已可接受下一采集命令、设备健康与积压未超限；不能因为旧 OCR 没结束而直接拒绝新周期。

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
start_time, close_time, finish_time
start_boundary, close_boundary, process_epoch

cycle_state: OPEN | CLOSED | INTERRUPTED
capture_id, capture_sealed
selected_frame_ids, pending_ocr_job_ids
ocr_state: OCRState.WAITING | RUNNING | SUCCESS | FAILED | TIMED_OUT
ocr_result, evidence_refs

frequency_window_sealed
measurement_frequencies
frequency_state: FrequencyState.RUNNING | SUCCESS | FAILED
final_frequency_hz, final_measurement_id

outcome: UNDECIDED | COMPLETE | REVIEW_REQUIRED | INTERRUPTED
commit_state: NOT_READY | READY | COMMITTING | RETRY_PENDING | COMMITTED
frozen_payload, payload_hash
errors, timestamps, deadlines
```

OCR 成功直接判断 `ocr_state == OCRState.SUCCESS`，频率正常直接判断 `frequency_state == FrequencyState.SUCCESS`。以下字段保留为只读派生属性：

```text
cycle_closed   = (cycle_state == CLOSED)
finished       = (outcome == COMPLETE and commit_state == COMMITTED)
```

界面显示的 `COLLECTING / WAITING_RESULT / COMMITTING / FINISHED` 由这些字段派生。不要同时维护两套互相独立的 Session 状态机。

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
DeviceFault / DeviceRecovered
SessionTimeout
CommitSucceeded / CommitFailed
```

### 14.3 最小命令集合

```text
StartCapture(machine_id, session_id, capture_id, parameters)
SealCapture(machine_id, session_id, capture_id, close_boundary)
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
- DBWriter 独立处理事务与重试，以结果事件通知 机器管理员。

控制事件与大图像数据分开传输。事件包含图像引用，不在公共控制队列里无限堆积大图像。队列满必须有明确的失败/背压分支；START/CLOSE 不得静默丢弃。

每一来源保留顺序与序号，但不要假定 IO、相机和频率三条独立来源在全局上天然有序；跨来源归属依靠窗口和不可变标识。

## 16. Session 完成检查

完成检查由每次相关事件更新后触发，也由超时/重试事件触发，不用空循环不断扫描等待。

```text
收到本 Session 的事件
       ↓
校验身份、去重、更新状态
       ↓
本 Session 是否已冻结提交内容？
       ├─ 是 → 仅处理提交回调或记录迟到事件
       └─ 否
           ↓
现场是否正常关闭？
       ├─ 否 → 继续采集/等待；发生中断则进入异常分支
       └─ 是
           ↓
OCR / 频率是否存在明确失败或已到期限？
       ├─ 是 → 生成待复核/异常记录
       └─ 否
           ↓
ocr_state == OCRState.SUCCESS && frequency_state == FrequencyState.SUCCESS && cycle_closed ?
       ├─ 否 → 等待下一事件
       └─ 是
           ↓
校验结果归属、证据与完整性
           ↓
冻结最终记录 → 置 COMMITTING → 提交 DBWriter
           ↓
成功确认 → COMMITTED / FINISHED
失败确认 → RETRY_PENDING，保留同一份冻结记录
```

伪代码仅表达业务顺序：

```python
def try_finalize(session):
    if session.commit_state in {"COMMITTING", "COMMITTED", "RETRY_PENDING"}:
        return

    if session.cycle_state == "INTERRUPTED":
        submit_frozen_exception(session, outcome="INTERRUPTED")
        return

    if not session.cycle_closed:
        return

    if session.has_terminal_failure:
        submit_frozen_exception(session, outcome="REVIEW_REQUIRED")
        return

    if session.ocr_state != OCRState.SUCCESS or session.frequency_state != FrequencyState.SUCCESS:
        return

    payload = validate_and_build_complete_payload(session)
    session.freeze(payload)
    session.outcome = "COMPLETE"
    session.commit_state = "COMMITTING"
    enqueue_commit(session.session_id, payload, session.payload_hash)
```

正式实现必须覆盖校验失败、提交队列满和持久化失败。失败不能留下无人处理的 COMMITTING 状态。冻结与“已提交请求”标记必须先于异步提交，以免重复完成事件触发两次提交。

数据库回调只更新目标 Session，不改变任何其他 Session 或 machine.active_session_id。

## 17. 提交、证据与恢复

### 17.1 最终记录

一次 Session 对应一条最终主记录，至少包含：

```text
session_id, machine_id
start_time, close_time, finish_time
camera_id, frequency_source_id
ocr_result / ordered_lines / fusion_score
final_frequency_hz
final_measurement_id
evidence_refs
outcome, error_codes
configuration_version, model_version, software_version
payload_hash
```

正常记录要求 OCR、频率、关闭信息及证据完整。异常记录允许缺失测量字段，但必须带明确原因，不计为正常完成。

### 17.2 幂等与事务

以 session_id 建立最终主记录唯一约束。应用状态门禁防止重复提交，数据库唯一约束作为最终防线。

提交成功但确认丢失时，重试同一个 session_id 和同一个冻结 payload。数据库已存在且内容一致，按成功处理；内容不一致，报告完整性冲突，不静默覆盖。这里保证的是最终逻辑记录不重复，不是假定传输永远只发生一次。

证据先保存到稳定位置并确认可引用，再将引用写入正常结果。提交失败保留证据和冻结记录，不先删除 Session。

### 17.3 运行持久化

“完整数据统一入库”针对最终正常业务记录，不禁止在启动、关闭和提交阶段保存运行记录。机器运行状态和未完成 Session 只保留在内存；冻结后的待提交结果和审计仍写入本地运行库，证据图片保存在磁盘。项目重启时清理旧待提交记录，不跨运行续办。

数据库临时不可用时保存到已配置的本地持久化待提交区，按策略重试，并对积压与磁盘容量报警。本次运行中存储恢复后按 session_id 去重补交，项目重启后不补交旧记录。

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
| 无有效图像/全部识别失败 | OCR 失败；正常 CLOSE 后形成待复核记录 |
| 关闭后没有有效频率 | 处理 CLOSE 时立即记录缺频率，不补 0、不额外等待 |
| 数据跨周期归属不明确 | 隔离并记录冲突，不写给“当前 Session” |
| 相机采集失败 | 标记本次采集异常；是否影响其他机器取决于共享资源范围 |
| IO 通信中断/无效输入 | 标记受影响机器不同步；不能生成正常 CLOSE |
| 长时间无 CLOSE | 超出 max_cycle_open_ms 后记录异常/中断，不伪造正常关闭 |
| OCR 超时/工作进程退出 | 按有限策略重试；失败则待复核，不能拖死其他 Session |
| 数据库写失败 | 保留冻结记录与证据，重试并报警，不置 FINISHED |
| 旧结果在新 Session 期间返回 | 只更新旧 Session；旧记录已冻结则保留为迟到审计 |
| 队列/待处理 Session 超限 | 不再接收新的正常采集周期，记录本轮未受理并报警 |
| 磁盘不足/证据写失败 | 不提交缺证据的正常记录，限制受影响的新周期 |
| 程序退出 | 停止接收新周期、有限时间排空后台、保存未完成状态；不伪造设备 CLOSE |

软件拒收只表示本系统不能保证本轮采集完整，不表示已经阻止实体机器启动。本轮未受理后必须跟踪到明确关闭，避免下一个事件被错接成新正常周期。

单设备故障按绑定范围隔离；共享 IO、OCR 或数据库故障可能影响多台机器，不能笼统承诺任何故障只影响一台。

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
A.cycle_closed = True
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
A 提交成功，A.finished = True
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
7. 最终记录幂等提交、持久化待提交区、恢复和超时处理。
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
| 12 | 关闭后没有有效频率 | 有限时间后记录待复核，不永久等待 |
| 13 | OCR 没有有效文字 | 不置成功、不创建空 OCR 的正常记录 |
| 14 | 数据库实际写入成功但确认丢失 | 同 ID 重试后仍只有一条一致记录 |
| 15 | IO 掉线或程序重启时机器正在运行 | 不生成伪 CLOSE 或中途正常 Session |
| 16 | 一个机器 OCR 积压 | 其他机器仍有调度机会，容量超限有明确处理 |
| 17 | 旧相机停止命令/回调晚到 | 不停止新采集，不将旧帧分给新 Session |
| 18 | 异常退出后重启 | 清理旧待处理状态，不续办旧任务；等待本次有效启动 |
| 19 | 证据保存失败 | 不提交缺证据的正常结果 |
| 20 | 关闭提前于采集时长 | 提前封口，只处理属于关闭边界前的已接收图像 |

最终判定原则：任何一条正常结果，都能证明它对应哪台机器、哪次启动到关闭的测量、哪些图像、哪次频率测量及哪套处理配置；任何无法完成这种归属或完整性验证的数据，都不能混入正常结果。
