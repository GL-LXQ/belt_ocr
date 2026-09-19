"""验证设备故障日志、全局退出和资源释放。"""

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import DEFAULT, AsyncMock, Mock

import pytest

import main
from app import App
from configuration import MachineConfiguration, MeasurementConfiguration
from fake_mvs import FakeMvsSdk
from enums import EventType
from models import MeasurementEvent


@pytest.fixture
def device_environment(tmp_path, monkeypatch):
    """准备两台假相机和独立运行目录。

    Args:
        tmp_path: 测试临时目录。
        monkeypatch: 测试依赖替换工具。

    Returns:
        (configuration, sdk)  # 测量配置和可检查释放状态的相机驱动
    """
    # 建立两台机器和独立存储路径，频率默认无读数。
    machines = tuple(
        MachineConfiguration(
            machine_id=f"M{number}",
            camera_id=f"CAM{number}",
            frequency_source_id=f"FREQ{number}",
            camera_serial=f"SERIAL{number}",
        )
        for number in (1, 2)
    )
    configuration = MeasurementConfiguration(
        machines=machines,
        database_path=tmp_path / "results.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=Path("fake-sdk"),
        capture_window_ms=60000,
        shutdown_timeout_ms=100,
    )

    # 使用正式相机封装与假 SDK 连接应用。
    sdk = FakeMvsSdk()
    monkeypatch.setattr("app.load_mvs_sdk", lambda *arguments: sdk)
    return configuration, sdk


@pytest.mark.parametrize("failure_stage", ["driver", "serial", "second_camera"])
def test_startup_failure_closes_opened_devices(device_environment, monkeypatch, caplog, failure_stage):
    """验证启动失败记录日志并释放此前已打开的资源。

    Args:
        device_environment: 测试配置与假驱动。
        monkeypatch: 测试依赖替换工具。
        caplog: 日志捕获器。
        failure_stage: 驱动、序列号或第二台相机失败阶段。

    Returns:
        None  # 启动失败，已打开的设备和实例锁均已释放
    """
    # 按阶段注入启动故障。
    configuration, sdk = device_environment
    if failure_stage == "driver":
        monkeypatch.setattr("app.load_mvs_sdk", Mock(side_effect=OSError("驱动加载失败")))
    elif failure_stage == "serial":
        machines = (configuration.machines[0], replace(configuration.machines[1], camera_serial=""))
        configuration = replace(configuration, machines=machines)
    else:
        open_camera = sdk.open_camera
        monkeypatch.setattr(sdk, "open_camera", Mock(side_effect=[open_camera("SERIAL1"), OSError("连接失败")]))

    # 启动并检查失败后的设备与记录库状态。
    application = App(configuration)
    with pytest.raises((OSError, RuntimeError)):
        asyncio.run(application.start())
    assert not application.accepting_signals
    assert not application.worker_tasks
    assert not application.database.lock_acquired
    assert all(camera.closed for camera in sdk.cameras.values())
    assert "测量系统初始化失败" in caplog.text


def test_cancelled_startup_failure_preserves_error_and_cleanup(device_environment, monkeypatch, caplog):
    """验证启动取消后的设备异常仍保留，且资源清理完成后才退出。

    Args:
        device_environment: 测量配置与假驱动。
        monkeypatch: SDK 加载入口替换工具。
        caplog: 日志捕获器。

    Returns:
        None  # 原始异常已传播一次，数据库实例锁已释放
    """
    configuration, sdk = device_environment
    loading_started = threading.Event()
    release_loading = threading.Event()
    loading_failure = OSError("取消期间驱动加载失败")

    def fail_driver_loading(*arguments):
        """等待测试释放后抛出驱动异常。

        Args:
            arguments: SDK 目录配置。

        Returns:
            无返回值  # 等待结束后抛出 OSError
        """
        loading_started.set()
        assert release_loading.wait(5)
        raise loading_failure

    async def cancel_startup():
        """取消启动并核对原始故障和资源清理结果。

        Args:
            无外部参数。

        Returns:
            None  # 启动失败，清理任务正常结束且没有实例锁遗留
        """
        application = App(configuration)
        startup_task = asyncio.create_task(application.start())
        try:
            # 驱动加载期间取消启动，并释放工作线程让其报错。
            assert await asyncio.to_thread(loading_started.wait, 2)
            startup_task.cancel()
            await asyncio.sleep(0)
            release_loading.set()
            with pytest.raises(OSError) as captured_failure:
                await asyncio.wait_for(startup_task, 2)
            assert captured_failure.value is loading_failure
            assert application.failure is loading_failure
            assert application.shutdown_task.done()
            assert not application.shutdown_task.cancelled()
            assert not application.database.lock_acquired
        finally:
            release_loading.set()
            await asyncio.gather(startup_task, return_exceptions=True)
            await application.stop()

    monkeypatch.setattr("app.load_mvs_sdk", fail_driver_loading)
    asyncio.run(cancel_startup())
    failure_logs = [record for record in caplog.records if record.exc_info]
    assert len(failure_logs) == 1
    assert failure_logs[0].exc_info[1] is loading_failure


@pytest.mark.parametrize("worker_kind", ["frequency", "storage", "return", "cancel"])
def test_background_failure_stops_all_devices(device_environment, caplog, worker_kind):
    """验证后台异常、意外返回和取消均停止全部设备且不重启。

    Args:
        device_environment: 测试配置与假驱动。
        caplog: 日志捕获器。
        worker_kind: 需要验证的后台故障类型。

    Returns:
        None  # 故障已传播、记录日志并完成全局清理
    """
    configuration, sdk = device_environment

    async def run_failure():
        """注入后台故障并等待应用自动清理。

        Args:
            无外部参数。

        Returns:
            None  # 首次故障已抛出且资源已关闭
        """
        # 替换目标后台任务，准备启动异常。
        application = App(configuration)
        if worker_kind == "frequency":
            operation = AsyncMock(side_effect=OSError("频率连接断开"))
            application.machine_managers["M1"].frequency_adapter.listen_measurements = operation
        else:
            operation = AsyncMock(side_effect=OSError("存储任务失败"))
            if worker_kind == "return":
                operation = AsyncMock()
            if worker_kind != "cancel":
                application.database.run = operation

        # 等待故障通知，退出不依赖再次调用业务入口。
        await application.start()
        if worker_kind == "cancel":
            await asyncio.sleep(0)
            application.worker_tasks[-1].cancel()
        with pytest.raises((OSError, RuntimeError)):
            await asyncio.wait_for(application.wait_for_failure(), 2)
        await asyncio.wait_for(application.shutdown_task, 2)

        # 检查全局停止、无任务重启和实例锁释放。
        assert sdk.closed
        assert not application.worker_tasks
        assert not application.accepting_signals
        assert not application.database.lock_acquired
        if worker_kind != "cancel":
            operation.assert_awaited_once()

    asyncio.run(run_failure())
    assert "后台任务" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("operation", ["start", "read", "encode", "stop"])
def test_camera_fault_interrupts_main_workflow(device_environment, monkeypatch, caplog, operation):
    """验证相机各阶段故障中断主流程等待并释放全部设备。

    Args:
        device_environment: 测试配置与假驱动。
        monkeypatch: 测试依赖替换工具。
        caplog: 日志捕获器。
        operation: 故障发生的相机操作。

    Returns:
        None  # 主流程抛出相机异常，全部设备已关闭
    """
    # 打开相机时替换指定设备操作，停止故障在短采集窗口触发。
    configuration, sdk = device_environment
    if operation in {"stop", "encode"}:
        configuration = replace(configuration, capture_window_ms=50)
    original_open = sdk.open_camera
    method_names = {
        "start": "start_grabbing",
        "read": "read_frame",
        "encode": "encode_image",
        "stop": "stop_grabbing",
    }

    def open_faulty_camera(serial, **parameters):
        """打开设备并为第一台相机注入一次故障。

        Args:
            serial: 相机序列号。
            parameters: 相机配置参数。

        Returns:
            camera  # 已打开并设置测试故障的相机
        """
        camera = original_open(serial, **parameters)
        if serial == "SERIAL1":
            method_name = method_names[operation]
            original_method = getattr(camera, method_name)
            failure = Mock(side_effect=OSError("相机连接断开"))
            if operation == "stop":
                failure = Mock(wraps=original_method, side_effect=[OSError("相机连接断开"), DEFAULT])
            monkeypatch.setattr(camera, method_name, failure)
        return camera

    # 主流程的长时间等待必须被故障立即打断。
    monkeypatch.setattr(sdk, "open_camera", open_faulty_camera)
    monkeypatch.setattr(main, "load_configuration", lambda path: configuration)
    with pytest.raises(OSError, match="相机连接断开"):
        asyncio.run(asyncio.wait_for(main.run_measurement_demo(Path("unused")), 3))
    assert sdk.closed
    assert all(camera.closed for camera in sdk.cameras.values())
    assert ("camera_id=CAM1" if operation == "encode" else "serial=SERIAL1") in caplog.text
    # 同一设备故障只在发生位置记录一次异常堆栈。
    failure_logs = [
        record for record in caplog.records
        if record.exc_info and str(record.exc_info[1]) == "相机连接断开"
    ]
    assert len(failure_logs) == 1


def test_no_data_and_normal_shutdown_are_not_faults(device_environment):
    """验证正常取帧超时、无频率读数和退出取消不会触发程序故障。

    Args:
        device_environment: 测试配置与假驱动。

    Returns:
        None  # 应用正常运行并退出，无故障记录
    """
    configuration, sdk = device_environment

    async def run_without_data():
        """执行无读数测量并正常退出。

        Args:
            无外部参数。

        Returns:
            None  # 正常等待未触发故障
        """
        application = App(configuration)
        await application.start()
        application.machine_managers["M1"].camera.device.read_frame = Mock(return_value=None)
        await application.handle_start("M1")
        await asyncio.sleep(0.05)
        assert application.failure is None
        await application.stop()
        assert application.failure is None
        assert sdk.closed

    asyncio.run(run_without_data())


def test_command_line_failure_returns_nonzero(monkeypatch, caplog):
    """验证主流程故障记录日志并返回非零退出码。

    Args:
        monkeypatch: 测试依赖替换工具。
        caplog: 日志捕获器。

    Returns:
        None  # 命令行以退出码 1 结束
    """
    # 替换主流程为设备异常并清空测试命令行参数。
    monkeypatch.setattr(main, "load_configuration", Mock(side_effect=OSError("配置读取失败")))
    monkeypatch.setattr("sys.argv", ["main.py"])

    # 验证日志和失败退出码。
    with pytest.raises(SystemExit) as failure:
        main.main()
    assert failure.value.code == 1
    assert "测量配置初始化失败" in caplog.text


def test_failure_releases_publishers_waiting_for_queue(device_environment):
    """验证故障退出会释放已阻塞入队的事件交付任务。

    Args:
        device_environment: 测试配置与假驱动。

    Returns:
        None  # 阻塞交付已结束，队列已排空
    """
    configuration, sdk = device_environment

    async def run_blocked_publishers():
        """填满事件队列并触发故障退出。

        Args:
            无外部参数。

        Returns:
            None  # 所有交付任务已退出
        """
        # 不启动消费者，创建多个等待容量的事件交付任务。
        application = App(replace(configuration, event_queue_capacity=1))
        manager = application.machine_managers["M1"]
        event = MeasurementEvent(EventType.CAPACITY_CHANGED, "M1", payload=True)
        await manager.queue.put(event)
        publishers = [asyncio.create_task(application.publish_event(event)) for _ in range(3)]
        await asyncio.sleep(0)
        assert all(not task.done() for task in publishers)

        # 退出释放队列容量，所有旧交付均丢弃而不再阻塞。
        application.report_failure(OSError("设备故障"))
        await asyncio.wait_for(application.stop(), 2)
        await asyncio.wait_for(asyncio.gather(*publishers), 2)
        await asyncio.wait_for(manager.queue.join(), 2)
        assert manager.queue.empty()

    asyncio.run(run_blocked_publishers())
