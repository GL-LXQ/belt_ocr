import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymodbus.exceptions import ModbusException

from modbus_client import ModbusClient


def test_create_client_uses_rtu_configuration(monkeypatch):
    """验证串口配置完整传入 RTU 客户端。

    Args:
        monkeypatch: 测试依赖替换工具。

    Returns:
        返回示例：
            None  # RTU 客户端工厂收到全部串口参数
    """
    client_factory = Mock(return_value=Mock())
    monkeypatch.setattr("modbus_client.AsyncModbusSerialClient", client_factory)
    modbus_client = ModbusClient("COM3", 19200, "E", 2, 7, 4, 1.5)

    modbus_client._create_client()

    client_factory.assert_called_once_with(
        port="COM3",
        baudrate=19200,
        parity="E",
        stopbits=2,
        bytesize=7,
        timeout=1.5,
    )


def test_connect_success_updates_connection_state():
    """验证连接成功时返回成功并更新连接状态。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 连接结果和内部状态均为 True
    """
    mock_client = Mock(connected=True)
    mock_client.connect = AsyncMock()
    modbus_client = ModbusClient("COM3")
    modbus_client._create_client = Mock(return_value=mock_client)

    assert asyncio.run(modbus_client.connect()) is True
    assert modbus_client._connected is True


def test_connect_failure_returns_false():
    """验证连接异常时返回失败并复位连接状态。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 连接结果和内部状态均为 False
    """
    mock_client = Mock(connected=False)
    mock_client.connect = AsyncMock(side_effect=OSError("port unavailable"))
    modbus_client = ModbusClient("COM3")
    modbus_client._create_client = Mock(return_value=mock_client)

    assert asyncio.run(modbus_client.connect()) is False
    assert modbus_client._connected is False


def test_read_discrete_inputs_returns_bits():
    """验证 FC02 成功响应按请求数量返回状态。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 返回三个 DI 状态并传入设备地址
    """
    result = SimpleNamespace(isError=lambda: False, bits=[True, False, True])
    mock_client = Mock(connected=True)
    mock_client.read_discrete_inputs = AsyncMock(return_value=result)
    modbus_client = ModbusClient("COM3", unit_id=4)
    modbus_client._client = mock_client
    modbus_client._connected = True

    assert asyncio.run(modbus_client.read_discrete_inputs(0, 3)) == [True, False, True]
    mock_client.read_discrete_inputs.assert_awaited_once_with(address=0, count=3, device_id=4)


@pytest.mark.parametrize(
    "result",
    [SimpleNamespace(isError=lambda: True, bits=[]), ModbusException("read failed")],
)
def test_read_discrete_inputs_failure_returns_none(result):
    """验证异常响应和 Modbus 异常均返回 None。

    Args:
        result: 异常响应对象或 Modbus 异常。

    Returns:
        返回示例：
            None  # 本次读取没有向业务层返回状态
    """
    mock_client = Mock(connected=True)
    if isinstance(result, Exception):
        mock_client.read_discrete_inputs = AsyncMock(side_effect=result)
    else:
        mock_client.read_discrete_inputs = AsyncMock(return_value=result)
    modbus_client = ModbusClient("COM3")
    modbus_client._client = mock_client
    modbus_client._connected = True

    assert asyncio.run(modbus_client.read_discrete_inputs(0)) is None


def test_read_discrete_inputs_without_connection_returns_none():
    """验证未连接读取会尝试连接且连接失败时返回 None。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 已调用连接且未发起 DI 读取
    """
    modbus_client = ModbusClient("COM3")
    modbus_client.connect = AsyncMock(return_value=False)

    assert asyncio.run(modbus_client.read_discrete_inputs(0)) is None
    modbus_client.connect.assert_awaited_once_with()


def test_read_discrete_inputs_connection_error_clears_connection_state():
    """验证读取连接异常会复位连接状态。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 读取返回 None 且连接状态为 False
    """
    mock_client = Mock(connected=True)
    mock_client.read_discrete_inputs = AsyncMock(side_effect=ConnectionError("disconnected"))
    modbus_client = ModbusClient("COM3")
    modbus_client._client = mock_client
    modbus_client._connected = True

    assert asyncio.run(modbus_client.read_discrete_inputs(0)) is None
    assert modbus_client._connected is False


def test_read_discrete_inputs_rejects_short_response():
    """验证状态数量不足时拒绝残缺响应。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 请求三个状态但收到两个时返回 None
    """
    result = SimpleNamespace(isError=lambda: False, bits=[True, False])
    mock_client = Mock(connected=True)
    mock_client.read_discrete_inputs = AsyncMock(return_value=result)
    modbus_client = ModbusClient("COM3")
    modbus_client._client = mock_client
    modbus_client._connected = True

    assert asyncio.run(modbus_client.read_discrete_inputs(0, 3)) is None


def test_read_discrete_inputs_propagates_programming_error():
    """验证非通信异常继续抛给后台任务处理。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # RuntimeError 原样抛出
    """
    mock_client = Mock(connected=True)
    mock_client.read_discrete_inputs = AsyncMock(side_effect=RuntimeError("invalid response handler"))
    modbus_client = ModbusClient("COM3")
    modbus_client._client = mock_client
    modbus_client._connected = True

    with pytest.raises(RuntimeError, match="invalid response handler"):
        asyncio.run(modbus_client.read_discrete_inputs(0))


def test_ensure_connected_reconnects_after_disconnect():
    """验证失效连接关闭后重新建立连接。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 旧客户端已关闭并调用一次连接入口
    """
    old_client = Mock(connected=False)
    modbus_client = ModbusClient("COM3")
    modbus_client._client = old_client
    modbus_client._connected = True
    modbus_client.connect = AsyncMock(return_value=True)

    assert asyncio.run(modbus_client._ensure_connected()) is True
    old_client.close.assert_called_once_with()
    modbus_client.connect.assert_awaited_once_with()


def test_disconnect_closes_client_and_clears_state():
    """验证断开连接会关闭客户端并清除状态。

    Args:
        无外部参数。

    Returns:
        返回示例：
            None  # 客户端已关闭且连接状态为 False
    """
    mock_client = Mock()
    modbus_client = ModbusClient("COM3")
    modbus_client._client = mock_client
    modbus_client._connected = True

    asyncio.run(modbus_client.disconnect())

    mock_client.close.assert_called_once_with()
    assert modbus_client._connected is False
