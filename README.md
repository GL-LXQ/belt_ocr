# BeltVision 项目说明

## 一、项目逻辑

BeltVision 是运行在工控机上的多皮带机视觉监测系统。一台工控机可管理多台机器，每台机器绑定一台海康相机、一个 IO 输入通道和一个频率仪。

系统启动后，从 `config/config.yaml` 读取公共配置，从 SQLite `machine` 表读取已启用机器，为每台机器建立 `Machine`、`Camera` 和 `FrequencyAdapter`，随后加载海康 MVS SDK 并逐台打开相机、校验 Modbus 串口与各机器的 DI 通道绑定，再启动机器事件处理、频率监听和 IO 轮询等后台任务；Modbus 串口连接在首次轮询读取时建立。当前 `config.yaml` 的 `modbus_serial_port` 与 `io_machine_channels` 尚未填写，校验不通过时启动失败。

IO 模块通过 Modbus RTU 持续读取 DI 状态。首次读取用于同步机器现场状态；之后 `False → True` 视为 START，`True → False` 视为 CLOSE。

IO 读取失败时，系统记录日志并清空旧 DI 状态，以 `IO_INTERRUPTED` 结束仍未收到 CLOSE 的采集周期；已经关闭、正在等待识别或入库的周期继续处理。轮询保持运行，通信恢复后的首份有效 DI 只保存为新基线并更新机器的等待复位标志，不产生事件，后续读数再按边沿变化处理。

START 后创建本轮 Session，同时开始相机采集和频率收集；频率当前为占位实现，按配置的模拟读数循环交付，真实协议待接入。相机在采集窗口内保存原始帧；采集结束后进入共享 OCR 流程。OCR 依次执行：

原始帧 → `filter_qualified_frames()` 筛帧（当前原样返回全部图片）→ `recognize_images()` 文字识别（接口占位，当前抛 `NotImplementedError`）→ `generate_final_text_and_images()` 最终文字与证据图片筛选。识别模型接入前，每轮都以 OCR 失败结束，不生成测量记录。

文字按去空白后的 20、8、3、2 位分类处理，最终生成文字、来源图片以及是否需要人工复核的信息。

CLOSE 后停止本轮采集，封闭频率列表并取最后一个有效频率。待 OCR 完成后，`Machine` 汇总文字、频率和复核状态，将证据帧编码为 JPG，保存到：

`{evidence_directory}/YYYYMMDD/machine_id/session_id/`

日期取本轮开始时间的本地日期，`evidence_directory` 当前为 `runtime/evidence`。

随后通过 `Database` 写入 SQLite 测量记录，并释放本轮 Session，机器重新等待下一次 START。正常轮与待复核轮都写入记录，待复核轮保存全部原始帧并置 `needs_review=1`；OCR 抛错、超时、无采集帧和周期中断只清理本轮，不写记录。

采集完成却没有帧时，图像采集阶段上报失败，OCR 不启动；OCR 执行异常或周期超时也进入 Session 失败收尾。`Machine` 将中文失败原因写入 Session、标记失败并把机器编号、周期编号和错误明细写入现有 `abnormal_events` 表；随后取消本轮任务，待现场关闭及相关任务结束后释放 Session。人工复核结果仍按正常测量流程保存，异常事件写入失败只记录日志，不阻断 Session 收尾。

严重的相机、IO、识别任务或系统级异常由 `SystemRuntime` 统一处理并停止整个应用；普通单轮业务失败只结束当前 Session。

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
→ Machine 汇总结果
→ JPG 证据图片落盘
→ Database 写入 measurements
→ Session 结束
→ 等待下一轮。

GUI 的后台入口由 `MonitoringService`（`QThread`）管理，在实时监测页点击“启动监测”时创建；`SystemRuntime.start()` 接收两个 Qt 信号的 emit 回调，实时监测页据此更新机器卡片的连接状态和测量进度节点，频率值、最近事件和画面区仍为占位。

## 三、项目结构

* `src/system_runtime.py`：系统启动、机器初始化、IO 信号路由、全局异常和退出。
* `src/machine.py`：单台机器的事件队列、Session 生命周期、OCR 调度和最终结算。
* `src/camera/camera.py`：单轮图像采集。
* `src/camera/hikrobot_sdk.py`：海康 MVS SDK、取帧、JPG 编码和相机关闭。
* `src/text_recognition.py`：筛帧、OCR、文字与证据图片终选。
* `src/frequency_adapter.py`：频率采集及当前 Session 归属。
* `src/modbus_client.py`：Modbus RTU 串口连接和 DI 状态读取。
* `src/database.py`：数据库初始化、测量记录、异常审计和实例锁。
* `src/repo/`：机器等基础数据访问。
* `src/service/`：GUI 与后端之间的业务服务。
* `src/models.py` / `src/enums.py`：Session、事件、帧、OCR 结果及状态定义。
* `src/config_util.py`：YAML 配置读取与校验。
* `src/main.py`：命令行测量入口，程序化发送 START/CLOSE 做整轮联调。
* `src/async_utils.py`：在线程中执行阻塞操作的公共封装。
* `ui/`：PySide6 桌面界面，包括实时监测、机器管理及其他业务页面。
