"""验证 Modbus DI 状态到现有机器事件入口的转换。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from enums import MachineState
from local_test_support import build_config, create_machine_database
from modbus_client import ModbusClient
from system_runtime import SystemRuntime


def build_io_runtime(tmp_path: Path, machine_channels: dict[str, int]) -> SystemRuntime:
    """创建已登记机器编号的 IO 测试运行时。

    Args:
        tmp_path: 测试临时目录。
        machine_channels: 机器编号到 DI 索引的映射。

    Returns:
        返回示例：
            SystemRuntime  # 已登记指定机器编号，尚未启动硬件资源
    """
    config = build_config(tmp_path, io_machine_channels=machine_channels)
    system_runtime = SystemRuntime(config)
    system_runtime.machines = {machine_id: SimpleNamespace() for machine_id in machine_channels}
    system_runtime.synchronize_machine = AsyncMock()
    system_runtime.handle_start = AsyncMock()
    system_runtime.handle_close = AsyncMock()
    return system_runtime


def test_first_io_states_only_synchronize_machines(tmp_path: Path) -> None:
    """验证首次 DI 读取只同步每台机器的现场状态。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 三台机器分别同步对应通道状态，没有产生启停事件
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0, "2": 1, "3": 2})

    asyncio.run(system_runtime.handle_io_states([False, True, False]))

    assert system_runtime.synchronize_machine.await_args_list == [
        (("1", MachineState.CLOSED),),
        (("2", MachineState.OPEN),),
        (("3", MachineState.CLOSED),),
    ]
    system_runtime.handle_start.assert_not_awaited()
    system_runtime.handle_close.assert_not_awaited()
    assert system_runtime.io_previous_states == {0: False, 1: True, 2: False}


def test_unchanged_io_states_do_not_create_events(tmp_path: Path) -> None:
    """验证连续相同 DI 状态不会重复产生机器事件。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 同步后重复状态没有产生启动或关闭事件
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0, "2": 1})
    asyncio.run(system_runtime.handle_io_states([False, True]))
    system_runtime.synchronize_machine.reset_mock()

    asyncio.run(system_runtime.handle_io_states([False, True]))

    system_runtime.synchronize_machine.assert_not_awaited()
    system_runtime.handle_start.assert_not_awaited()
    system_runtime.handle_close.assert_not_awaited()


def test_io_state_edges_use_existing_machine_entries(tmp_path: Path) -> None:
    """验证 DI 上升沿和下降沿进入对应机器的现有处理方法。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # DI0 只启动机器 1，DI1 只关闭机器 2
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0, "2": 1, "3": 2})
    system_runtime.io_previous_states = {0: False, 1: True, 2: False}

    asyncio.run(system_runtime.handle_io_states([True, False, False]))

    system_runtime.handle_start.assert_awaited_once_with("1")
    system_runtime.handle_close.assert_awaited_once_with("2")
    assert system_runtime.io_previous_states == {0: True, 1: False, 2: False}


def test_io_state_updates_after_machine_entry_succeeds(tmp_path: Path) -> None:
    """验证机器入口失败时不提前登记新的 DI 状态。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 启动入口异常后上次状态仍为 False
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0})
    system_runtime.io_previous_states = {0: False}
    system_runtime.handle_start = AsyncMock(side_effect=RuntimeError("start failed"))

    with pytest.raises(RuntimeError, match="start failed"):
        asyncio.run(system_runtime.handle_io_states([True]))

    assert system_runtime.io_previous_states == {0: False}


def test_short_io_states_do_not_change_previous_states(tmp_path: Path) -> None:
    """验证残缺 DI 结果不会产生事件或修改上次状态。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 上次状态保持不变且没有机器事件
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0, "2": 2})
    system_runtime.io_previous_states = {0: True, 2: True}

    asyncio.run(system_runtime.handle_io_states([True, False]))

    assert system_runtime.io_previous_states == {0: True, 2: True}
    system_runtime.synchronize_machine.assert_not_awaited()
    system_runtime.handle_start.assert_not_awaited()
    system_runtime.handle_close.assert_not_awaited()


def test_validate_io_configuration_rejects_duplicate_channels(tmp_path: Path) -> None:
    """验证启用机器不能绑定相同 DI 通道。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 重复通道配置抛出 ValueError
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0, "2": 0})

    with pytest.raises(ValueError, match="不能绑定相同"):
        system_runtime.validate_io_configuration()


def test_validate_io_configuration_rejects_missing_serial_port(tmp_path: Path) -> None:
    """验证启用机器存在时必须配置 Modbus RTU 串口。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # 串口缺失时配置校验抛出 ValueError
    """
    config = build_config(tmp_path, modbus_serial_port=None, io_machine_channels={"1": 0})
    system_runtime = SystemRuntime(config)
    system_runtime.machines = {"1": SimpleNamespace()}

    with pytest.raises(ValueError, match="未配置 Modbus RTU 串口"):
        system_runtime.validate_io_configuration()


def test_start_rejects_enabled_machine_without_channel(tmp_path: Path, monkeypatch) -> None:
    """验证启动拒绝未绑定 DI 通道的启用机器。

    Args:
        tmp_path: 测试临时目录。
        monkeypatch: 测试依赖替换工具。

    Returns:
        返回示例：
            None  # 缺少通道时启动失败且未加载相机 SDK
    """
    config = build_config(tmp_path, io_machine_channels={"1": 0})
    create_machine_database(config.database_path, [
        {"machine_name": "一号皮带机", "camera_serial": "CAM-A", "frequency_meter_serial": "FREQ-A"},
        {"machine_name": "二号皮带机", "camera_serial": "CAM-B", "frequency_meter_serial": "FREQ-B"},
    ])
    load_mvs_sdk = AsyncMock()
    monkeypatch.setattr("system_runtime.load_mvs_sdk", load_mvs_sdk)
    system_runtime = SystemRuntime(config)

    with pytest.raises(ValueError, match="启用机器未配置 DI 通道：2"):
        asyncio.run(system_runtime.start())
    load_mvs_sdk.assert_not_called()


def test_listen_io_ignores_failed_read_and_disconnects(tmp_path: Path) -> None:
    """验证通信失败不改变状态，停止后释放 Modbus 客户端。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        返回示例：
            None  # True、失败、True 序列没有产生启停事件，客户端已断开
    """
    system_runtime = build_io_runtime(tmp_path, {"1": 0})
    system_runtime.accepting_signals = True
    system_runtime.config = build_config(
        tmp_path,
        io_machine_channels={"1": 0},
        modbus_poll_interval_ms=1,
        modbus_reconnect_interval_ms=1,
    )

    # Modbus 客户端已改由启动流程创建，此处按串口配置直接建立客户端。
    system_runtime.modbus_client = ModbusClient(serial_port="COM-TEST")
    system_runtime.modbus_client.read_discrete_inputs = AsyncMock(side_effect=[[True], None, [True]])
    system_runtime.modbus_client.disconnect = AsyncMock()

    async def stop_after_third_state(states: list[bool]) -> None:
        """处理状态并在第三次有效读取后进入停止流程。

        Args:
            states: 本次有效 DI 状态。

        Returns:
            返回示例：
                None  # 状态已处理，第三次读取时已设置停止标志
        """
        await SystemRuntime.handle_io_states(system_runtime, states)
        if system_runtime.modbus_client.read_discrete_inputs.await_count == 3:
            system_runtime.stopping = True

    system_runtime.handle_io_states = stop_after_third_state
    asyncio.run(system_runtime.listen_io())

    system_runtime.handle_start.assert_not_awaited()
    system_runtime.handle_close.assert_not_awaited()
    assert system_runtime.io_previous_states == {0: True}
    system_runtime.modbus_client.disconnect.assert_awaited_once_with()
