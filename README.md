# BeltVision 项目说明

## 一、项目逻辑

BeltVision 是运行在工控机上的多皮带机视觉监测系统。一台工控机可管理多台机器，每台机器绑定一台海康相机、一个 IO 输入通道和一个频率仪。

系统启动后，从 `config/config.yaml` 读取公共配置，从 SQLite `machine` 表读取已启用机器，为每台机器建立 `MachineRuntime`、`Camera` 和 `FrequencyAdapter`，随后加载海康 MVS SDK 并逐台打开相机、校验 Modbus 串口与各机器的 DI 通道绑定，再启动机器事件处理、频率监听和 IO 轮询等后台任务；Modbus 串口连接在首次轮询读取时建立。当前 `config.yaml` 的 `modbus_serial_port` 与 `io_machine_channels` 尚未填写，校验不通过时启动失败。

相机运行参数由 `config/config.yaml` 的 `camera` 段读入 `AppConfig`，Runtime 为每台机器写入 `MachineConfig`，打开相机时由 MVS SDK 依次设置 Mono8、关闭自动曝光与自动增益、写入曝光 80 微秒和增益 0，再选择输出线路、设置频闪模式与信号源并使能频闪；参数写入失败时释放相机句柄并按单台连接失败处理。`camera_line_selector` 和 `camera_line_source` 仍需按现场 MVS 实际值填入，启用频闪时缺失任一值会使配置校验失败。

IO 模块通过 Modbus RTU 持续读取 DI 状态。每台机器绑定通道的首份有效读数只建立基线，并用当前电平确认机器是否在等待复位，不产生 START 或 CLOSE；之后的 `False → True` 视为 START，`True → False` 视为 CLOSE。

IO 读取失败时，系统记录日志并清空旧 DI 状态，以 `IO_INTERRUPTED` 结束仍未收到 CLOSE 的采集周期；已经关闭、正在等待识别或入库的周期继续处理。轮询保持运行，通信恢复后的首份有效读数按上述基线规则处理，断线期间发生的电平变化不再与旧状态比较，也不补发 START 或 CLOSE。

首次读取或断线重连时，`ModbusClient` 创建串口客户端并读取其连接状态；未连接时返回失败，IO 监听按重连间隔继续尝试。连接过程出现未知程序异常时，异常直接交给 Runtime 的全局故障流程。

重连与读取 DI 统一由一次 `read_discrete_inputs()` 调用承担：`OSError` 复位连接状态并按通信失败处理，`ModbusException` 按本次通信失败处理，两者都返回失败并沿用重连间隔继续尝试。其他异常原样交给 Runtime 登记全局故障，不根据异常文本决定是否重连。

退出时，Runtime 停止 IO 监听后统一关闭 Modbus 串口，再关闭相机 SDK、数据库并释放实例锁。Modbus 关闭失败会登记全局故障，但不阻断后续资源释放；此前已记录的首次故障保持不变。

数据库收尾时先尝试关闭运行库连接，再解锁并关闭实例锁文件；某一步关闭失败仍继续处理其他数据库资源，最后将最先发生的关闭异常交给 Runtime 记录。

现场 START 经本机事件队列进入 `MachineRuntime`，创建当前 `MeasurementSession`；相机采集结果先完成帧准备与筛选，有合格帧时再进入 OCR 排队与识别，频率读数按收到的顺序保存到同一 Session。收到 CLOSE 后停止本轮采集并确定最终频率；关闭信号和 OCR 结果都到达时，先保存证据图片，再写入测量记录并释放当前 Session。频率当前按配置模拟，真实协议待接入。

相机通过 `CaptureResult.frames` 发送 `CameraFrame`，其 `image_bytes` 保存 SDK 复制的原始像素字节；`MachineRuntime.run_ocr_pipeline()` 调用 `prepare_frames_for_ocr()` 为相机帧补充 Session 和采集信息，生成 `MeasurementFrame`，再筛选出 `qualified_frames`。识别时，`convert_mono8_frame_to_array()` 校验 Mono8 格式与字节数，将合格帧转换为 `image_numpy`（二维 `uint8` 图像）交给共享 OCR Engine；证据编码继续使用原始像素字节。

原始帧先由 `prepare_frames_for_ocr()` 在线程中整理为带周期身份的 `MeasurementFrame`，再调用 `select_qualified_frames()` 初筛；这一步不占用共享 OCR 资源，当前初筛仍原样返回全部图片。初筛为空时直接生成包含全部原始帧的人工复核结果并交给现有 `OCR_COMPLETED` 流程；有合格帧时才开始等待共享资源，获得资源后由 `recognize_qualified_frames()` 调用 `recognize_images()` 逐帧转换 Mono8 图像并使用共享 OCR Engine，最后由 `generate_final_text_and_images()` 终选文字与证据图片。预处理后和获得资源后都检查 Session 状态；明确的预处理、帧格式和模型结果结构错误按本轮 OCR 失败处理，未分类异常交给 Runtime 全局故障流程。

START 时只启动 `CYCLE_TIMEOUT`；相机采集完成后 OCR 任务在 `ocr_lock_wait_timeout_ms` 内等待共享 OCR 处理资源，资源由 `TextRecognizer` 内部串行管理，`MachineRuntime` 不直接管理 OCR 锁。等待超时经独立事件将本轮失败写入 `abnormal_events`，等待真实 CLOSE 后释放 Session。获得资源并确认 Session 仍有效时才启动 `OCR_TIMEOUT` 和实际识别；`ocr_result_timeout_ms` 只计算获得资源后的 OCR 处理时间，正常识别完成时取消该期限。后续 Session 各自独立等待并记录资源等待失败。

独立 OCR 项目的核心代码与原始配置 `config.yaml` 位于 `src/ocr/`：Runtime 在至少一台相机连接成功后，通过 `src/text_recognizer.py` 中共享的 `TextRecognizer` 加载配置并初始化一次 `BeltOCREngine`，等待模型准备完成后才启动机器任务并开放 START；三台机器及后续测量周期复用同一实例，独立调用识别入口时也由 `TextRecognizer` 兜底初始化。Engine 的路径入口仍按配置保存 JSON；内存入口 `process_image(image)` 返回 `image_path=None` 且不保存 JSON，两种入口共用尺寸处理、ROI、预处理、识别、过滤与文字块分组流程。

模型返回后，OCR 先检查结果数量及业务筛选必需的 `blocks`、`lines`、`text` 结构；仅对通过现有格式过滤、实际参与比较的候选检查 `confidence`。接口结果异常抛出 `OCRProcessingError`，按 `OCR_FAILED` 结束当前 Session；未知识别异常由任务完成回调交给 Runtime 的全局故障流程，不生成 `OCR_FAILED`。正常返回但没有可靠业务文字时继续生成待人工复核的结果。

文字按去空白后的 20、8、3、2 位分类处理，最终生成文字、来源图片以及是否需要人工复核的信息。

CLOSE 后停止本轮采集，封闭频率列表并取最后一个有效频率。待 OCR 完成后，`MachineRuntime` 汇总文字、频率和复核状态，将证据帧编码为 JPG，保存到：

`{evidence_directory}/YYYYMMDD/machine_id/session_id/`

日期取本轮开始时间的本地日期，`evidence_directory` 当前为 `runtime/evidence`。

随后通过 `Database` 写入 SQLite 测量记录，并释放本轮 Session，机器重新等待下一次 START。正常轮与待复核轮都写入记录，待复核轮保存全部原始帧并置 `needs_review=1`；OCR 抛错、超时、无采集帧和周期中断只清理本轮，不写记录。OCR 失败或超时后若现场尚未 CLOSE，失败 Session 保留至真实 CLOSE；`current_recognition_task` 仅表示当前 Session 的任务，`unfinished_recognition_tasks` 保存全部未结束任务，因此 CLOSE 后不再等待旧 OCR 任务即可释放。旧任务继续持有共享 OCR 处理资源直到实际底层识别结束，随后资源才允许下一轮使用；迟到结果不会进入新 Session，退出时仍等待全部 OCR 任务。

退出时，`SystemRuntime` 停止相机并等待本机任务结束；`MachineRuntime` 仅将尚未完成的 Session 标记为失败、记录退出原因，再清空当前周期。已写入数据库的 Session 即使因任务引用暂时留在机器中，也保持 `COMMITTED` 状态，不写入退出失败事件。

证据图片编码阶段遇到明确的 MVS SDK 错误时，清理本轮新建图片，以 `EVIDENCE_ENCODING_FAILED` 将当前 Session 和异常明细写入 `abnormal_events`，不提交测量记录；相机仍可供下一轮采集，其他机器继续运行。未知编码异常继续交给 Runtime 的全局故障流程。

证据图片文件操作出现 `OSError` 时清理本轮新建图片，将它包装为 `EvidenceWriteError`，并以 `EVIDENCE_WRITE_FAILED` 结束当前 Session；图片全部保存后，测量记录提交遇到 SQLite `SQLITE_BUSY` 时最多尝试两次，两次间隔 100 毫秒。写锁重试耗尽或发生其他 SQLite 提交错误时，以 `DATABASE_WRITE_FAILED` 结束本轮，已保存的图片保留。数据库发现同一 Session 内容冲突时抛出 `CommitIntegrityConflictError`，以 `COMMIT_INTEGRITY_CONFLICT` 结束本轮，原记录不被覆盖。这三类存储故障在 Session 失败记录完成后进入 Runtime 全局故障流程并停止接收新测量；已知 MVS 图片编码失败只结束当前 Session，未知保存异常继续沿现有全局故障路径上报。

采集完成却没有帧时，图像采集阶段上报失败，OCR 不启动；已知的 OCR 处理错误或周期超时也进入 Session 失败收尾。`MachineRuntime` 将失败原因写入 Session、标记失败并把机器编号、周期编号和错误明细写入现有 `abnormal_events` 表；随后取消并解绑本轮 OCR 任务，待现场关闭后释放 Session，旧任务独立收尾。人工复核结果仍按正常测量流程保存。异常事件写入失败只记录日志，不影响本轮收尾；收尾全部完成后，审计异常经 `on_system_failure()` 升级为系统级故障并安排 Runtime 退出。调用方已有原始系统级根因（证据图片写入失败、测量结果入库失败或提交冲突）时，`handle_session_failure()` 通过 `system_error` 参数优先上报该根因，审计异常只留在日志，不覆盖首次故障。

异常按影响范围进入现有三个入口：`handle_session_failure()` 只结算当前 Session，并在收尾完成后把调用方根因或异常事件存储故障交给系统级入口，`handle_machine_failure()` 记录本机故障并复用 Session 失败收尾，`handle_system_failure()` 关闭 Runtime 信号入口并安排整体退出。正常或迟到的同轮相机 `CAPTURE_FAILED` 都进入机器级入口；若当前 Session 已因 OCR 超时失败，只登记本机故障，不重复结算 Session 或写入异常事件。现场 CLOSE 后释放失败 Session，本机后续不再受理 START，其他机器继续运行。

单次取帧没有数据时继续采集；整轮没有帧时以 `CAPTURE_EMPTY` 结束当前 Session。采集阶段发生明确的 MVS 设备异常时，相机先释放本轮采集资源，再向所属机器交付 `CAPTURE_FAILED`，由现有失败流程记录 Session 和 `abnormal_events`；故障相机停止受理新周期，其他机器继续运行。单台相机启动连接失败时保留该机器的不可用状态并继续初始化其他机器；所有相机都连接失败时结束本次启动。设备修复后通过重新启动客户端恢复。

机器收到 START 时先检查相机可用状态和采集锁，未受理的周期不会创建 Session，也不会把采集锁占用记为设备故障。已受理的周期由相机采集任务交付结果或设备故障事件，再由机器完成失败记录、等待 CLOSE 并释放 Session。

运行中出现明确的相机采集设备故障时，`Camera` 交付 `CAPTURE_FAILED`，`MachineRuntime` 沿用启动时传入的相机状态通知回调，将机器编号、“相机故障”和原始错误原因发送到实时页；界面保留该故障状态，同时继续更新本轮测量进度节点。

未知的相机程序异常、IO、识别任务或系统级异常由 `SystemRuntime` 统一处理并停止整个应用；明确的单台相机采集设备异常和普通单轮业务失败只结束当前 Session。

## 二、数据流向

`config.yaml + machine表`
→ `SystemRuntime`
→ 建立各机器运行对象
→ 连接相机和校验 Modbus IO
→ IO 检测 START
→ 创建 Session
→ 相机采集 + 频率采集并行
→ 原始帧进入 OCR
→ 筛帧（占位）
→ 模型识别（待接入）
→ 最终文字/图片筛选
→ IO 检测 CLOSE
→ 取得最终频率
→ MachineRuntime 汇总结果
→ JPG 证据图片落盘
→ Database 写入 measurement_records
→ Session 结束
→ 等待下一轮。

GUI 启动入口创建 Repo、Service 和唯一的 `AppController`，四个正式页面通过 `AppController` 请求机器、历史和异常事件数据，再进入对应 Service 与 Repo。实时监测由 `AppController` 创建、启动、停止并释放 `src/runtime/system_runtime_thread.py` 中的 `SystemRuntimeThread`（`QThread`）；该线程仅负责在后台运行 `SystemRuntime`，不处理业务判断。`MachineRuntime` 的相机状态、测量进度、OCR 文字和周期关闭通知经 `SystemRuntime`、`SystemRuntimeThread`、`AppController` 的 Qt 信号进入 `RealtimePage`，页面按机器编号及当前 Session 更新 `MachineCard`。窗口关闭时先请求 Controller 停止监测，待 Runtime 完成资源释放并发出结束信号后再退出。页面仍负责卡片动画、OCR 分类显示和当前周期缓存；机器卡片展示动画、状态、OCR 结果、频率和进度，频率数值仍未正式接入。实时页不再创建系统日志演示表格，运行日志仍由各模块按原链路记录。

桌面入口在创建 `QApplication` 后初始化浅色 Fluent 主题，`FluentWindow` 一次创建并持有四个正式页面和三个占位页。导航切换复用页面实例；进入历史页、异常页时各自通过 Controller 刷新一次，实时页缩放时仅重排已有机器卡片。机器表单、历史详情和异常详情在主窗口遮罩中展示；历史详情从记录目录读取 JPG 证据，缩略图在同样的遮罩中打开大图。保存与查询仍沿用页面到 Controller、Service、Repo 的原有数据流，关闭窗口时继续等待监测线程完成资源释放。

实时监测页点击 Fluent 主按钮“启动监测”后，`RealtimePage` 将启动请求交给 `AppController`；启动成功时禁用启动按钮并启用停止按钮，收到监测结束通知后恢复按钮状态。

业务 Service 向 `AppController` 返回数据或抛出业务异常；遇到已知 SQLite、JSON 故障时，Service 将技术详情记入日志，并把对外异常转换为可展示的固定提示。`AppController` 将结果包装为 `Result.ok(...)` 或 `Result.error(...)`，UI 继续读取 `success`、`data`、`message` 三个字段。

机器、测量记录和异常事件 Service 在方法返回值中组装带业务字段名的字典（`machine_id`、`machines`、`records`、`record`、`machine_ids`、`events`、`event`），`AppController` 直接用 `Result.ok(service_data)` 包装后交给 UI；线程运行状态由 Controller 组装为 `{"running": ...}`。删除、复核和监测启停等无数据操作仍返回 `Result.ok()`。

机器新增和修改成功时，`MachineService` 返回包含 `machine_id` 的字典；字段重复时抛出携带字段名和对应提示的 `MachineDuplicateFieldError`。`AppController` 将业务数据或异常转换为 `Result.ok(...)` 或 `Result.error(...)`，页面继续使用 `data["field"]` 定位重复字段。

`SystemRuntime.start()` 只在启动时读取一次启用机器并据此创建 `MachineRuntime`、`Camera`、`FrequencyAdapter`，运行期间不刷新机器配置。因此 `AppController` 以 `runtime_thread` 是否存在作为拦截条件：只要线程尚未清空（启动中、运行中、停止清理中），`create_machine()`、`update_machine()`、`delete_machine()` 在调用 `MachineService` 之前直接返回 `Result.error("监测运行中，请先停止监测后再修改机器配置。")`；`SystemRuntimeThread` 发出 `finished`、`finish_monitoring()` 将 `runtime_thread` 置为 `None` 后，机器写操作自动恢复。机器查询接口不受影响。

历史记录页进入时通过 `AppController` 调用 `MeasurementRecordService`，再经 `MeasurementRecordRepo` 从业务库 `measurement_records` 按当前状态、机器和可选日期范围统计总数，并以 SQLite `LIMIT / OFFSET` 每页读取 20 条测量结果；日期范围按运行电脑的本地自然日解释，Service 将开始日零点和结束日次日零点转换为 UTC，Repo 以包含下界、排除上界的条件筛选 `finish_time`。结果按结束时间倒序，同时间按 `session_id` 倒序，切换筛选或重新进入页面从第一页读取，重新进入时恢复不限时间；复核后刷新当前页，末页消失时回退到最后有效页。机器选项或记录读取失败时，历史页清空表格和分页显示并禁用翻页按钮。详情保留原始 OCR、频率、复核原因和本轮 JPG 证据图片。待复核记录可确认原文字或按行保存人工修正文字，Repo 只对尚未复核的记录写入 UTC 复核时间和可选的 `reviewed_lines` JSON，原 `ordered_lines`、`needs_review` 与 `review_reason` 保留；已复核详情同时显示原始与最终文字，列表摘要优先显示人工结果。机器名称从机器表关联取得，历史中停用或软删除的机器仍可查询；`abnormal_events` 保留在独立运行库，不进入历史记录页。

GUI 启动时先在运行库确保 `abnormal_events` 表存在。异常事件页进入时经 `AppController`、`AbnormalEventService`、`AbnormalEventRepo` 读取该表，按时间倒序显示异常记录，并支持机器筛选、完整 Session ID 搜索和详情查看。后续异常写入使用中文 `reason`，已有记录原样保留；页面直接显示库中的异常原因，详情展示完整 `payload_json`。

运行链路从数据库读取已启用机器的名称、编号及设备绑定，并把机器名称随 `MachineConfig` 传给相机和单机运行时；终端日志先显示机器名称，再保留 `machine_id`、`session_id` 等定位字段，按 DI 信号、开始测量、相机采集、OCR 排队与识别、频率结算、结果保存和测量结束串成时间线。监测系统另行记录启动、共享 OCR 模型加载、Modbus DI 断线重连和资源释放；正常 DI 轮询、逐帧采集和事件队列收发不产生日志。

开发联调可从仓库根目录运行 `python scripts/simulate_measurement.py normal` 或 `python scripts/simulate_measurement.py review`。正常场景读取 `statistics/imgs` 的 JPG，待复核场景取 `statistics/test_images_without_results` 中按文件名排序的首张 BMP，统一解码为 Mono8 相机帧。脚本通过现有 Camera、TextRecognizer、MachineRuntime 的事件队列完成真实 OCR、50.0 Hz 频率结算和 CLOSE，再由 MachineRuntime 将证据 JPG 与测量结果写入正式配置的业务库；历史记录页按原有查询和图片展示流程查看新记录。模拟只替代相机取流、图片编码和现场输入，不启动真实 MVS 或 Modbus。

## 三、项目结构

* `src/runtime/system_runtime.py`：系统启动、机器初始化、IO 信号路由、全局异常和退出。
* `src/runtime/machine_runtime.py`：单台机器运行时实例，负责事件队列、Session 生命周期、OCR 调度和最终结算。
* `src/camera/camera.py`：单轮图像采集。
* `src/camera/hikrobot_sdk.py`：海康 MVS SDK、取帧、JPG 编码和相机关闭。
* `src/text_recognizer.py`：筛帧、OCR、文字与证据图片终选。
* `src/frequency_adapter.py`：频率采集及当前 Session 归属。
* `src/modbus_client.py`：Modbus RTU 串口连接和 DI 状态读取。
* `src/database.py`：数据库初始化、测量记录、异常审计和实例锁。
* `src/repo/`：机器等基础数据访问。
* `src/service/`：机器、测量记录与异常事件业务服务。
* `src/controller/`：GUI 请求入口、统一结果与实时监测生命周期。
* `src/runtime/system_runtime_thread.py`：运行 SystemRuntime 的后台线程与实时 Qt 信号。
* `src/models.py` / `src/enums.py`：Session、事件、帧、OCR 结果及状态定义。
* `src/config_util.py`：YAML 配置读取与校验。
* `src/async_utils.py`：在线程中执行阻塞操作的公共封装。
* `ui/`：PySide6 桌面界面，包括实时监测、机器管理及其他业务页面。

实时监测页的数据流：载入或手动刷新时读取启用机器并建立摘要卡片，Controller 的连接、测量进度、周期关闭及 OCR 通知更新现有内存缓存，再同步四项总览统计和选中机器详情。四张总览卡与机器卡网格均使用 16px 间距；固定展开的 176px 导航先展示品牌与工作台分组，页面切换仍复用原入口。机器列表占满详情卡左侧空间，卡片最小宽度为 280px，并随列表宽度在两列或三列间重排、均分列宽；380px 详情卡与最后一列始终相隔 16px，缩放时只重排已有卡片。皮带动画直接绘制在白色卡片中，OCR 摘要保留首条文字和浅灰底。右侧详情卡从已有机器记录与实时缓存显示序列号、当前状态、相机状态、独立的频率卡及按 20 / 8 / 3 / 2 分类的完整只读 OCR，继续展示本轮进度；底部运行时长和今日两项统计暂以 `--` 占位，不发起新查询。详情内容在空间不足时独立滚动，机器列表仍透出实时页统一的浅灰背景；选择在刷新后保留，周期隔离及皮带动画继续沿用原流程。
