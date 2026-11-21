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
from homeassistant.const import UnitOfTemperature, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    STATUS_COOLING,
    STATUS_DEFROST,
    STATUS_DISABLED,
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
    """What the smart heating controller is doing."""

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
        STATUS_PAUSED_WARM,
        STATUS_PAUSED_MILD,
        STATUS_PAUSED_ROOMS_OFF,
    ]

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_status")

    @property
    def native_value(self):
        return self.coordinator.smart.status

