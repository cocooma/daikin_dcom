"""Sensors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import (
    DOMAIN,
    I_DHW_TEMP,
    I_ERROR_CODE,
    I_ERROR_SUBCODE,
    I_FLOW_RATE,
    I_LWT_PRE_BUH,
    I_LWT_PRE_PHE,
    I_OUTSIDE_TEMP,
    I_REFRIGERANT_TEMP,
    I_RETURN_WATER,
    I_ROOM_TEMP,
)
from .coordinator import DcomCoordinator
from .entity import DcomEntity
from .smart import (
    STATUS_ACTIVE,
    STATUS_COMPRESSOR_REST,
    STATUS_COOLING,
    STATUS_CLOUDY,
    STATUS_DEFROST,
    STATUS_DISABLED,
    STATUS_EARLY_START,
    STATUS_PAUSED_MILD,
    STATUS_PAUSED_COAST,
    STATUS_PAUSED_NO_DEMAND,
    STATUS_PAUSED_ROOMS_OFF,
    STATUS_SLAB_CHARGE,
    STATUS_PAUSED_WARM,
    STATUS_USER_OFF,
    STATUS_WAITING,
)


@dataclass(frozen=True, kw_only=True)
class DcomSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DcomCoordinator], float | int | str | None]


def _temp(addr: int) -> Callable[[DcomCoordinator], float | None]:
    return lambda c: c.temp("i", addr)


def _temp_desc(key: str, name: str, addr: int) -> DcomSensorDescription:
    return DcomSensorDescription(
        key=key,
        name=name,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_temp(addr),
    )


def _flow(c: DcomCoordinator) -> float | None:
    val = c.value("i", I_FLOW_RATE)
    return None if val is None else val / 100


SENSORS: tuple[DcomSensorDescription, ...] = (
    _temp_desc("lwt_pre_phe", "Leaving water temperature (pre PHE)", I_LWT_PRE_PHE),
    _temp_desc("lwt_pre_buh", "Leaving water temperature (pre BUH)", I_LWT_PRE_BUH),
    _temp_desc("return_water", "Return water temperature", I_RETURN_WATER),
    _temp_desc("dhw_temp", "Domestic hot water temperature", I_DHW_TEMP),
    _temp_desc("outside_temp", "Outside air temperature", I_OUTSIDE_TEMP),
    _temp_desc("refrigerant_temp", "Liquid refrigerant temperature", I_REFRIGERANT_TEMP),
    _temp_desc("room_temp", "Remocon room temperature", I_ROOM_TEMP),
    DcomSensorDescription(
        key="flow_rate",
        name="Flow rate",
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_flow,
    ),
    DcomSensorDescription(
        key="error_code",
        name="Unit error code",
        value_fn=lambda c: c.text("i", I_ERROR_CODE),
    ),
    DcomSensorDescription(
        key="error_subcode",
        name="Unit error sub code",
        value_fn=lambda c: c.value("i", I_ERROR_SUBCODE),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            *(DcomSensor(coordinator, d) for d in SENSORS),
            DcomSmartTargetSensor(coordinator),
            DcomSmartStatusSensor(coordinator),
            DcomCompressorStartsSensor(coordinator),
            DcomThermalPowerSensor(coordinator),
            DcomSlabTemperatureSensor(coordinator),
            DcomHeatForecastSensor(coordinator, "today"),
            DcomHeatGraphSensor(coordinator, "delivered"),
            DcomHeatGraphSensor(coordinator, "predicted"),
            DcomHeatForecastErrorSensor(coordinator),
            DcomHeatForecastSensor(coordinator, "tomorrow"),
            *(
                [DcomElectricalPowerSensor(coordinator), DcomCopSensor(coordinator)]
                if coordinator.cop.has_meter
                else []
            ),
        ]
    )


class DcomSensor(DcomEntity, SensorEntity):
    entity_description: DcomSensorDescription

    def __init__(
        self, coordinator: DcomCoordinator, description: DcomSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator)


class _SmartSensorBase(DcomEntity, SensorEntity):
    """Updated by the smart heating controller as well as by polls."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.smart.add_listener(self.async_write_ha_state)
        )

    @property
    def extra_state_attributes(self):
        return self.coordinator.smart.attributes


class DcomSmartTargetSensor(_SmartSensorBase):
    """LWT setpoint calculated by the smart heating controller."""

    _attr_name = "Smart heating target LWT"
    _unrecorded_attributes = frozenset({"curve_points"})
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_target_lwt")

    @property
    def native_value(self):
        return self.coordinator.smart.target_lwt

    @property
    def extra_state_attributes(self):
        smart = self.coordinator.smart
        return {**smart.attributes, "curve_points": smart.curve_points}


# icon of the smart heating status sensor, per status
STATUS_ICONS = {
    STATUS_DISABLED: "mdi:power-off",
    STATUS_WAITING: "mdi:timer-sand",
    STATUS_COOLING: "mdi:snowflake",
    STATUS_USER_OFF: "mdi:radiator-off",
    STATUS_DEFROST: "mdi:snowflake-melt",
    STATUS_ACTIVE: "mdi:radiator",
    STATUS_EARLY_START: "mdi:clock-fast",
    STATUS_CLOUDY: "mdi:weather-cloudy-clock",
    STATUS_PAUSED_WARM: "mdi:weather-sunny",
    STATUS_PAUSED_MILD: "mdi:sun-clock",
    STATUS_PAUSED_ROOMS_OFF: "mdi:thermometer-off",
    STATUS_PAUSED_NO_DEMAND: "mdi:pause-circle-outline",
    STATUS_PAUSED_COAST: "mdi:airplane-landing",
    STATUS_SLAB_CHARGE: "mdi:battery-charging-high",
    STATUS_COMPRESSOR_REST: "mdi:sleep",
}


class DcomSmartStatusSensor(_SmartSensorBase, RestoreEntity):
    """What the smart heating controller is doing.

    Logbook entries (compressor rest start / end) are attached to this entity,
    so they show in its Activity.
    """

    async def async_added_to_hass(self) -> None:
        # show the last status through the restart instead of a brief "disabled"
        if (last := await self.async_get_last_state()) is not None:
            self.coordinator.smart.hold_status(last.state)
        await super().async_added_to_hass()
        self.coordinator.smart.status_entity_id = self.entity_id

    _attr_name = "Smart heating status"
    _attr_translation_key = "smart_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        STATUS_DISABLED,
        STATUS_WAITING,
        STATUS_COOLING,
        STATUS_USER_OFF,
        STATUS_DEFROST,
        STATUS_ACTIVE,
        STATUS_EARLY_START,
        STATUS_CLOUDY,
        STATUS_PAUSED_WARM,
        STATUS_PAUSED_MILD,
        STATUS_PAUSED_ROOMS_OFF,
        STATUS_PAUSED_NO_DEMAND,
        STATUS_PAUSED_COAST,
        STATUS_SLAB_CHARGE,
        STATUS_COMPRESSOR_REST,
    ]

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_status")

    @property
    def native_value(self):
        return self.coordinator.smart.status

    @property
    def icon(self) -> str:
        return STATUS_ICONS.get(self.coordinator.smart.status, "mdi:heat-pump-outline")



class DcomCompressorStartsSensor(_SmartSensorBase):
    """Compressor starts since midnight (counted whether or not smart heating is on)."""

    _attr_name = "Compressor starts today"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "compressor_starts_today")

    @property
    def native_value(self):
        return self.coordinator.smart.starts_today

    @property
    def extra_state_attributes(self):
        smart = self.coordinator.smart
        return {
            "space_heating_starts": smart.heating_starts_today,
            "hot_water_starts": smart.starts_today - smart.heating_starts_today,
            "compressor_rest_until": smart.attributes.get("compressor_rest_until"),
        }


# --- COP -------------------------------------------------------------------------
class _CopSensorBase(DcomEntity, SensorEntity):
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.cop.add_listener(self.async_write_ha_state))


class _CopData(ExtraStoredData):
    def __init__(self, data: dict) -> None:
        self.data = data

    def as_dict(self) -> dict:
        return self.data


class DcomThermalPowerSensor(_CopSensorBase, RestoreEntity):
    """Heat delivered (heating / DHW) or extracted (cooling) from flow × ΔT.

    Works without a meter. Also persists all COP counters across restarts.
    """

    _attr_name = "Thermal power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "thermal_power")

    async def async_added_to_hass(self) -> None:
        if (extra := await self.async_get_last_extra_data()) is not None:
            self.coordinator.cop.restore(extra.as_dict())
        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> _CopData:
        return _CopData(self.coordinator.cop.persist())

    @property
    def native_value(self):
        return self.coordinator.cop.thermal_kw

    @property
    def extra_state_attributes(self):
        return {"mode": self.coordinator.cop.mode}


class DcomElectricalPowerSensor(_CopSensorBase):
    """Heat pump electrical power as used for the COP (from the configured sensor)."""

    _attr_name = "Heat pump electrical power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "electrical_power")

    @property
    def native_value(self):
        return self.coordinator.cop.electrical_kw


class DcomCopSensor(_CopSensorBase):
    """COP: 15 min rolling value for the current mode; per-mode figures as attributes."""

    _attr_name = "COP"
    _attr_icon = "mdi:heat-pump-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "cop")

    @property
    def native_value(self):
        return self.coordinator.cop.cop_now

    @property
    def extra_state_attributes(self):
        return self.coordinator.cop.attributes


# --- heat demand forecast ----------------------------------------------------------
class DcomHeatForecastSensor(DcomEntity, SensorEntity):
    """Predicted space heating need (heat delivered, kWh) for today or tomorrow."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:home-thermometer-outline"

    def __init__(self, coordinator: DcomCoordinator, day: str) -> None:
        super().__init__(coordinator, f"heat_forecast_{day}")
        self._day = day
        self._attr_name = f"Heat demand forecast {day}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.forecast.add_listener(self.async_write_ha_state))

    @property
    def native_value(self):
        f = self.coordinator.forecast
        return f.today_kwh if self._day == "today" else f.tomorrow_kwh

    @property
    def extra_state_attributes(self):
        f = self.coordinator.forecast
        attrs = {"model": f.status}
        if self._day == "today":
            attrs["delivered_so_far_kwh"] = round(f.day["heat"], 1)
            if f.today_kwh is not None:
                attrs["remaining_kwh"] = round(max(0.0, f.today_kwh - f.day["heat"]), 1)
            attrs.update(f.today_inputs)
        else:
            attrs.update(f.tomorrow_inputs)
            attrs.update(f.model_summary)
            attrs.update(f.accuracy)
            attrs["last_days"] = f.history
        return attrs


class DcomSlabTemperatureSensor(_SmartSensorBase):
    """Floor slab temperature, estimated from the return water while it circulates
    with the compressor off (needs the pump running continuously, [F-0D] = 0)."""

    _attr_name = "Floor slab temperature (estimated)"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:heating-coil"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "slab_temperature")

    @property
    def native_value(self):
        return self.coordinator.smart.slab_temp

    @property
    def extra_state_attributes(self):
        smart = self.coordinator.smart
        if smart.slab_measured is None:
            return {"measured": None}
        from homeassistant.util import dt as dt_util
        age = (dt_util.utcnow() - smart.slab_measured).total_seconds() / 60
        return {"measured": dt_util.as_local(smart.slab_measured).strftime("%H:%M"),
                "age_minutes": round(age)}


class DcomHeatGraphSensor(DcomEntity, SensorEntity):
    """For graphs: heat delivered today, and the forecast for today made yesterday.

    Both are plain measurements (no energy device class) so a Statistics graph
    card with stat type "max" and period "day" shows one bar per day for each.
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: DcomCoordinator, which: str) -> None:
        super().__init__(coordinator, f"heat_{which}_today")
        self._which = which
        if which == "delivered":
            self._attr_name = "Heat delivered today"
            self._attr_icon = "mdi:radiator"
        else:
            self._attr_name = "Heat predicted for today"
            self._attr_icon = "mdi:crystal-ball"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.forecast.add_listener(self.async_write_ha_state))

    @property
    def native_value(self):
        f = self.coordinator.forecast
        if self._which == "delivered":
            return round(f.day["heat"], 2)
        return f.predicted_today


class DcomHeatForecastErrorSensor(DcomEntity, SensorEntity):
    """Yesterday's forecast error: (actual - predicted) / predicted, in %.

    Positive = the house needed more heat than forecast."""

    _attr_name = "Heat demand forecast error"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:target-variant"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "heat_forecast_error")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.forecast.add_listener(self.async_write_ha_state))

    @property
    def native_value(self):
        last = self.coordinator.forecast.last_error
        return None if last is None else last["error_pct"]

    @property
    def extra_state_attributes(self):
        f = self.coordinator.forecast
        attrs = dict(f.last_error or {})
        attrs.update(f.accuracy)
        return attrs
