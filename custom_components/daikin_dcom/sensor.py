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
from homeassistant.const import UnitOfPower, UnitOfTemperature, UnitOfVolumeFlowRate
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
    STATUS_DEFROST,
    STATUS_DISABLED,
    STATUS_EARLY_START,
    STATUS_PAUSED_MILD,
    STATUS_PAUSED_ROOMS_OFF,
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


class DcomSmartStatusSensor(_SmartSensorBase):
    """What the smart heating controller is doing.

    Logbook entries (compressor rest start / end) are attached to this entity,
    so they show in its Activity.
    """

    async def async_added_to_hass(self) -> None:
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
        STATUS_PAUSED_WARM,
        STATUS_PAUSED_MILD,
        STATUS_PAUSED_ROOMS_OFF,
        STATUS_COMPRESSOR_REST,
    ]

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_status")

    @property
    def native_value(self):
        return self.coordinator.smart.status



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
