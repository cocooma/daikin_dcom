"""Thin async Modbus client for the DCOM-LT/MB (TCP or RTU-over-TCP)."""
from __future__ import annotations

import asyncio
import inspect
import logging

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import FRAMING_RTU

_LOGGER = logging.getLogger(__name__)


class DcomError(Exception):
    """Communication problem (connection, timeout)."""


class DcomModbusError(DcomError):
    """The device answered with a Modbus exception."""


def _rtu_framer():
    try:  # pymodbus >= 3.8
        from pymodbus.framer import FramerType

        return FramerType.RTU
    except ImportError:  # pymodbus 3.6 / 3.7
        from pymodbus.framer import ModbusRtuFramer

        return ModbusRtuFramer


class DcomClient:
    """Serialises access to one Modbus connection."""

    def __init__(
        self, host: str, port: int, slave: int, framing: str, timeout: float = 5
    ) -> None:
        self._host = host
        self._port = port
        self._slave = slave
        self._framing = framing
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._client: AsyncModbusTcpClient | None = None
        self._slave_kw = "slave"

    def _build(self) -> AsyncModbusTcpClient:
        kwargs = {"port": self._port, "timeout": self._timeout}
        if self._framing == FRAMING_RTU:
            kwargs["framer"] = _rtu_framer()
        client = AsyncModbusTcpClient(self._host, **kwargs)
        params = inspect.signature(client.read_holding_registers).parameters
        self._slave_kw = "device_id" if "device_id" in params else "slave"
        return client

    async def _ensure(self) -> AsyncModbusTcpClient:
        if self._client is None:
            self._client = self._build()
        if not self._client.connected:
            await self._client.connect()
        if not self._client.connected:
            raise DcomError(f"Cannot connect to {self._host}:{self._port}")
        return self._client

    async def read(self, kind: str, address: int, count: int) -> list[int]:
        """Read `count` registers; kind is 'h' (holding) or 'i' (input)."""
        async with self._lock:
            client = await self._ensure()
            func = (
                client.read_holding_registers
                if kind == "h"
                else client.read_input_registers
            )
            try:
                rr = await func(address, count=count, **{self._slave_kw: self._slave})
            except ModbusException as err:
                raise DcomError(f"Modbus error: {err}") from err
            except (OSError, asyncio.TimeoutError) as err:
                raise DcomError(f"Communication error: {err}") from err
            if rr.isError():
                if hasattr(rr, "exception_code"):
                    raise DcomModbusError(
                        f"Exception {rr.exception_code} reading {kind}{address}"
                    )
                raise DcomError(f"Error reading {kind}{address}: {rr}")
            return list(rr.registers)

    async def write(self, address: int, value: int) -> None:
        """Write a single holding register."""
        async with self._lock:
            client = await self._ensure()
            try:
                rr = await client.write_register(
                    address, value, **{self._slave_kw: self._slave}
                )
            except ModbusException as err:
                raise DcomError(f"Modbus error: {err}") from err
            except (OSError, asyncio.TimeoutError) as err:
                raise DcomError(f"Communication error: {err}") from err
            if rr.isError():
                raise DcomError(f"Error writing holding register {address}: {rr}")

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
