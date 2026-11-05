"""Select: weather dependent mode."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, H_WEATHER_MODE, WEATHER_MODES
from .coordinator import DcomCoordinator
from .entity import DcomEntity

_LABELS = {
    "fixed": "Fixed",
    "weather_dependent": "Weather dependent",
    "fixed_scheduled": "Fixed + scheduled",
    "weather_dependent_scheduled": "Weather dependent + scheduled",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DcomWeatherModeSelect(coordinator)])


class DcomWeatherModeSelect(DcomEntity, SelectEntity):
    _attr_name = "Weather dependent mode"
    _attr_options = list(_LABELS.values())

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "weather_mode")

    @property
    def current_option(self) -> str | None:
        val = self.coordinator.value("h", H_WEATHER_MODE)
        key = WEATHER_MODES.get(val)
        return _LABELS.get(key) if key else None

    async def async_select_option(self, option: str) -> None:
        for num, key in WEATHER_MODES.items():
            if _LABELS[key] == option:
                await self.coordinator.async_write(H_WEATHER_MODE, num)
                return
