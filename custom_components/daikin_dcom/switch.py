"""Switches."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, H_QUIET_MODE
from .coordinator import DcomCoordinator
from .entity import DcomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DcomQuietModeSwitch(coordinator)])


class DcomQuietModeSwitch(DcomEntity, SwitchEntity):
    _attr_name = "Quiet mode"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "quiet_mode")

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.value("h", H_QUIET_MODE)
        return None if val is None else val == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(H_QUIET_MODE, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(H_QUIET_MODE, 0)
