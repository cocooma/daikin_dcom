"""Data update coordinator for the DCOM-LT/MB."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CONTROL_MODE,
    CONF_SCAN_INTERVAL,
    DEFAULT_CONTROL_MODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    READ_BLOCKS,
    SENTINELS,
)
from .modbus_client import DcomClient, DcomError, DcomModbusError

_LOGGER = logging.getLogger(__name__)


class DcomCoordinator(DataUpdateCoordinator[dict[str, dict[int, int]]]):
    """Polls all documented registers and handles writes."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: DcomClient
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.entry = entry
        self.client = client
        self.control_mode: str = entry.options.get(
            CONF_CONTROL_MODE, entry.data.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE)
        )
        self.smart = None  # SmartHeatingController, set in __init__.py
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Daikin Altherma 3",
            manufacturer="Daikin",
            model="Altherma 3 (DCOM-LT/MB)",
        )

    async def _async_update_data(self) -> dict[str, dict[int, int]]:
        data: dict[str, dict[int, int]] = {"h": {}, "i": {}}
        failed = 0
        for kind, start, count in READ_BLOCKS:
            try:
                regs = await self.client.read(kind, start, count)
            except DcomModbusError as err:
                failed += 1
                _LOGGER.debug("Block %s%d+%d not readable: %s", kind, start, count, err)
                continue
            except DcomError as err:
                raise UpdateFailed(str(err)) from err
            for offset, value in enumerate(regs):
                data[kind][start + offset] = value
        if failed == len(READ_BLOCKS):
            raise UpdateFailed("Device rejected all register reads")
        return data

    # --- helpers used by entities ------------------------------------------
    def value(self, kind: str, addr: int) -> int | None:
        """Signed int16 value, or None if missing / special return value."""
        raw = (self.data or {}).get(kind, {}).get(addr)
        if raw is None:
            return None
        if raw >= 0x8000:
            raw -= 0x10000
        if raw in SENTINELS:
            return None
        return raw

    def temp(self, kind: str, addr: int) -> float | None:
        """temp16 value in degC."""
        val = self.value(kind, addr)
        return None if val is None else val / 100

    def text(self, kind: str, addr: int) -> str | None:
        """text16 value (two ASCII characters)."""
        raw = (self.data or {}).get(kind, {}).get(addr)
        if raw is None or raw in SENTINELS:
            return None
        return (chr(raw >> 8) + chr(raw & 0xFF)).strip()

    async def async_write(self, addr: int, value: int) -> None:
        """Write a holding register and update local state optimistically."""
        try:
            await self.client.write(addr, value & 0xFFFF)
        except DcomError as err:
            raise UpdateFailed(str(err)) from err
        if self.data is not None:
            self.data["h"][addr] = value & 0xFFFF
            self.async_set_updated_data(self.data)
