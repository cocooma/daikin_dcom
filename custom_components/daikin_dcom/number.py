"""Numbers: weather dependent LWT setpoint offsets."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, H_WEATHER_COOL_OFFSET, H_WEATHER_HEAT_OFFSET
from .coordinator import DcomCoordinator
from .entity import DcomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DcomOffsetNumber(
                coordinator, "weather_heat_offset",
                "Weather dependent heating LWT offset", H_WEATHER_HEAT_OFFSET,
            ),
            DcomOffsetNumber(
                coordinator, "weather_cool_offset",
                "Weather dependent cooling LWT offset", H_WEATHER_COOL_OFFSET,
            ),
        ]
    )


class DcomOffsetNumber(DcomEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = -10
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: DcomCoordinator, key: str, name: str, register: int
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._register = register

    @property
    def native_value(self) -> float | None:
        return self.coordinator.value("h", self._register)

    async def async_set_native_value(self, value: float) -> None:
        # negative values are written as two's complement (handled by coordinator)
        await self.coordinator.async_write(self._register, int(round(value)))
