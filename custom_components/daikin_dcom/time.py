"""Time entities: smart heating mild-day window start / end."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import DcomCoordinator
from .entity import DcomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DcomSmartWindowTime(coordinator, "start"),
            DcomSmartWindowTime(coordinator, "end"),
        ]
    )


class DcomSmartWindowTime(DcomEntity, TimeEntity, RestoreEntity):
    """Start or end of the window in which heating runs on mild days."""

    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: DcomCoordinator, which: str) -> None:
        super().__init__(coordinator, f"smart_window_{which}")
        self._which = which
        self._smart = coordinator.smart
        self._attr_name = f"Smart heating window {which}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            value = time.fromisoformat(last.state)
        except ValueError:
            return  # unknown / unavailable: keep the default
        self._apply(value)

    def _apply(self, value: time) -> None:
        if self._which == "start":
            self._smart.set_window(start=value)
        else:
            self._smart.set_window(end=value)

    @property
    def native_value(self) -> time:
        s = self._smart.settings
        return s.window_start if self._which == "start" else s.window_end

    async def async_set_value(self, value: time) -> None:
        self._apply(value.replace(second=0, microsecond=0))
        self.async_write_ha_state()
