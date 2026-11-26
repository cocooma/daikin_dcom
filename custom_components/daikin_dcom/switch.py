"""Switches."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import DOMAIN, H_QUIET_MODE
from .coordinator import DcomCoordinator
from .entity import DcomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DcomQuietModeSwitch(coordinator),
            DcomSmartHeatingSwitch(coordinator),
            DcomSmartWindowSwitch(coordinator),
            DcomSmartCloudySwitch(coordinator),
            DcomSmartDemandSwitch(coordinator),
        ]
    )


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


class _SmartData(ExtraStoredData):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def as_dict(self) -> dict[str, Any]:
        return self.data


class DcomSmartHeatingSwitch(DcomEntity, SwitchEntity, RestoreEntity):
    """Enables the HA-side heating curve and mild-day scheduling (smart.py)."""

    _attr_name = "Smart heating control"
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_heating")
        self._smart = coordinator.smart

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (extra := await self.async_get_last_extra_data()) is not None:
            self._smart.restore(extra.as_dict())
        self.async_on_remove(self._smart.add_listener(self.async_write_ha_state))

    @property
    def extra_restore_state_data(self) -> _SmartData:
        return _SmartData(self._smart.persist())

    @property
    def is_on(self) -> bool:
        return self._smart.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self._smart.status}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._smart.async_enable()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._smart.async_disable()
        self.async_write_ha_state()


class DcomSmartWindowSwitch(DcomEntity, SwitchEntity, RestoreEntity):
    """On: on mild days heat only inside the window. Off: no time restriction."""

    _attr_name = "Smart heating mild-day window"
    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_window_enabled")
        self._smart = coordinator.smart

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._smart.set_window(enabled=last.state == "on")

    @property
    def is_on(self) -> bool:
        return self._smart.settings.window_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._smart.set_window(enabled=True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._smart.set_window(enabled=False)
        self.async_write_ha_state()


class DcomSmartCloudySwitch(DcomEntity, SwitchEntity, RestoreEntity):
    """On: when it is cloudy AND a thermostat calls for heat, the mild-day window
    is not enforced. Needs the weather entity option. Off: window always enforced."""

    _attr_name = "Smart heating cloudy-day override"
    _attr_icon = "mdi:weather-cloudy-clock"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_cloudy_override")
        self._smart = coordinator.smart

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._smart.set_cloudy(last.state == "on")

    @property
    def is_on(self) -> bool:
        return self._smart.settings.cloudy_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._smart.set_cloudy(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._smart.set_cloudy(False)
        self.async_write_ha_state()


class DcomSmartDemandSwitch(DcomEntity, SwitchEntity, RestoreEntity):
    """On: space heating only runs while a selected thermostat calls for heat."""

    _attr_name = "Smart heating heat only on thermostat demand"
    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "smart_demand_only")
        self._smart = coordinator.smart

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._smart.set_demand_only(last.state == "on")

    @property
    def is_on(self) -> bool:
        return self._smart.settings.demand_only

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._smart.set_demand_only(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._smart.set_demand_only(False)
        self.async_write_ha_state()
