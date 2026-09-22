"""Modbus RTU 客户端。"""

from __future__ import annotations

import contextlib
import logging

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException


logger = logging.getLogger(__name__)


class ModbusClient:
    """提供 Modbus RTU 连接和离散输入读取功能。"""

    def __init__(
        self,
        serial_port: str,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
        unit_id: int = 1,
        timeout: float = 3.0,
    ) -> None:
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
            返回示例：
                None  # 串口参数与设备地址已登记，尚未创建客户端和连接
        """
        # 登记串口通信参数。
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize

        # 登记 Modbus 设备地址与请求超时时间。
        self.unit_id = unit_id
        self.timeout = timeout

        # 初始化串口客户端对象与连接状态空位。
        self._client: AsyncModbusSerialClient | None = None
        self._connected = False

    def _create_client(self) -> AsyncModbusSerialClient:
        """创建 Modbus RTU 串口客户端。

        Args:
            无外部参数。

        Returns:
            返回示例：
                AsyncModbusSerialClient  # 使用当前串口配置创建、尚未连接的客户端
        """
        return AsyncModbusSerialClient(
            port=self.serial_port,
            baudrate=self.baudrate,
            parity=self.parity,
            stopbits=self.stopbits,
            bytesize=self.bytesize,
            timeout=self.timeout,
        )

    async def connect(self) -> bool:
        """创建客户端并连接 Modbus 设备。

        Args:
            无外部参数。

        Returns:
            返回示例：
                True  # 串口客户端已建立连接
                False  # 创建或连接过程异常，或设备未报告已连接
        """
        try:
            # 按当前串口配置创建客户端并发起连接。
            self._client = self._create_client()
            await self._client.connect()

            # 以客户端上报的状态作为本机连接结果。
            self._connected = bool(self._client.connected)
        except Exception as error:
            # 连接过程异常时复位状态并记录失败原因。
            self._connected = False
            logger.error("Modbus RTU 连接失败: %s", error)
            return False

        # 客户端未报告已连接时视为连接失败。
        if not self._connected:
            logger.error("Modbus RTU 连接失败，设备未报告已连接")
            return False

        # 记录连接成功的串口名称。
        logger.info("Modbus RTU 已连接: %s", self.serial_port)
        return True

    async def disconnect(self) -> None:
        """关闭 Modbus RTU 连接。

        Args:
            无外部参数。

        Returns:
            返回示例：
                None  # 串口客户端已关闭并释放，本机连接状态已复位
        """
        # 关闭已创建的串口客户端。
        if self._client is not None:
            self._client.close()

        # 释放客户端对象并复位连接状态。
        self._client = None
        self._connected = False

    async def read_discrete_inputs(self, address: int, count: int = 1) -> list[bool] | None:
        """读取 Modbus FC02 离散输入。

        Args:
            address: 离散输入起始地址。
            count: 要读取的输入数量。

        Returns:
            返回示例：
                None  # 尚未连接、响应为异常帧或发生 Modbus 异常
                [True, False]  # 自起始地址开始的各离散输入状态
        """
        # 未建立连接时不发起读取。
        if self._client is None or not self._connected:
            return None

        try:
            # 按设备地址读取离散输入。
            result = await self._client.read_discrete_inputs(address=address, count=count, device_id=self.unit_id)

            # 响应为异常帧时放弃本次结果。
            if result.isError():
                logger.warning("读取离散输入失败，地址: %s", address)
                return None

            # 按请求数量返回输入状态列表。
            return list(result.bits[:count])
        except ModbusException as error:
            # Modbus 通信异常时记录原因并返回空。
            logger.error("读取离散输入时发生 Modbus 异常: %s", error)
            return None

    async def _ensure_connected(self) -> bool:
        """确保当前存在有效的 Modbus RTU 连接。

        Args:
            无外部参数。

        Returns:
            返回示例：
                True  # 现有连接仍有效，或重新连接成功
                False  # 重新连接失败
        """
        # 现有客户端仍上报已连接时直接复用。
        if self._client is not None and self._connected and self._client.connected:
            return True

        # 复位状态并关闭失效客户端。
        self._connected = False
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()

        # 重新建立连接。
        return await self.connect()

    def _is_connection_error(self, error: Exception) -> bool:
        """判断异常是否属于连接异常。

        Args:
            error: 待判断的异常对象。

        Returns:
            返回示例：
                True  # 异常类型为连接、超时或操作系统错误，或异常文本含连接类关键字
                False  # 异常类型和异常文本都不属于连接异常
        """
        # 按异常类型直接判定连接、超时和操作系统错误。
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return True

        # 取小写异常文本供关键字匹配。
        error_text = str(error).lower()

        # 登记常见的连接类异常文本关键字。
        connection_indicators = [
            "connection",
            "timeout",
            "timed out",
            "refused",
            "reset",
            "broken pipe",
            "no response",
            "disconnected",
            "not connected",
        ]

        # 异常文本命中任一关键字即视为连接异常。
        return any(indicator in error_text for indicator in connection_indicators)
