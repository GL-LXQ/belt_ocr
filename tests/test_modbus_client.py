import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymodbus.exceptions import ModbusException

from modbus_client import ModbusClient


def test_create_client_uses_rtu_configuration(monkeypatch):
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
    mock_client = Mock(connected=True)
    mock_client.connect = AsyncMock()
    modbus_client = ModbusClient("COM3")
    modbus_client._create_client = Mock(return_value=mock_client)

    assert asyncio.run(modbus_client.connect()) is True
    assert modbus_client._connected is True


def test_connect_failure_returns_false():
    mock_client = Mock(connected=False)
    mock_client.connect = AsyncMock(side_effect=OSError("port unavailable"))
    modbus_client = ModbusClient("COM3")
    modbus_client._create_client = Mock(return_value=mock_client)

    assert asyncio.run(modbus_client.connect()) is False
    assert modbus_client._connected is False


def test_read_discrete_inputs_returns_bits():
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
    modbus_client = ModbusClient("COM3")
    modbus_client.connect = AsyncMock(return_value=False)

    assert asyncio.run(modbus_client.read_discrete_inputs(0)) is None


def test_ensure_connected_reconnects_after_disconnect():
    old_client = Mock(connected=False)
    modbus_client = ModbusClient("COM3")
    modbus_client._client = old_client
    modbus_client._connected = True
    modbus_client.connect = AsyncMock(return_value=True)

    assert asyncio.run(modbus_client._ensure_connected()) is True
    old_client.close.assert_called_once_with()
    modbus_client.connect.assert_awaited_once_with()


def test_disconnect_closes_client_and_clears_state():
    mock_client = Mock()
    modbus_client = ModbusClient("COM3")
    modbus_client._client = mock_client
    modbus_client._connected = True

    asyncio.run(modbus_client.disconnect())

    mock_client.close.assert_called_once_with()
    assert modbus_client._connected is False
