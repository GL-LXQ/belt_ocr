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

## 开发验证

改了什么就测什么，选择足够覆盖本次改动的测试文件或节点，不默认运行全量 pytest。例如修改文字标准化时，可运行对应测试：

```powershell
uv run --no-sync python -m pytest -q tests/test_text_recognition_result.py::test_reliable_text_removes_whitespace_and_uppercases
git diff --check
```

pytest 用于代码回归；纯文档修改核对命令、路径和业务描述并运行 `git diff --check` 即可。单元测试不代替现场相机、Modbus DI、模型运行环境与真实设备联调。

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
