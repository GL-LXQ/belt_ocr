"""验证单台相机连接失败时其他机器继续初始化。"""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from camera.hikrobot_sdk import MvsError
from config_util import AppConfig, MachineConfig
from runtime.system_runtime import SystemRuntime


@pytest.mark.asyncio
async def test_camera_connection_failure_preserves_other_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认一台相机连接失败后另一台相机仍可完成启动。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 故障机器不可用，正常机器已连接且运行时继续接收信号
    """
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        modbus_serial_port="COM1",
        io_machine_channels={"1": 0, "2": 1},
    )
    runtime = SystemRuntime(config)

    async def wait_for_worker() -> None:
        """让测试后台任务保持运行直至取消。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 任务收到取消后结束
        """
        await asyncio.Event().wait()

    async def wait_for_input(address: int, count: int) -> list[bool]:
        """让测试 IO 读取保持等待直至取消。

        Args:
            address: DI 起始地址。
            count: 本次读取的 DI 数量。

        Returns:
            返回示例：
                [False, False]  # 测试任务被取消前不会返回
        """
        await asyncio.Event().wait()

    # 建立两台机器和相机连接结果。
    runtime.machines = {
        machine_id: SimpleNamespace(
            machine_config=MachineConfig(
                machine_id=machine_id,
                machine_name=f"{machine_id}号皮带机",
                camera_serial=f"camera-{machine_id}",
                frequency_meter_serial=f"meter-{machine_id}",
                camera_pixel_format="Mono8",
                camera_exposure_time_us=80.0,
                camera_gain=0.0,
                camera_line_selector="Line2",
                camera_line_mode="Strobe",
                camera_line_source="ExposureStartActive",
                camera_strobe_enabled=True,
            ),
            camera=SimpleNamespace(sdk_camera=None),
            frequency_adapter=SimpleNamespace(listen_measurements=wait_for_worker),
            listen_events=wait_for_worker,
            waiting_cycle_reset=False,
            initialized=False,
        )
        for machine_id in ("1", "2")
    }
    runtime.initialize_machines = Mock()
    healthy_camera = SimpleNamespace(closed=False, faulted=False)
    camera_sdk = SimpleNamespace(open_camera=Mock(
        side_effect=[MvsError("相机未连接"), healthy_camera]
    ))
    modbus_client = SimpleNamespace(
        read_discrete_inputs=wait_for_input,
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr("runtime.system_runtime.load_mvs_sdk", Mock(return_value=camera_sdk))
    monkeypatch.setattr("runtime.system_runtime.ModbusClient", Mock(return_value=modbus_client))
    camera_state_notification = Mock()
    initialization_started = threading.Event()
    initialization_finished = threading.Event()
    initialization_thread_ids: list[int] = []

    def initialize_ocr() -> None:
        """等待测试放行并记录 OCR 初始化时的启动状态。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 相机连接和信号入口状态已核对
        """
        # 核对相机连接结果及尚未开放的信号入口。
        assert camera_sdk.open_camera.call_count == 2
        assert runtime.machines["2"].camera.sdk_camera is healthy_camera
        assert not runtime.accepting_signals
        initialization_thread_ids.append(threading.get_ident())

        # 通知测试初始化已经开始并等待放行。
        initialization_started.set()
        if not initialization_finished.wait(timeout=5):
            raise TimeoutError("测试未放行 OCR 初始化")

    runtime.text_recognizer.initialize = Mock(side_effect=initialize_ocr)

    # 启动运行时并核对故障机器与正常机器的连接状态。
    startup_task = asyncio.create_task(runtime.start(camera_state_notification))
    try:
        # OCR 初始化未完成时，信号入口和机器任务保持关闭。
        assert await asyncio.to_thread(initialization_started.wait, 5)
        assert not runtime.accepting_signals
        assert not runtime.worker_tasks

        # 放行 OCR 初始化并核对后续启动结果。
        initialization_finished.set()
        await startup_task
        assert runtime.machines["1"].camera.sdk_camera is None
        assert runtime.machines["2"].camera.sdk_camera is healthy_camera
        camera_sdk.open_camera.assert_any_call(
            "camera-2",
            pixel_format="Mono8",
            exposure_time_us=80.0,
            gain=0.0,
            line_selector="Line2",
            line_mode="Strobe",
            line_source="ExposureStartActive",
            strobe_enabled=True,
        )
        camera_state_notification.assert_any_call("1", "连接失败", "相机未连接")
        runtime.initialize_machines.assert_called_once_with(
            None, camera_state_notification, None, None
        )
        assert runtime.accepting_signals
        assert runtime.failure is None
        runtime.text_recognizer.initialize.assert_called_once_with()
        assert len(initialization_thread_ids) == 1
        assert initialization_thread_ids[0] != threading.get_ident()
    finally:
        # 停止测试后台任务并关闭本地运行库。
        initialization_finished.set()
        await asyncio.gather(startup_task, return_exceptions=True)
        runtime.stopping = True
        for worker_task in runtime.worker_tasks:
            worker_task.cancel()
        await asyncio.gather(*runtime.worker_tasks, return_exceptions=True)
        runtime.database.close()


def test_initialize_machines_passes_camera_state_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认运行时把现有相机状态回调保存到每台机器。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 已建立的机器保存相同的状态通知回调
    """
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
    )
    runtime = SystemRuntime(config)
    enabled_machine = {
        "id": 1,
        "machine_name": "1号皮带机",
        "camera_serial": "camera-1",
        "frequency_meter_serial": "meter-1",
    }
    monkeypatch.setattr("runtime.system_runtime.MachineRepo.list_enabled", Mock(return_value=[enabled_machine]))
    camera_state_notification = Mock()

    # 建立机器并核对状态通知回调。
    ocr_notification = Mock()
    cycle_closed_notification = Mock()
    runtime.initialize_machines(
        notify_camera_state=camera_state_notification,
        notify_ocr_result=ocr_notification,
        notify_cycle_closed=cycle_closed_notification,
    )
    assert runtime.machines["1"].notify_ocr_result is ocr_notification
    assert runtime.machines["1"].notify_camera_state is camera_state_notification
    assert runtime.machines["1"].notify_cycle_closed is cycle_closed_notification


def test_initialize_machines_copies_common_camera_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认公共相机参数写入每台机器的配置。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 机器配置包含全部公共相机参数
    """
    # 建立带有完整相机参数的运行配置。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        camera_pixel_format="Mono8",
        camera_exposure_time_us=80.0,
        camera_gain=0.0,
        camera_line_selector="Line2",
        camera_line_mode="Strobe",
        camera_line_source="ExposureStartActive",
        camera_strobe_enabled=True,
    )
    runtime = SystemRuntime(config)
    machine_rows = [
        {
            "id": 1,
            "machine_name": "1号皮带机",
            "camera_serial": "camera-1",
            "frequency_meter_serial": "meter-1",
        },
        {
            "id": 2,
            "machine_name": "2号皮带机",
            "camera_serial": "camera-2",
            "frequency_meter_serial": "meter-2",
        },
    ]
    monkeypatch.setattr(
        "runtime.system_runtime.MachineRepo.list_enabled", Mock(return_value=machine_rows)
    )

    # 初始化机器并核对每台机器保存的参数。
    runtime.initialize_machines()
    for machine in runtime.machines.values():
        machine_config = machine.machine_config
        assert machine_config.machine_name == f"{machine_config.machine_id}号皮带机"
        assert machine.camera.machine_name == machine_config.machine_name
        assert machine_config.camera_pixel_format == "Mono8"
        assert machine_config.camera_exposure_time_us == 80.0
        assert machine_config.camera_gain == 0.0
        assert machine_config.camera_line_selector == "Line2"
        assert machine_config.camera_line_mode == "Strobe"
        assert machine_config.camera_line_source == "ExposureStartActive"
        assert machine_config.camera_strobe_enabled is True

    # 确认两台机器复用 Runtime 持有的同一个 OCR 处理器。
    assert runtime.machines["1"].text_recognizer is runtime.text_recognizer
    assert runtime.machines["2"].text_recognizer is runtime.text_recognizer
    assert (
        runtime.machines["1"].text_recognizer
        is runtime.machines["2"].text_recognizer
    )


@pytest.mark.asyncio
async def test_all_camera_connections_failed_stops_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认所有相机连接失败时结束启动并释放已初始化资源。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 启动失败，运行时没有开放信号入口
    """
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        modbus_serial_port="COM1",
        io_machine_channels={"1": 0},
    )
    runtime = SystemRuntime(config)
    machine = SimpleNamespace(
        machine_config=MachineConfig("1", "1号皮带机", "camera-1", "meter-1"),
        camera=SimpleNamespace(sdk_camera=None),
        discard_pending_events=Mock(),
        release_resources=AsyncMock(),
    )
    runtime.machines = {"1": machine}
    runtime.initialize_machines = Mock()
    camera_sdk = SimpleNamespace(
        open_camera=Mock(side_effect=MvsError("相机未连接")),
        close=Mock(),
    )
    modbus_client = SimpleNamespace(disconnect=AsyncMock())
    monkeypatch.setattr("runtime.system_runtime.load_mvs_sdk", Mock(return_value=camera_sdk))
    monkeypatch.setattr("runtime.system_runtime.ModbusClient", Mock(return_value=modbus_client))
    runtime.text_recognizer.initialize = Mock()

    # 启动全部相机不可用的运行时并核对失败收尾。
    with pytest.raises(MvsError, match="所有启用机器的相机均连接失败"):
        await runtime.start()
    assert not runtime.accepting_signals
    assert not runtime.worker_tasks
    assert machine.camera.sdk_camera is None
    runtime.text_recognizer.initialize.assert_not_called()
    camera_sdk.close.assert_called_once()
    modbus_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_ocr_initialization_failure_stops_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认 OCR 初始化失败沿用启动失败清理流程。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的属性替换工具。

    Returns:
        返回示例：
            None  # 信号入口未开放，相机和数据库资源已释放
    """
    # 建立单台机器与连接成功的相机。
    config = AppConfig(
        database_path=tmp_path / "measurements.sqlite3",
        evidence_directory=tmp_path / "evidence",
        mvs_development_directory=tmp_path,
        modbus_serial_port="COM1",
        io_machine_channels={"1": 0},
    )
    runtime = SystemRuntime(config)
    machine = SimpleNamespace(
        machine_config=MachineConfig("1", "1号皮带机", "camera-1", "meter-1"),
        camera=SimpleNamespace(sdk_camera=None),
        discard_pending_events=Mock(),
        release_resources=AsyncMock(),
    )
    runtime.machines = {"1": machine}
    runtime.initialize_machines = Mock()
    camera_sdk = SimpleNamespace(open_camera=Mock(return_value=Mock()), close=Mock())
    modbus_client = SimpleNamespace(disconnect=AsyncMock())
    monkeypatch.setattr("runtime.system_runtime.load_mvs_sdk", Mock(return_value=camera_sdk))
    monkeypatch.setattr("runtime.system_runtime.ModbusClient", Mock(return_value=modbus_client))

    # 在相机连接后令 OCR 初始化抛错。
    initialization_error = RuntimeError("OCR 模型加载失败")
    runtime.text_recognizer.initialize = Mock(side_effect=initialization_error)
    with pytest.raises(RuntimeError, match="OCR 模型加载失败"):
        await runtime.start()

    # 核对初始化顺序和启动失败清理结果。
    runtime.text_recognizer.initialize.assert_called_once_with()
    camera_sdk.open_camera.assert_called_once()
    assert not runtime.accepting_signals
    assert not runtime.worker_tasks
    assert runtime.failure is initialization_error
    machine.release_resources.assert_awaited_once()
    camera_sdk.close.assert_called_once()
    modbus_client.disconnect.assert_awaited_once()
    assert runtime.database.anchor_connection is None
