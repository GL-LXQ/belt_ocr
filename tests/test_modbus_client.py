"""验证 Modbus 串口连接失败、程序异常和读取重连。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modbus_client import ModbusClient


@pytest.mark.asyncio
async def test_connect_returns_false_when_client_is_disconnected() -> None:
    """确认客户端未连接时返回失败。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 未连接状态和返回值已核对
    """
    # 建立未连接的串口客户端并执行连接。
    modbus_client = ModbusClient("COM1")
    serial_client = SimpleNamespace(
        connect=AsyncMock(return_value=False),
        connected=False,
    )
    modbus_client._create_client = Mock(return_value=serial_client)
    modbus_client._connected = True
    connection_result = await modbus_client.connect()

    # 核对本次连接结果和状态。
    assert connection_result is False
    assert modbus_client._connected is False
    serial_client.connect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["create", "connect"])
async def test_connect_propagates_program_error(failure_stage: str) -> None:
    """确认创建或连接时的程序异常原样抛出。

    Args:
        failure_stage: 抛出异常的连接步骤。

    Returns:
        返回示例：
            None  # 程序异常原样抛出且连接状态已复位
    """
    # 按异常步骤设置串口客户端和原始异常。
    modbus_client = ModbusClient("COM1")
    program_error = TypeError("连接参数错误")
    modbus_client._connected = True
    if failure_stage == "create":
        modbus_client._create_client = Mock(side_effect=program_error)
    else:
        serial_client = SimpleNamespace(connect=AsyncMock(side_effect=program_error))
        modbus_client._create_client = Mock(return_value=serial_client)

    # 核对程序异常未被转换成连接失败。
    with pytest.raises(TypeError) as raised_error:
        await modbus_client.connect()
    assert raised_error.value is program_error
    assert modbus_client._connected is False


@pytest.mark.asyncio
async def test_read_reconnects_after_failed_connection() -> None:
    """确认首次连接失败后下一次读取会重新连接。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 首次读取失败，重连后返回输入状态
    """
    # 准备首次失败和随后成功的串口客户端。
    failed_client = SimpleNamespace(
        connect=AsyncMock(return_value=False),
        connected=False,
        close=Mock(),
    )
    response = SimpleNamespace(isError=Mock(return_value=False), bits=[True])
    connected_client = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        connected=True,
        read_discrete_inputs=AsyncMock(return_value=response),
    )
    modbus_client = ModbusClient("COM1")
    modbus_client._create_client = Mock(side_effect=[failed_client, connected_client])

    # 连续读取并核对失败后的重连结果。
    assert await modbus_client.read_discrete_inputs(0) is None
    assert await modbus_client.read_discrete_inputs(0) == [True]
    assert modbus_client._create_client.call_count == 2
    failed_client.close.assert_called_once()
