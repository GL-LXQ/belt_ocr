# 第 22 节验收测试对照

本表对应 README 的 20 个验收场景，范围为当前模拟适配器和实际业务、恢复、SQLite 保存逻辑。
测试通过不代表真实设备协议、IO 电平去抖、OCR 算法或现场持续运行已经验收。

2026-09-17 完整执行结果：**51 项测试全部通过，无跳过，耗时 61.778 秒**。
其中新增 15 项验收测试，并强化原有通道绑定、提交内容一致性、超时和存储队列测试。
本次修复了无周期身份频率的歧义审计分类，以及周期身份冲突未阻止正常结算的问题。
本地执行日志位于 `runtime/acceptance-final-test.log`，该运行目录不纳入版本管理。

## 执行方法

在项目目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

测试使用临时图片目录和独立 SQLite 文件。进程崩溃测试启动独立 Python 子进程，在检查点落盘后调用 `os._exit`，随后由新应用实例读取原库恢复。
跨周期 OCR 和提交回调通过事件等待点控制先后顺序，不依靠固定延迟碰巧触发。
异步业务测试与 `main.py` 一样关闭 asyncio 调试模式；保留所有业务断言、实际期限任务和异常退出检查。
性能诊断中，Windows 上 asyncio 调试堆栈的文件状态查询占单次三机测试约 8.7 / 10.1 秒，会干扰短时限场景；正式模式不执行这些调试查询。

测试文件：

- [核心流程](tests/test_measurement_flow.py)
- [恢复与故障](tests/test_recovery_and_faults.py)
- [新增验收场景](tests/test_acceptance_scenarios.py)

## 场景与断言

表中省略测试函数共有的 `test_` 前缀。新增验收测试名包含对应 README 编号。

| 编号 | 验证内容 | 对应测试 |
|---|---|---|
| 1 | 同时创建三份 Session；最终记录中的机器、相机、频率通道分别对应配置；错误通道与错误事件来源不能混入候选。 | `three_machines_save_independent_records`；`01_bound_sources_reject_cross_machine_data` |
| 2 | OCR 完成但未关闭时，数据库仍无测量记录。 | `three_machines_save_independent_records` |
| 3 | 暂停旧轮识别，关闭后活动位置立即释放，新轮可以开始。 | `03_04_05_late_ocr_and_commit_preserve_new_cycle` |
| 4 | 新轮开始后才释放旧轮 OCR；两轮使用不同文字，最终各自保存正确文字与图像。 | `03_04_05_late_ocr_and_commit_preserve_new_cycle` |
| 5 | 旧轮已写库、提交确认仍暂停时，新轮保持活动；释放确认后仍不修改新轮绑定。 | `03_04_05_late_ocr_and_commit_preserve_new_cycle` |
| 6 | 业务入口连续并发接收 40 次启动，只有一个 Session 和一条正常结果。 | `06_repeated_start_input_creates_one_cycle` |
| 7 | 新轮活动期间重放同一关闭事件，再以新事件 ID 重发旧轮关闭，均不关闭新轮；两轮各保存一次。 | `07_replayed_close_does_not_close_the_next_cycle`；`stale_close_cannot_close_a_new_cycle` |
| 8 | 同一帧、频率和逐帧 OCR 结果重复投递不增加任务和候选；同事件 ID 重放、新事件 ID 重发、识别重试均不增加最终记录。 | `08_replayed_frame_results_do_not_duplicate_jobs_or_lines`；`frame_retry_is_persisted_and_not_counted_twice`；`duplicate_event_is_rejected_after_restart` |
| 9 | 同轮多次及相邻两轮测量数值相同，仍按不同测量 ID 和 Session 保存，分别选择本轮最终值。 | `equal_frequency_values_have_different_measurement_ids`；`09_identical_values_in_consecutive_cycles_keep_identity` |
| 10 | 旧显示值携带旧测量时间进入新周期，即使重贴新 Session 身份也被隔离；没有新测量时保存待复核，不沿用旧值。 | `10_previous_display_value_is_not_a_new_measurement` |
| 11 | 无 Session 身份的延迟频率只写歧义审计，不猜测分配；事件与测量中的周期身份冲突时，事件目标档案转待复核，不把读数转交另一轮。 | `11_unassigned_delayed_frequency_is_audited_without_guessing`；`11_conflicting_cycle_identity_cannot_produce_normal_record`；`frequency_identity_conflict_requires_review` |
| 12 | 无有效频率保存待复核；在途频率一直未送达时有限等待后记录封口超时。 | `no_valid_frequency_saves_review_record`；`frequency_drain_has_a_deadline` |
| 13 | 空文字保持失败状态，未关闭前不提交，关闭后只保存待复核结果。 | `empty_ocr_waits_for_close_then_saves_review_record` |
| 14 | 第一次真实 SQLite 写入后模拟确认丢失，重试后只有一条记录，完整内容与冻结提交内容一致。 | `lost_acknowledgement_does_not_duplicate_record` |
| 15 | 三台运行中的机器收到 IO 故障后保存中断记录，关闭时间为空；恢复通信或确认仍运行均不从中途创建正常周期；确认关闭后才能重新启动。另有运行中进程崩溃与初始 OPEN 测试。 | `15_io_loss_interrupts_cycles_without_fabricating_close`；`process_crash_marks_open_cycle_interrupted`；`initial_open_state_does_not_create_midcycle_session` |
| 16 | M01 积压六帧时，M02、M03 各自在 M01 第二帧前获得调度；队列容量超限有明确异常结果。 | `16_busy_machine_yields_ocr_to_other_machines`；`ocr_capacity_failure_creates_review_record` |
| 17 | 旧停止命令不封停新采集；旧帧送入新 Session 被拒绝，旧采集封口不修改新轮活动位置。 | `early_close_seals_only_the_old_capture`；`17_20_late_frames_respect_capture_identity_and_close_boundary` |
| 18 | 已关闭且封口完整的任务在强制退出后以原 Session 和证据续办；未关闭任务在强制退出后标记中断。待提交冻结内容也经过退出重启验证。 | `18_closed_cycle_survives_forced_process_exit`；`process_crash_marks_open_cycle_interrupted`；`pending_payload_survives_shutdown_and_restart` |
| 19 | 分别注入证据写入、同步、原子替换失败，均保存待复核并清理临时文件；OCR 前或成功后证据丢失也不能正常提交。 | `19_evidence_write_sync_and_replace_failures_require_review`；`19_evidence_lost_after_ocr_cannot_be_committed_as_complete`；`missing_evidence_cannot_be_saved_as_complete` |
| 20 | 提前关闭真实文件夹采集；按窗口身份与关闭边界拒绝启动前、关闭后及封口后的帧，最终只保存符合归属规则的图像。 | `early_close_seals_only_the_old_capture`；`17_20_late_frames_respect_capture_identity_and_close_boundary` |

## 验证边界

- 第 6 项验证业务入口的重复启动处理。物理输入持续有效、边沿识别、接点抖动需在确定 IO 接入后验证。
- 第 8 项验证任务和结果的幂等处理。OCR 仍返回模拟的已筛选文字，实际识别和投票融合不在当前实现范围内。
- 第 10、11 项注入的是已经解析的测量身份与时间。真实仪器能否提供可信的测量身份、如何区分旧显示与新测量，需结合实际协议验证。
- 第 15 项注入设备健康事件；真实断线检测速度和重连流程需接入设备验证。
- 第 17、20 项遵循 README 第 8 节：已明确属于旧窗口、关闭前采到的帧可在封口前延迟送达；关闭后产生或封口后到达的帧不进入该轮。真实 SDK 的回调和时间映射尚未接入。
- 本地临时文件、SQLite 事务与子进程恢复均实际执行；磁盘错误由故障注入触发，没有实施物理断电或破坏磁盘。
- 有限测试不替代现场长时间运行、吞吐量和硬件可靠性验收。
