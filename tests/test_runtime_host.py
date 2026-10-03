"""验证无界面宿主的并发命令、完整快照与独立监测生命周期。"""

import asyncio
from collections import OrderedDict
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from enums import ProgressStage, ProgressStatus, SessionState
from runtime.hardware_lock import HardwareOwnershipError, HardwareOwnershipLock
from runtime.runtime_host import RuntimeHost
from src.controller.result import Result


MACHINE = {
    "id": 1,
    "machine_name": "一号机",
    "camera_serial": "CAM1",
    "frequency_meter_serial": "FREQ1",
    "enabled": True,
}


class FakeRuntime:
    """通过线程安全事件控制启动和清理的设备替身。"""

    def __init__(self, start_gate: Event | None = None, stop_gate: Event | None = None):
        """创建无硬件运行状态和可观察的生命周期标志。

        Args:
            start_gate: 启动放行事件，省略则立即完成。
            stop_gate: 清理放行事件，省略则立即完成。

        Returns:
            None  # 运行替身尚未启动
        """
        self.start_gate = start_gate
        self.stop_gate = stop_gate
        self.start_entered = Event()
        self.stop_entered = Event()
        self.failure = None
        self.cleanup_failed = False
        self.machines = {}
        self.start_count = 0
        self.stop_count = 0

    async def start(self, *callbacks):
        """登记启动并等待测试放行。

        Args:
            callbacks: 真实宿主提供的六个状态通知回调。

        Returns:
            None  # 运行替身已经启动
        """
        self.start_count += 1
        self.start_entered.set()
        while self.start_gate is not None and not self.start_gate.is_set():
            await asyncio.sleep(0.005)

    async def stop(self):
        """登记清理并等待测试放行。

        Args:
            无外部参数。

        Returns:
            None  # 运行替身已完成清理
        """
        self.stop_count += 1
        self.stop_entered.set()
        while self.stop_gate is not None and not self.stop_gate.is_set():
            await asyncio.sleep(0.005)


@pytest.fixture
def isolated_ownership(tmp_path, monkeypatch):
    """将进程锁放入本测试目录，避免与其他回归进程互相干扰。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 替换工具。

    Returns:
        None  # 同一测试内的所有配置仍共享同一个锁文件
    """
    original = HardwareOwnershipLock.__init__
    def initialize(lock):
        """保留真实操作系统锁，只替换测试锁路径。

        Args:
            lock: 当前设备锁实例。

        Returns:
            None  # 测试锁路径已设置
        """
        original(lock)
        lock.path = tmp_path / "hardware.lock"
    monkeypatch.setattr(HardwareOwnershipLock, "__init__", initialize)


def create_host(tmp_path, runtime, **options):
    """使用配置与运行时替身建立宿主。

    Args:
        tmp_path: pytest 临时目录。
        runtime: 无硬件运行时替身。
        options: 宿主容量等可选参数。

    Returns:
        RuntimeHost(...)  # 已注入隔离依赖的宿主
    """
    return RuntimeHost(
        tmp_path,
        lambda: {"machines": [MACHINE]},
        runtime_factory=lambda config, **kwargs: runtime,
        configuration_loader=lambda directory: object(),
        **options,
    )


async def wait_until(predicate):
    """在有限测试期限内等待跨线程状态生效。

    Args:
        predicate: 返回状态是否已满足的函数。

    Returns:
        None  # 条件已满足，否则测试超时失败
    """
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_concurrent_start_stop_is_idempotent_and_holds_mutation_guard(tmp_path, isolated_ownership):
    """反复启停不会重复构造任务，停止清理完成前持续拒绝写入。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # Runtime 仅启动和停止一次，写入在清理后才恢复
    """
    start_gate, stop_gate = Event(), Event()
    runtime = FakeRuntime(start_gate, stop_gate)
    host = create_host(tmp_path, runtime)
    try:
        results = await asyncio.gather(*(host.start() for _ in range(8)))
        assert all(result.success for result in results)
        await wait_until(runtime.start_entered.is_set)
        assert host.status == "starting"
        assert runtime.start_count == 1
        assert not (await host.run_mutation(lambda: Result.ok())).success

        # 启动未完成时的停止请求不会被随后启动成功覆盖。
        await asyncio.gather(*(host.stop() for _ in range(8)))
        assert host.status == "stopping"
        assert not (await host.start()).success
        start_gate.set()
        await wait_until(runtime.stop_entered.is_set)
        assert not (await host.run_mutation(lambda: Result.ok())).success
        stop_gate.set()
        await wait_until(lambda: host.task is None)
        assert runtime.stop_count == 1
        assert host.status == "stopped"
        assert (await host.run_mutation(lambda: Result.ok())).success
    finally:
        start_gate.set()
        stop_gate.set()
        await host.shutdown()


@pytest.mark.asyncio
async def test_disconnected_sse_client_does_not_stop_monitoring(tmp_path, isolated_ownership):
    """客户端断线只释放订阅，监测继续到显式停止。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 运行生命周期不受 SSE 连接数量影响
    """
    runtime = FakeRuntime()
    host = create_host(tmp_path, runtime)
    await host.start()
    await wait_until(lambda: host.status == "running")
    queue = host.subscribe()
    assert (await queue.get())["status"] == "running"
    host.unsubscribe(queue)
    assert host.status == "running"
    assert runtime.stop_count == 0
    await host.shutdown()
    assert runtime.stop_count == 1


@pytest.mark.asyncio
async def test_bounded_clients_receive_complete_latest_snapshot_after_gap(tmp_path):
    """慢客户端只保留最新完整状态，重新连接也无需旧事件历史。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        None  # 连接上限、队列上限和完整恢复均已验证
    """
    host = create_host(tmp_path, FakeRuntime(), max_clients=1, queue_capacity=2)
    await host.refresh_machines()
    queue = host.subscribe()
    with pytest.raises(RuntimeError):
        host.subscribe()
    for index in range(7):
        host.sessions = [{"machine_id": "1", "session_id": f"cycle-{index}"}]
        host.publish_snapshot()
    assert queue.qsize() <= 2
    snapshots = []
    while not queue.empty():
        snapshots.append(queue.get_nowait())
    assert snapshots[-1]["sessions"][0]["session_id"] == "cycle-6"
    assert snapshots[-1]["machines"][0]["id"] == "1"
    host.unsubscribe(queue)
    reconnected = host.subscribe()
    assert reconnected.get_nowait() == host.snapshot()
    await host.shutdown()
    assert reconnected.get_nowait() is None


@pytest.mark.asyncio
async def test_overlapping_cycles_keep_final_frequency_and_order(tmp_path):
    """旧周期晚到结果不会覆盖新现场周期，结束快照保留频率和识别文字。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        None  # 两个周期的阶段、结果和顺序保持独立
    """
    host = create_host(tmp_path, FakeRuntime())
    host.loop = asyncio.get_running_loop()
    await host.refresh_machines()
    machines = {"1": host.machine_states[0]}
    sessions = OrderedDict()
    old = SimpleNamespace(
        state=SessionState.RUNNING, start_time="2026-10-03T10:00:00Z", finish_time=None,
        errors=[], final_frequency=SimpleNamespace(value_hz=50.25),
    )
    new = SimpleNamespace(
        state=SessionState.RUNNING, start_time="2026-10-03T10:00:01Z", finish_time=None,
        errors=[], final_frequency=None,
    )
    machine = SimpleNamespace(
        active_session_id="old",
        waiting_cycle_reset=False,
        cycles={"old": SimpleNamespace(session=old)},
    )
    runtime = SimpleNamespace(machines={"1": machine})
    _, progress, ocr, closed, _, _, _ = host.create_callbacks(machines, sessions, runtime)
    progress("1", "old", ProgressStage.SESSION_START, ProgressStatus.SUCCESS)
    closed("1", "old")
    machine.active_session_id = "new"
    machine.cycles["new"] = SimpleNamespace(session=new)
    progress("1", "new", ProgressStage.SESSION_START, ProgressStatus.SUCCESS)
    old.state = SessionState.COMMITTED
    ocr("1", "old", ("ABC",))
    progress("1", "old", ProgressStage.EVIDENCE_STORAGE, ProgressStatus.SUCCESS)
    del machine.cycles["old"]
    host.collect_runtime_snapshot(runtime, machines, sessions)
    await asyncio.sleep(0)
    snapshot = host.snapshot()
    assert snapshot["machines"][0]["active_session_id"] == "new"
    assert [session["session_id"] for session in snapshot["sessions"]] == ["old", "new"]
    assert snapshot["sessions"][0]["final_frequency_hz"] == 50.25
    assert snapshot["sessions"][0]["recognized_lines"] == ["ABC"]
    assert snapshot["sessions"][0]["state"] == "COMMITTED"
    assert snapshot["sessions"][1]["recognized_lines"] == []


@pytest.mark.asyncio
async def test_cancelled_write_keeps_start_serialized_until_write_finishes(tmp_path, isolated_ownership):
    """HTTP 请求取消不允许后台写入尚未完成时启动监测。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 取消后仍等待写入线程结束才释放启动锁
    """
    host = create_host(tmp_path, FakeRuntime())
    entered, finished = Event(), Event()
    def mutate():
        """模拟可控的配置落盘过程。

        Args:
            无外部参数。

        Returns:
            Result(...)  # 测试配置写入完成
        """
        entered.set()
        finished.wait(3)
        return Result.ok()
    mutation = asyncio.create_task(host.run_mutation(mutate))
    await wait_until(entered.is_set)
    mutation.cancel()
    start = asyncio.create_task(host.start())
    await asyncio.sleep(0.01)
    mutation.cancel()
    await asyncio.sleep(0.02)
    assert not start.done()
    finished.set()
    with pytest.raises(asyncio.CancelledError):
        await mutation
    assert (await start).success
    await host.shutdown()


@pytest.mark.asyncio
async def test_cleanup_failure_retains_owner_and_blocks_restart(tmp_path, isolated_ownership):
    """设备清理失败后保留所有权，禁止重启或编辑直到进程退出。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 清理失败明确可见，其他实例仍无法取得设备
    """
    runtime = FakeRuntime()
    runtime.cleanup_failed = True
    host = create_host(tmp_path, runtime)
    try:
        await host.start()
        await wait_until(lambda: host.status == "running")
        await host.stop()
        await wait_until(lambda: host.task is None)
        assert host.cleanup_failed
        assert host.is_active
        assert host.status == "failed"
        assert not (await host.start()).success
        assert not (await host.run_mutation(lambda: Result.ok())).success
        with pytest.raises(HardwareOwnershipError):
            HardwareOwnershipLock().acquire()
    finally:
        if host.retained_lock:
            host.retained_lock.release()
        await host.shutdown()


@pytest.mark.asyncio
async def test_start_failure_releases_owner_without_starting_devices(tmp_path, isolated_ownership):
    """启动配置读取失败时释放所有权，允许修正配置后再次启动。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 故障已展示且未创建任何硬件运行对象
    """
    runtime = FakeRuntime()
    host = create_host(tmp_path, runtime)
    host.configuration_loader = Mock(side_effect=ValueError("测试配置错误"))
    await host.start()
    await wait_until(lambda: host.task is None)
    assert host.status == "failed"
    assert host.failure == "测试配置错误"
    assert runtime.start_count == 0
    with HardwareOwnershipLock():
        assert not host.is_active
    await host.shutdown()


def test_snapshot_schema_rejects_missing_session_identity():
    """显式快照模型拒绝不能按周期恢复的事件数据。

    Args:
        无外部参数。

    Returns:
        None  # 缺失 Session ID 的周期不会被当作有效 SSE 数据
    """
    from pydantic import ValidationError
    from src.api.models import RuntimeSession
    with pytest.raises(ValidationError):
        RuntimeSession.model_validate({"machine_id": "1", "state": "RUNNING"})


@pytest.mark.asyncio
async def test_repeated_monitor_cancellation_waits_for_real_cleanup(tmp_path, isolated_ownership):
    """多次取消宿主任务仍先停止工作线程并等清理结束。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 取消期间保持停止中，真正清理后才解除写入保护
    """
    stop_gate = Event()
    runtime = FakeRuntime(stop_gate=stop_gate)
    host = create_host(tmp_path, runtime)
    await host.start()
    await wait_until(lambda: host.status == "running")
    task = host.task
    try:
        task.cancel()
        await wait_until(runtime.stop_entered.is_set)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert host.status == "stopping"
        assert host.is_active
        assert not (await host.run_mutation(lambda: Result.ok())).success
        stop_gate.set()
        await task
        assert runtime.stop_count == 1
        assert host.task is None
        assert not host.is_active
    finally:
        stop_gate.set()
        await host.shutdown()


@pytest.mark.asyncio
async def test_machine_baseline_is_read_under_hardware_ownership(tmp_path, isolated_ownership):
    """机器列表与运行配置在相同所有权锁内读取，其他实例不能插入新机器。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 构造回调的机器基线不会落后于真实运行时机器列表
    """
    entered, finish_read = Event(), Event()
    def read_machines():
        """在持锁读取期间等待测试检查竞争状态。

        Args:
            无外部参数。

        Returns:
            dict  # 当前机器配置快照
        """
        entered.set()
        finish_read.wait(3)
        return {"machines": [MACHINE]}
    host = create_host(tmp_path, FakeRuntime())
    host.machine_reader = read_machines
    await host.start()
    try:
        await wait_until(entered.is_set)
        with pytest.raises(HardwareOwnershipError):
            HardwareOwnershipLock().acquire()
        finish_read.set()
        await wait_until(lambda: host.status == "running")
        assert host.snapshot()["machines"][0]["id"] == "1"
    finally:
        finish_read.set()
        await host.shutdown()


@pytest.mark.asyncio
async def test_reclaimed_failed_cycle_keeps_terminal_metadata(tmp_path):
    """真实周期回收入口在轮询前结束时仍交付全部终态字段。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        None  # 最后错误、结束时间和频率不会被快速回收丢失
    """
    from runtime.machine_runtime import CycleContext, MachineRuntime
    host = create_host(tmp_path, FakeRuntime())
    host.loop = asyncio.get_running_loop()
    await host.refresh_machines()
    machines = {"1": host.machine_states[0]}
    sessions = OrderedDict()
    session = SimpleNamespace(
        session_id="fast-failed", machine_id="1", state=SessionState.RUNNING,
        start_time="start", finish_time=None, errors=[], final_frequency=None, ocr_result=None,
    )
    machine = MachineRuntime.__new__(MachineRuntime)
    machine.machine_config = SimpleNamespace(machine_name="一号机")
    machine.active_session_id = None
    machine.waiting_cycle_reset = False
    machine.cycles = {session.session_id: CycleContext(session)}
    machine.state_changed = asyncio.Event()
    runtime = SimpleNamespace(machines={"1": machine})
    callbacks = host.create_callbacks(machines, sessions, runtime)
    machine.notify_session_finished = callbacks[6]
    callbacks[1]("1", session.session_id, ProgressStage.IMAGE_CAPTURE, ProgressStatus.FAILED)
    session.state = SessionState.FAILED
    session.errors = ["本轮未采集到图像"]
    session.finish_time = "finish"
    session.final_frequency = SimpleNamespace(value_hz=42.5)
    machine.release_finished_session(session.session_id)
    await asyncio.sleep(0)
    result = host.snapshot()["sessions"][0]
    assert result["state"] == "FAILED"
    assert result["errors"] == ["本轮未采集到图像"]
    assert result["finish_time"] == "finish"
    assert result["final_frequency_hz"] == 42.5
    assert host.snapshot()["machines"][0]["inflight_count"] == 0


@pytest.mark.asyncio
async def test_group_cancellation_cannot_orphan_executor_work(tmp_path, isolated_ownership):
    """模拟事件循环退出时取消所有新任务，真实监测线程仍完成释放。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 所有 asyncio 任务结束前真实 Runtime.stop 已完成
    """
    runtime = FakeRuntime()
    host = create_host(tmp_path, runtime)
    original_tasks = asyncio.all_tasks()
    await host.start()
    await wait_until(lambda: host.status == "running")
    monitoring_tasks = asyncio.all_tasks() - original_tasks
    for task in monitoring_tasks:
        task.cancel()
    await asyncio.gather(*monitoring_tasks, return_exceptions=True)
    assert runtime.stop_count == 1
    assert host.task is None
    assert not host.is_active
    await host.shutdown()


@pytest.mark.asyncio
async def test_cancel_before_first_run_does_not_leave_starting_state(tmp_path, isolated_ownership):
    """任务首次调度前取消时不残留启动锁，也不创建硬件运行实例。

    Args:
        tmp_path: pytest 临时目录。
        isolated_ownership: 使用临时锁路径的测试准备。

    Returns:
        None  # 宿主回到停止状态，退出流程可以完成
    """
    runtime = FakeRuntime()
    host = create_host(tmp_path, runtime)
    await host.start()
    task = host.task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert runtime.start_count == 0
    assert host.task is None
    assert host.status == "stopped"
    await host.shutdown()
