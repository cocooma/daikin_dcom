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
    async_add_entities(DcomSensor(coordinator, d) for d in SENSORS)


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
