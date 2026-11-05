"""Binary sensors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    I_BOOSTER_HEATER,
    I_COMPRESSOR,
    I_DEFROST,
    I_DISINFECTION,
    I_HOT_START,
    I_PUMP,
    I_THREE_WAY_VALVE,
    I_UNIT_ERROR,
)
from .coordinator import DcomCoordinator
from .entity import DcomEntity


@dataclass(frozen=True, kw_only=True)
class DcomBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[DcomCoordinator], bool | None]


def _flag(addr: int) -> Callable[[DcomCoordinator], bool | None]:
    def fn(c: DcomCoordinator) -> bool | None:
        val = c.value("i", addr)
        return None if val is None else val == 1

    return fn


def _problem(c: DcomCoordinator) -> bool | None:
    val = c.value("i", I_UNIT_ERROR)
    return None if val is None else val != 0


BINARY_SENSORS: tuple[DcomBinaryDescription, ...] = (
    DcomBinaryDescription(
        key="unit_error",
        name="Unit error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_problem,
    ),
    DcomBinaryDescription(
        key="pump", name="Circulation pump", device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_flag(I_PUMP),
    ),
    DcomBinaryDescription(
        key="compressor", name="Compressor", device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_flag(I_COMPRESSOR),
    ),
    DcomBinaryDescription(
        key="booster_heater", name="Booster heater",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_flag(I_BOOSTER_HEATER),
    ),
    DcomBinaryDescription(
        key="disinfection", name="Disinfection operation",
        value_fn=_flag(I_DISINFECTION),
    ),
    DcomBinaryDescription(key="defrost", name="Defrost / startup", value_fn=_flag(I_DEFROST)),
    DcomBinaryDescription(key="hot_start", name="Hot start", value_fn=_flag(I_HOT_START)),
    DcomBinaryDescription(
        key="three_way_valve_dhw", name="3-way valve in DHW position",
        value_fn=_flag(I_THREE_WAY_VALVE),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DcomBinarySensor(coordinator, d) for d in BINARY_SENSORS)


class DcomBinarySensor(DcomEntity, BinarySensorEntity):
    entity_description: DcomBinaryDescription

    def __init__(
        self, coordinator: DcomCoordinator, description: DcomBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator)
