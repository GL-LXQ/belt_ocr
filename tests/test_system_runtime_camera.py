"""验证单台相机连接失败时其他机器继续初始化。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from camera.hikrobot_sdk import MvsError
from config_util import AppConfig, MachineConfig
from system_runtime import SystemRuntime


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
                camera_serial=f"camera-{machine_id}",
                frequency_meter_serial=f"meter-{machine_id}",
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
    monkeypatch.setattr("system_runtime.load_mvs_sdk", Mock(return_value=camera_sdk))
    monkeypatch.setattr("system_runtime.ModbusClient", Mock(return_value=modbus_client))
    camera_state_notification = Mock()

    # 启动运行时并核对故障机器与正常机器的连接状态。
    try:
        await runtime.start(camera_state_notification)
        assert runtime.machines["1"].camera.sdk_camera is None
        assert runtime.machines["2"].camera.sdk_camera is healthy_camera
        camera_state_notification.assert_any_call("1", "连接失败", "相机未连接")
        runtime.initialize_machines.assert_called_once_with(None, camera_state_notification, None)
        assert runtime.accepting_signals
        assert runtime.failure is None
    finally:
        # 停止测试后台任务并关闭本地运行库。
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
        "camera_serial": "camera-1",
        "frequency_meter_serial": "meter-1",
    }
    monkeypatch.setattr("system_runtime.MachineRepo.list_enabled", Mock(return_value=[enabled_machine]))
    camera_state_notification = Mock()

    # 建立机器并核对状态通知回调。
    ocr_notification = Mock()
    runtime.initialize_machines(
        notify_camera_state=camera_state_notification,
        notify_ocr_result=ocr_notification,
    )
    assert runtime.machines["1"].notify_ocr_result is ocr_notification
    assert runtime.machines["1"].notify_camera_state is camera_state_notification


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
        machine_config=MachineConfig("1", "camera-1", "meter-1"),
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
    monkeypatch.setattr("system_runtime.load_mvs_sdk", Mock(return_value=camera_sdk))
    monkeypatch.setattr("system_runtime.ModbusClient", Mock(return_value=modbus_client))

    # 启动全部相机不可用的运行时并核对失败收尾。
    with pytest.raises(MvsError, match="所有启用机器的相机均连接失败"):
        await runtime.start()
    assert not runtime.accepting_signals
    assert not runtime.worker_tasks
    assert machine.camera.sdk_camera is None
    camera_sdk.close.assert_called_once()
    modbus_client.disconnect.assert_awaited_once()
