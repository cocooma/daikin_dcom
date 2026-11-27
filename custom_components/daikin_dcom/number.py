"""Numbers: weather dependent LWT offsets and smart heating curve points."""
from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
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
            *(DcomCurveNumber(coordinator, *spec) for spec in CURVE_NUMBERS),
            *(DcomRestNumber(coordinator, *spec) for spec in REST_NUMBERS),
            DcomSoftToleranceNumber(coordinator),
        ]
    )


# field, name, min, max, icon
CURVE_NUMBERS = (
    ("cold_out", "Heating curve cold point outdoor", -25, 10, "mdi:snowflake-thermometer"),
    ("cold_lwt", "Heating curve cold point LWT", 25, 55, "mdi:water-thermometer"),
    ("warm_out", "Heating curve warm point outdoor", 0, 25, "mdi:sun-thermometer"),
    ("warm_lwt", "Heating curve warm point LWT", 25, 55, "mdi:water-thermometer-outline"),
)

# field, name, min, max, step, unit
REST_NUMBERS = (
    ("rest_cold_out", "Compressor rest cold point outdoor", -20, 15, 0.5, "°C"),
    ("rest_cold_min", "Compressor rest cold point time", 5, 60, 1, "min"),
    ("rest_mild_out", "Compressor rest mild point outdoor", 0, 25, 0.5, "°C"),
    ("rest_mild_min", "Compressor rest mild point time", 5, 60, 1, "min"),
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


class DcomCurveNumber(DcomEntity, RestoreNumber):
    """One point of the smart heating curve, adjustable from the dashboard.

    Changes apply immediately (no reload) and are restored after a restart.
    The first value comes from the integration options / defaults.
    """

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: DcomCoordinator,
        field: str,
        name: str,
        lo: float,
        hi: float,
        icon: str,
    ) -> None:
        super().__init__(coordinator, f"smart_curve_{field}")
        self._smart = coordinator.smart
        self._field = field
        self._attr_name = name
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            value = float(last.native_value)
            if self._smart.curve_valid(self._field, value):
                self._smart.set_curve(self._field, value, evaluate=False)

    @property
    def native_value(self) -> float:
        return getattr(self._smart.settings, self._field)

    async def async_set_native_value(self, value: float) -> None:
        if not self._smart.curve_valid(self._field, value):
            raise ServiceValidationError(
                "The warm point outdoor temperature must be higher than the cold point's."
            )
        self._smart.set_curve(self._field, value)
        self.async_write_ha_state()


class DcomRestNumber(DcomEntity, RestoreNumber):
    """One point of the compressor rest curve (outdoor °C → rest minutes).

    Applies to the next rest, no reload; restored after a restart. Both rest
    times start from the old "Compressor rest time" option (default 20 min).
    """

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-sand"

    def __init__(
        self,
        coordinator: DcomCoordinator,
        field: str,
        name: str,
        lo: float,
        hi: float,
        step: float,
        unit: str,
    ) -> None:
        super().__init__(coordinator, f"smart_{field}")
        self._smart = coordinator.smart
        self._field = field
        self._attr_name = name
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        if unit == "°C":
            self._attr_device_class = NumberDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            value = float(last.native_value)
            if self._smart.rest_valid(self._field, value):
                self._smart.set_rest(self._field, value)

    @property
    def native_value(self) -> float:
        return getattr(self._smart.settings, self._field)

    async def async_set_native_value(self, value: float) -> None:
        if not self._smart.rest_valid(self._field, value):
            raise ServiceValidationError(
                "The mild point outdoor temperature must be higher than the cold point's."
            )
        self._smart.set_rest(self._field, value)
        self.async_write_ha_state()


class DcomSoftToleranceNumber(DcomEntity, RestoreNumber):
    """Soft landing tolerance: how far the rooms may head above / fall below target.

    Smaller = closer to target but more compressor starts."""

    _attr_name = "Smart heating soft landing tolerance"
    _attr_icon = "mdi:arrow-collapse-vertical"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 0.2
    _attr_native_max_value = 1.5
    _attr_native_step = 0.1

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_soft_tolerance")
        self._smart = coordinator.smart

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._smart.set_soft_tolerance(min(1.5, max(0.2, float(last.native_value))))

    @property
    def native_value(self) -> float:
        return self._smart.settings.soft_tolerance

    async def async_set_native_value(self, value: float) -> None:
        self._smart.set_soft_tolerance(value)
        self.async_write_ha_state()
