"""验证 Modbus 串口连接失败、程序异常和读取重连。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymodbus.exceptions import ModbusException

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_error", "communication_failure"),
    [
        (TypeError("timeout 参数错误"), False),
        (RuntimeError("not connected 程序错误"), False),
        (OSError("串口连接失效"), True),
        (ModbusException("协议异常"), True),
    ],
)
async def test_read_uses_exception_type_instead_of_message(
    read_error: Exception,
    communication_failure: bool,
) -> None:
    """确认读取异常按类型处理，不按异常文本猜测。

    Args:
        read_error: 本次读取时抛出的异常。
        communication_failure: 异常是否按本次通信失败处理。

    Returns:
        返回示例：
            None  # 通信异常已返回失败，程序异常已原样抛出
    """
    # 建立已连接的客户端并设置读取异常。
    modbus_client = ModbusClient("COM1")
    modbus_client._client = SimpleNamespace(
        connected=True,
        read_discrete_inputs=AsyncMock(side_effect=read_error),
    )
    modbus_client._connected = True

    # 按异常类型核对读取结果或原始异常。
    if communication_failure:
        assert await modbus_client.read_discrete_inputs(0) is None
    else:
        with pytest.raises(type(read_error)) as raised_error:
            await modbus_client.read_discrete_inputs(0)
        assert raised_error.value is read_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection_error",
    [
        OSError("串口连接失效"),
        ModbusException("协议异常"),
    ],
)
async def test_read_returns_none_when_reconnect_fails_with_communication_error(
    connection_error: Exception,
) -> None:
    """确认重连阶段的通信异常按本次通信失败返回。

    Args:
        connection_error: 连接阶段抛出的通信异常。

    Returns:
        返回示例：
            None  # 通信异常未向上传播，读取返回失败
    """
    # 建立无有效连接的客户端，并让重连抛出通信异常。
    modbus_client = ModbusClient("COM1")
    serial_client = SimpleNamespace(connect=AsyncMock(side_effect=connection_error))
    modbus_client._create_client = Mock(return_value=serial_client)

    # 核对通信异常被转换成一次读取失败。
    assert await modbus_client.read_discrete_inputs(0) is None
    assert modbus_client._connected is False
    serial_client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_propagates_program_error_from_reconnect() -> None:
    """确认重连阶段的程序异常原样抛出。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 程序异常未被伪装成通信失败
    """
    # 建立无有效连接的客户端，并让重连抛出程序异常。
    modbus_client = ModbusClient("COM1")
    program_error = TypeError("连接参数错误")
    serial_client = SimpleNamespace(connect=AsyncMock(side_effect=program_error))
    modbus_client._create_client = Mock(return_value=serial_client)

    # 核对程序异常仍向上传播给调用方。
    with pytest.raises(TypeError) as raised_error:
        await modbus_client.read_discrete_inputs(0)
    assert raised_error.value is program_error
