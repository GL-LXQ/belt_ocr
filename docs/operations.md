# 运维与验证

[返回项目首页](../README.md)

以下命令均从仓库根目录执行，使用已按 README 准备好的 Python 环境。示例采用 Windows 路径，请替换为自己的路径；备份和恢复目标每次都使用全新名称。

## 备份业务记录和证据

```powershell
uv run --no-sync python scripts/backup_measurements.py --destination "D:\BeltBackups\snapshot-01"
```

默认读取 `config/config.yaml`，以只读连接生成业务库一致快照，包含机器、测量和人工复核信息，再复制快照引用目录中的全部 JPG/JPEG，而非界面预览的前四张。不会启动监测，也不依赖相机连接。

快照通过 SQLite `integrity_check` 后，文件大小、SHA-256、记录数量和图片映射写入 `manifest.json`。全部校验成功才将 `.partial` 暂存目录发布为正式备份；源库和源图片不修改、不清空，已有目标不覆盖。失败时保留暂存目录供检查，请换新目标重试。

可选来源和期限：

- `--config-dir "D:\BeltConfig"`：指定包含 `config.yaml` 的目录。
- `--database "D:\BeltData\measurements.sqlite3" --evidence-root "D:\BeltData\evidence"`：同时覆盖两个来源，不依赖默认配置。
- `--timeout-seconds 30`：调整 SQLite 快照最长等待时间，默认 10 秒。

命令行相对路径以当前工作目录为基准；配置内相对路径仍以配置文件所在目录为基准。

## 独立校验

```powershell
uv run --no-sync python scripts/backup_measurements.py --verify "D:\BeltBackups\snapshot-01"
```

校验不读取生产配置。应在备份完成、传输到其他介质后，以及恢复前检查；失败的备份不得用于正式恢复。校验覆盖清单结构、数据库完整性、记录数量、文件哈希和证据映射。

## 恢复到新目录

```powershell
uv run --no-sync python scripts/backup_measurements.py --restore "D:\BeltBackups\snapshot-01" "D:\BeltRestore\snapshot-01"
uv run --no-sync python scripts/backup_measurements.py --verify "D:\BeltRestore\snapshot-01"
```

目标必须尚不存在，不能位于备份或原证据目录内部；已有目录（即使为空）、文件和链接均不覆盖。恢复不需要原数据库、原图片路径或生产配置，也不启动监测。

恢复先完整校验备份，再复制业务库和全部清单图片。仅将副本中每轮记录的 `evidence_directory` 改为新目标中的绝对路径，机器配置、文字、频率明细、时间和复核字段保持原值；重新校验后生成新清单。原库、原备份和生产配置均不自动切换。

恢复成功后，由操作者让独立测试配置指向新目录中的 `measurements.sqlite3`，核对查询和证据，再决定是否切换生产配置。失败时会保留不完整目标并报告路径，不能把失败目录用于查询；检查后换新目录重试。再次迁移时重新恢复到另一个新目录，不直接移动已恢复目录。

### 备份范围与限制

- 仅备份业务库及记录引用的证据，不包括独立异常运行库、模型和程序配置。
- 记录只保存证据目录，无法判断备份前是否已丢失其中某张图片；目录缺失、没有 JPG/JPEG、复制时文件内容变化或备份后文件损坏均会失败。证据目录中的嵌套目录和符号链接也会被拒绝。
- 恢复适用于当前无触发器的业务库，不对自行增加的数据库触发器副作用作专门隔离。
- 工具不执行定时备份、月度切换、分表或历史清理。

## 桌面与浏览器开发

Tauri 开发版只调用仓库 `.venv` 的 Python 3.12，源码入口是 `python -m src.api --desktop`，不是系统 `python`。启动前可设置以下绝对路径，分别覆盖业务配置目录和 OCR 配置文件；配置目录内必须已有 `config.yaml`：

```powershell
$env:BELTVISION_CONFIG_DIR = "D:\BeltDev\config"
$env:BELTVISION_OCR_CONFIG = "D:\BeltDev\ocr.yaml"
cd desktop
npm run tauri:dev
```

请使用独立开发配置，所有数据库、运行库、证据与模型路径都由操作者明确核对。软件会初始化配置指定的表，不会自动复制仓库生产数据或启动监测。

仅需浏览器联调时，在仓库根目录开两个终端：

```powershell
uv run --no-sync python -m src.api --config-dir "D:\BeltDev\config" --ocr-config "D:\BeltDev\ocr.yaml" --port 8765
```

```powershell
cd desktop
npm run dev
```

访问 Vite 显示的本机地址，默认 `http://127.0.0.1:1420`。后端就绪时在第一个终端输出一次 JSON 握手；把其中 `token` 粘贴到页面连接框。令牌只在本次进程和页面内存中生效，刷新页面需重新输入；不要把握手重定向到日志、提交到仓库或写入 `VITE_*`、URL、localStorage。Vite 只把 `/api` 代理到 `127.0.0.1:8765`；若改变后端端口，同时修改开发代理或仅将非敏感接口地址配置为 `VITE_API_BASE_URL`。

浏览器持有令牌后有同样的本机业务操作权限，包括启动监测和写配置；它不是无副作用演示。浏览器不开放原生文件夹命令。接口、SSE 和图片均使用 Authorization Bearer 请求，图片通过鉴权读取后生成临时 blob URL。SSE 重连会重取当前状态，不能把连接中断期间的陈旧画面当作当前状态。

### 退出与故障处理

- Tauri 通过 stdin 传递一次性令牌，不将其放入命令行、环境变量或日志；仅在握手确认直接子进程 PID、回环地址及鉴权健康检查成功后交给页面。
- 正常关闭窗口调用鉴权 `/api/v1/shutdown`，并等待后台采集、OCR、证据保存、异常审计及实际进程退出。等待期间保持窗口显示；不要把 Windows 的“结束任务”当作正常退出。
- 父进程异常消失时，stdin EOF 触发相同清理；无响应的原生驱动或解释器崩溃仍可能无法完成。清理失败或进程异常退出会显示失败，不宣称数据已安全保存。
- 后端仍存活时禁用重试，不自动创建第二个实例。启动超时会请求清理但不会定时强杀。清理完成并确认旧 PID 已退出后才能重试；缺失的配置可修复后重试，修改环境路径须重启应用。
- “打开证据目录”只传 `session_id` 给 Rust。后端从该记录已保存路径解析并验证目录，Rust 再检查本机目录后调用固定文件管理器；不向页面开放任意路径、Shell 或文件系统权限。
- 不要通过 Uvicorn `--reload`、多个 workers 或多份进程包装器启动现场后端；开发热更新只用于 Vue 页面。设备占用锁覆盖本版本、同一用户临时目录下的多个配置和入口；无法约束尚未更新的旧客户端或其他系统用户会话。升级前完全退出旧程序，不得跨用户并行操作同一硬件。桌面单实例机制不能代替操作者核对资源。

## 打包与部署

先按 [Tauri 平台前提](https://v2.tauri.app/start/prerequisites/) 安装目标系统构建工具。PyInstaller 必须在目标 OS/架构上构建；Linux 构建成功不能证明 Windows 安装器、MVS DLL 或 GPU 原生库可用。

在 Python 3.12 环境同步项目依赖并打包，默认自动安装锁定的 CPU 版 PaddlePaddle：

```powershell
uv sync --frozen --group build
uv run --no-sync python scripts/build_backend.py --check
uv run --no-sync python scripts/build_backend.py
cd desktop
npm ci
npm run tauri:build -- --config src-tauri/tauri.bundle.conf.json
```

PaddlePaddle 由项目依赖和锁文件管理，无需单独安装。`--check` 仅检查解释器与模块是否存在，不加载模型或连接硬件。脚本调用 PyInstaller 单目录模式，将可执行文件和 `_internal` 依赖整体放在 `desktop/src-tauri/backend-runtime/`；已有暂存目录不会自动覆盖，重新构建前先移走旧产物。Tauri 的发布覆盖配置把整个目录按相同资源路径打包，Rust 只启动这个固定 sidecar，不调用目标机任意 Python。基础 Tauri 配置关闭安装器打包，适合开发编译；Windows NSIS 安装器必须显式传入上述发布配置。

开发环境未装 PaddlePaddle 时，可以加 `--development-without-ocr` 验证 API sidecar 打包；这种产物明确不能用于现场监测。正式包收集 Paddle/PaddleOCR/PaddleX 的 Python 与原生文件，但 GPU 驱动、CUDA/CUDNN、MVS Runtime、模型文件和许可证仍须按厂商要求在目标机准备与验收，不承诺自动携带所有外部依赖。当前没有已验证的 Windows 安装器或真机联调结论。

### 发布版配置位置

发布版不读取仓库路径，不自带现场配置、数据库、模型或证据。默认使用 Tauri `app_config_dir()` 下的 `config.yaml` 和 `ocr.yaml`，应用标识为 `com.beltvision.desktop`。Windows 默认是 `%APPDATA%\com.beltvision.desktop`；其他平台由系统应用配置目录决定。操作员需先准备文件，并为可写数据选择独立目录。也可在启动应用前用绝对路径 `BELTVISION_CONFIG_DIR` 和 `BELTVISION_OCR_CONFIG` 明确覆盖；Rust 会将解析后的路径通过 stdin 交给 sidecar。

安装目录中的 sidecar 与依赖只作为程序资源。业务配置内的相对路径继续以 `config.yaml` 所在目录为基准，不以安装目录或终端当前目录为基准。模型路径请配置为目标机可用的明确路径。升级前备份数据库和证据，先用独立配置验证，再由操作者切换。

## 开发验证

改了什么就测什么，选择覆盖本次改动的测试文件或节点，不默认运行全量 pytest。Python、前端和 Rust 可分别验证：

```powershell
uv run --no-sync python -m pytest -q tests/test_build_backend.py
cd desktop
npm run typecheck
npm test
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --no-default-features
cargo check --manifest-path src-tauri/Cargo.toml
cd ..
git diff --check
```

Rust `--no-default-features` 只验证不依赖 WebView 的进程监督逻辑；完整 `cargo check`/`cargo build` 仍需平台 GTK/WebKit 或 Windows C++ 工具。单元测试不等于界面像素验收，也不代替相机、Modbus DI、模型、GPU 与真实设备联调。旧 Qt 测试仅归档，不在当前默认 pytest 回归范围；需要使用无 Qt 的服务层与 API 测试验证保留的业务语义。项目不再依赖 PySide/PyQt 或 Fluent Widgets；PaddleX 官方要求的 OpenCV 发行包在部分平台可能携带原生 GUI 库，它不是旧 Python Qt 界面依赖。不要并装多个共享 `cv2` 命名空间的 OpenCV 发行包。

## 模拟一轮测量

**这不是只读演示：脚本调用真实 OCR，并将测量记录和 JPG 证据写入 `config/config.yaml` 指向的业务库、证据目录，同时使用配置中的运行库。运行前停止监测、备份数据，并确认目标；若需隔离，请在独立项目副本配置独立的数据库和证据路径。脚本没有切换配置目录的命令行参数。**

确保目标业务库至少有一台已启用机器，且项目环境中的 PaddlePaddle、模型与 `src/ocr/config.yaml` 可用后，按需要选择一个场景：

```powershell
uv run --no-sync python scripts/simulate_measurement.py normal
uv run --no-sync python scripts/simulate_measurement.py review
```

- `normal`：读取 `statistics/imgs` 中的 JPG。
- `review`：读取 `statistics/test_images_without_results` 中按文件名排序的首张 BMP。

脚本选择第一台启用机器，用 Mono8 图片帧替代相机取流，用模拟输入完成 START、50.0 Hz 频率和 CLOSE；图片编码由 OpenCV 替代，不启动真实 MVS 或 Modbus。相机、识别和单机事件处理仍沿现有业务流程执行，最终结果取决于真实 OCR 输出。

成功时终端输出周期编号、识别文字、频率、复核状态和证据目录，可在历史记录／图片管理中核对对应记录。不要将模拟数据误认为现场实测数据。
