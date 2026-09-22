"""验证 Machine 自行等待事件队列排空并丢弃退出时的待处理事件。"""

import asyncio
from pathlib import Path
from unittest.mock import patch

from camera.camera import Camera
from config_util import MachineConfig
from database import Database
from enums import EventType
from frequency_adapter import FrequencyAdapter
from local_test_support import FakeMvsSdk, build_config, create_machine_database
from machine import Machine
from models import RuntimeEvent
from system_runtime import SystemRuntime
from text_recognition import TextRecognizer


def build_machine(directory: Path) -> Machine:
    """组装一台未启动后台任务的机器运行对象，用于单独检查事件队列行为。

    Args:
        directory: 测试临时目录。

    Returns:
        返回示例：
            Machine  # 事件队列已按配置容量创建，尚未启动事件处理任务
    """
    async def ignore_event(event: RuntimeEvent) -> None:
        """丢弃测试期间路由到事件入口的事件。

        Args:
            event: 待丢弃的事件。

        Returns:
            返回示例：
                None  # 测试只核对队列状态
        """
        return None

    def ignore_error(error: Exception) -> None:
        """忽略测试期间登记的致命故障。

        Args:
            error: 待忽略的异常。

        Returns:
            返回示例：
                None  # 测试不检查故障内容
        """
        return None

    config = build_config(directory, event_queue_capacity=8)
    machine_config = MachineConfig(
        machine_id="1",
        camera_serial="CAM-A",
        frequency_meter_serial="FREQ-A",
    )

    # 组装采集器、频率适配器、共享 OCR 与存储入口，队列行为不依赖这些部件。
    return Machine(
        machine_config=machine_config,
        config=config,
        camera=Camera("1", config.capture_window_ms, config.camera_timeout_ms, ignore_event, ignore_error),
        frequency_adapter=FrequencyAdapter(machine_config, config, ignore_event),
        text_recognizer=TextRecognizer(),
        database=Database(config, ignore_event),
        publish_event=ignore_event,
        notify_measurement_progress=None,
        on_fatal_error=ignore_error,
        state_changed=asyncio.Event(),
    )


def test_worker_processes_queued_event(tmp_path: Path) -> None:
    """验证事件进入本机队列后仍由 worker 取出处理并完成回执。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 事件已被处理，处理回执已正常结束
    """
    machine = build_machine(tmp_path)
    handled_events: list[RuntimeEvent] = []

    async def record_event(event: RuntimeEvent) -> None:
        """记录 worker 取出的事件，替代真实业务分派。

        Args:
            event: worker 取到的事件。

        Returns:
            返回示例：
                None  # 事件已登记
        """
        handled_events.append(event)

    async def run() -> None:
        """启动 worker 后入队一条事件，等待回执结束。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 事件处理与回执等待已完成
        """
        machine.handle_event = record_event
        acknowledgement = asyncio.get_running_loop().create_future()
        worker = asyncio.create_task(machine.listen_events())

        # 按 SystemRuntime.publish_event() 相同的方式送入本机队列。
        await machine.queue.put(RuntimeEvent(EventType.MACHINE_STARTED, "1", acknowledgement=acknowledgement))

        # 回执由 worker 在处理结束时置结果。
        await asyncio.wait_for(acknowledgement, 2)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())

    assert [event.event_type for event in handled_events] == [EventType.MACHINE_STARTED]


def test_wait_until_event_queue_drained_keeps_join_semantics(tmp_path: Path) -> None:
    """验证等待排空会阻塞到事件处理结束，与原来的 queue.join() 一致。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 阻塞与放行两种情形都符合队列结算语义
    """
    machine = build_machine(tmp_path)
    handled_count = 0

    async def count_event(event: RuntimeEvent) -> None:
        """累计 worker 已处理的事件条数。

        Args:
            event: worker 取到的事件。

        Returns:
            返回示例:
                None  # 计数已增加
        """
        nonlocal handled_count
        handled_count += 1

    async def run() -> None:
        """先验证有未处理事件时等待会超时，再验证 worker 处理完后等待立即结束。

        Args:
            无外部参数。

        Returns:
            返回示例:
                None  # 等待排空的阻塞与结束行为均已确认
        """
        machine.handle_event = count_event

        # 没有消费者时两条待处理事件会让等待超时。
        await machine.queue.put(RuntimeEvent(EventType.MACHINE_CLOSED, "1"))
        await machine.queue.put(RuntimeEvent(EventType.MACHINE_CLOSED, "1"))
        blocked = False
        try:
            await asyncio.wait_for(machine.wait_until_event_queue_drained(), 0.05)
        except asyncio.TimeoutError:
            blocked = True
        assert blocked

        # 启动 worker 后等待排空，处理完成的三条事件全部结算。
        worker = asyncio.create_task(machine.listen_events())
        await machine.queue.put(RuntimeEvent(EventType.MACHINE_CLOSED, "1"))
        await asyncio.wait_for(machine.wait_until_event_queue_drained(), 2)
        assert handled_count == 3
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_discard_pending_events_cancels_acknowledgements(tmp_path: Path) -> None:
    """验证丢弃会取消未完成的处理回执，并保持队列任务计数正确。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例:
            None  # 队列已清空，后续等待排空不会卡住
    """
    machine = build_machine(tmp_path)

    async def run() -> None:
        """送入三条带回执的事件后丢弃，核对回执状态与队列结算计数。

        Args:
            无外部参数。

        Returns:
            返回示例:
                None  # 待处理事件已丢弃，等待排空立即结束
        """
        loop = asyncio.get_running_loop()

        # 前两条回执尚未完成，第三条已经处理结束。
        pending_first = loop.create_future()
        pending_second = loop.create_future()
        finished = loop.create_future()
        finished.set_result(None)
        for acknowledgement in (pending_first, pending_second, finished):
            await machine.queue.put(RuntimeEvent(EventType.MACHINE_CLOSED, "1", acknowledgement=acknowledgement))

        # 丢弃全部待处理事件。
        machine.discard_pending_events()
        assert machine.queue.empty()

        # 未完成的回执被取消，已完成的回执保持不变。
        assert pending_first.cancelled()
        assert pending_second.cancelled()
        assert finished.done() and not finished.cancelled()

        # 每条取出的事件恰好结算一次，等待排空不会永久阻塞。
        await asyncio.wait_for(machine.wait_until_event_queue_drained(), 1)

    asyncio.run(run())


def test_discard_leaves_in_flight_event_to_worker(tmp_path: Path) -> None:
    """验证丢弃只清理未取走的事件，正在处理的事件仍由 worker 结算。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例:
            None  # 在途事件由 worker 正常结束，队列计数没有超发
    """
    machine = build_machine(tmp_path)
    handled_events: list[RuntimeEvent] = []

    async def run() -> None:
        """让 worker 停在第一条事件处理中，丢弃第二条后核对队列结算。

        Args:
            无外部参数。

        Returns:
            返回示例:
                None  # 在途事件与待处理事件分别由 worker 与丢弃逻辑结算
        """
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_event(event: RuntimeEvent) -> None:
            """记录事件并等待放行，模拟 worker 正在处理中。

            Args:
                event: worker 取到的事件。

            Returns:
                返回示例:
                    None  # 事件已登记并放行
            """
            handled_events.append(event)
            entered.set()
            await release.wait()

        machine.handle_event = hold_event
        worker = asyncio.create_task(machine.listen_events())

        # 第一条事件被 worker 取走并停在处理中。
        first = RuntimeEvent(EventType.MACHINE_CLOSED, "1")
        await machine.queue.put(first)
        await asyncio.wait_for(entered.wait(), 2)

        # 第二条事件仍在队列中等待，丢弃时应当只清掉这一条。
        second = RuntimeEvent(EventType.MACHINE_CLOSED, "1")
        await machine.queue.put(second)
        machine.discard_pending_events()
        assert machine.queue.empty()
        assert handled_events == [first]

        # 放行在途事件，由 worker 自己结算队列计数。
        release.set()
        await asyncio.wait_for(machine.wait_until_event_queue_drained(), 2)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_shutdown_still_drains_machine_event_queues(tmp_path: Path) -> None:
    """验证退出流程仍在资源释放的两个阶段分别清理本机事件队列。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例:
            None  # 退出流程调用了等待排空与两次丢弃待处理事件
    """
    config = build_config(tmp_path, capture_window_ms=200, camera_timeout_ms=20)
    create_machine_database(config.database_path, [{
        "machine_name": "一号皮带机",  # 机器名称
        "camera_serial": "CAM-A",  # 相机序列号
        "frequency_meter_serial": "FREQ-A",  # 频率仪序列号
    }])
    sdk = FakeMvsSdk()

    async def run() -> None:
        """启动应用后登记队列方法计数，退出时核对调用次数。

        Args:
            无外部参数。

        Returns:
            返回示例:
                None  # 退出流程的队列清理调用次数已确认
        """
        with patch("system_runtime.load_mvs_sdk", lambda *arguments: sdk):
            system_runtime = SystemRuntime(config)
            await system_runtime.start()
            machine = system_runtime.machines["1"]

            discard_calls = 0
            original_discard = machine.discard_pending_events

            def count_discard_calls() -> None:
                """累加丢弃调用次数后执行原清理逻辑。

                Args:
                    无外部参数。

                Returns:
                    返回示例:
                        None  # 计数已增加，队列已清理
                """
                nonlocal discard_calls
                discard_calls += 1
                original_discard()

            machine.discard_pending_events = count_discard_calls

            wait_calls = 0
            original_wait = machine.wait_until_event_queue_drained

            async def count_wait_calls() -> None:
                """累加等待排空调用次数后执行原等待逻辑。

                Args:
                    无外部参数。

                Returns:
                    返回示例:
                        None  # 计数已增加，队列已排空
                """
                nonlocal wait_calls
                wait_calls += 1
                await original_wait()

            machine.wait_until_event_queue_drained = count_wait_calls

            await system_runtime.stop()

        # 正常收尾等待一次排空；释放阶段与任务取消后各丢弃一次，
        # 阻塞入队补偿分支可能再丢弃一次，因此次数不少于两次。
        assert wait_calls == 1
        assert discard_calls >= 2

    asyncio.run(run())
