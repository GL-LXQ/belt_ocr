"""Modbus RTU 客户端。"""

from __future__ import annotations

import contextlib
import logging

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException


logger = logging.getLogger(__name__)


class ModbusClient:
    """提供 Modbus RTU 连接和离散输入读取功能。"""

    def __init__(self, serial_port: str, baudrate: int = 9600, parity: str = "N", stopbits: int = 1,
                 bytesize: int = 8, unit_id: int = 1, timeout: float = 3.0) -> None:
        """初始化 Modbus RTU 客户端。

        Args:
            serial_port: RTU 串口名称。
            baudrate: 串口波特率。
            parity: 串口校验方式。
            stopbits: 停止位数量。
            bytesize: 数据位数量。
            unit_id: Modbus 设备地址。
            timeout: 请求超时时间，单位为秒。

        Returns:
            None。
        """
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.unit_id = unit_id
        self.timeout = timeout
        self._client: AsyncModbusSerialClient | None = None
        self._connected = False

    def _create_client(self) -> AsyncModbusSerialClient:
        """创建 Modbus RTU 串口客户端。

        Args:
            无外部参数。

        Returns:
            AsyncModbusSerialClient: 使用当前串口配置创建的客户端。
        """
        return AsyncModbusSerialClient(port=self.serial_port, baudrate=self.baudrate, parity=self.parity,
                                       stopbits=self.stopbits, bytesize=self.bytesize, timeout=self.timeout)

    async def connect(self) -> bool:
        """创建客户端并连接 Modbus 设备。

        Args:
            无外部参数。

        Returns:
            bool: 连接成功返回 True，连接失败返回 False。
        """
        try:
            self._client = self._create_client()
            await self._client.connect()
            self._connected = bool(self._client.connected)
        except Exception as error:
            self._connected = False
            logger.error("Modbus RTU 连接失败: %s", error)
            return False

        if not self._connected:
            logger.error("Modbus RTU 连接失败，设备未报告已连接")
            return False

        logger.info("Modbus RTU 已连接: %s", self.serial_port)
        return True

    async def disconnect(self) -> None:
        """关闭 Modbus RTU 连接。

        Args:
            无外部参数。

        Returns:
            None。
        """
        if self._client is not None:
            self._client.close()
        self._connected = False

    async def read_discrete_inputs(self, address: int, count: int = 1) -> list[bool] | None:
        """读取 Modbus FC02 离散输入。

        Args:
            address: 离散输入起始地址。
            count: 要读取的输入数量。

        Returns:
            list[bool] | None: 成功返回输入状态列表，读取失败返回 None。
        """
        if self._client is None or not self._connected:
            return None

        try:
            result = await self._client.read_discrete_inputs(address=address, count=count, device_id=self.unit_id)
            if result.isError():
                logger.warning("读取离散输入失败，地址: %s", address)
                return None
            return list(result.bits[:count])
        except ModbusException as error:
            logger.error("读取离散输入时发生 Modbus 异常: %s", error)
            return None

    async def _ensure_connected(self) -> bool:
        """确保当前存在有效的 Modbus RTU 连接。

        Args:
            无外部参数。

        Returns:
            bool: 当前或重新建立连接成功返回 True，否则返回 False。
        """
        if self._client is not None and self._connected and self._client.connected:
            return True

        self._connected = False
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        return await self.connect()

    def _is_connection_error(self, error: Exception) -> bool:
        """判断异常是否属于连接异常。

        Args:
            error: 待判断的异常对象。

        Returns:
            bool: 异常属于连接、超时或操作系统错误时返回 True，否则返回 False。
        """
        return isinstance(error, (ConnectionError, TimeoutError, OSError))
