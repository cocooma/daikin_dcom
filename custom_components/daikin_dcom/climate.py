"""Climate entities: combined space heating/cooling and domestic hot water."""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
    PRESET_BOOST,
    PRESET_NONE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONTROL_LWT,
    DHW_RANGE,
    DOMAIN,
    H_DHW_BOOSTER,
    H_DHW_ONOFF,
    H_DHW_SP,
    H_LWT_COOL_SP,
    H_LWT_HEAT_SP,
    H_OPERATION_MODE,
    H_ROOM_COOL_SP,
    H_ROOM_HEAT_SP,
    H_SPACE_ONOFF,
    H_WEATHER_COOL_OFFSET,
    H_WEATHER_HEAT_OFFSET,
    H_WEATHER_MODE,
    I_BOOSTER_HEATER,
    I_COMPRESSOR,
    I_DEFROST,
    I_DHW_TEMP,
    I_LWT_PRE_PHE,
    I_OPERATION_MODE,
    I_ROOM_TEMP,
    I_THREE_WAY_VALVE,
    RANGES,
    WEATHER_DEPENDENT_MODES,
    WEATHER_OFFSET_RANGE,
)
from .coordinator import DcomCoordinator
from .entity import DcomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [DcomSpaceClimate(coordinator), DcomDhwClimate(coordinator)]
    )


class DcomSpaceClimate(DcomEntity, ClimateEntity):
    """Space heating + cooling in a single climate entity.

    HVAC modes map to registers 3 (operation mode) and 4 (space on/off):
      off  -> reg4 = 0
      heat -> reg3 = 1, reg4 = 1
      cool -> reg3 = 2, reg4 = 1
      auto -> reg3 = 0, reg4 = 1
    The target temperature written is the heating or cooling setpoint that is
    currently active (LWT regs 1/2 or room thermostat regs 6/7, selectable
    in the integration options).

    In LWT control with weather dependent mode active (reg 53 = 1 or 3) the
    fixed LWT setpoints are not used by the unit and the DCOM exposes no
    register for the curve-calculated setpoint. The target temperature then
    is the weather dependent setpoint offset (regs 54/55, -10..10 degC), like
    the shift shown on the Daikin user interface.
    """

    _attr_name = "Space heating/cooling"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "space_climate")
        lwt = coordinator.control_mode == CONTROL_LWT
        self._heat_reg = H_LWT_HEAT_SP if lwt else H_ROOM_HEAT_SP
        self._cool_reg = H_LWT_COOL_SP if lwt else H_ROOM_COOL_SP
        self._ranges = RANGES[coordinator.control_mode]

    @property
    def available(self) -> bool:
        c = self.coordinator
        return (
            super().available
            and c.value("h", H_SPACE_ONOFF) is not None
            and c.value("h", H_OPERATION_MODE) is not None
        )

    @property
    def _cooling_active(self) -> bool:
        """True if the cooling setpoint is the relevant one right now."""
        mode = self.coordinator.value("h", H_OPERATION_MODE)
        if mode == 2:
            return True
        if mode == 1:
            return False
        return self.coordinator.value("i", I_OPERATION_MODE) == 2  # auto

    @property
    def _weather_dependent(self) -> bool:
        """True if the target is the weather dependent offset."""
        c = self.coordinator
        return (
            c.control_mode == CONTROL_LWT
            and c.value("h", H_WEATHER_MODE) in WEATHER_DEPENDENT_MODES
        )

    @property
    def _target_reg(self) -> int:
        cooling = self._cooling_active
        if self._weather_dependent:
            return H_WEATHER_COOL_OFFSET if cooling else H_WEATHER_HEAT_OFFSET
        return self._cool_reg if cooling else self._heat_reg

    @property
    def hvac_mode(self) -> HVACMode | None:
        c = self.coordinator
        if c.value("h", H_SPACE_ONOFF) == 0:
            return HVACMode.OFF
        return {0: HVACMode.AUTO, 1: HVACMode.HEAT, 2: HVACMode.COOL}.get(
            c.value("h", H_OPERATION_MODE)
        )

    @property
    def hvac_action(self) -> HVACAction | None:
        c = self.coordinator
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if c.value("i", I_DEFROST) == 1:
            return HVACAction.DEFROSTING
        if c.value("i", I_THREE_WAY_VALVE) == 1:  # producing DHW
            return HVACAction.IDLE
        if c.value("i", I_COMPRESSOR) == 1 or c.value("i", I_BOOSTER_HEATER) == 1:
            if c.value("i", I_OPERATION_MODE) == 2:
                return HVACAction.COOLING
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        c = self.coordinator
        if c.control_mode == CONTROL_LWT:
            return c.temp("i", I_LWT_PRE_PHE)
        return c.temp("i", I_ROOM_TEMP)

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.value("h", self._target_reg)

    @property
    def _range(self) -> tuple[int, int]:
        if self._weather_dependent:
            return WEATHER_OFFSET_RANGE
        return self._ranges["cool" if self._cooling_active else "heat"]

    @property
    def min_temp(self) -> float:
        return self._range[0]

    @property
    def max_temp(self) -> float:
        return self._range[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        op = c.value("i", I_OPERATION_MODE)
        return {
            "active_operation": {1: "heating", 2: "cooling"}.get(op),
            "control_mode": c.control_mode,
            "weather_dependent": self._weather_dependent,
            "heating_setpoint": c.value("h", self._heat_reg),
            "cooling_setpoint": c.value("h", self._cool_reg),
            "heating_weather_offset": c.value("h", H_WEATHER_HEAT_OFFSET),
            "cooling_weather_offset": c.value("h", H_WEATHER_COOL_OFFSET),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        c = self.coordinator
        if hvac_mode == HVACMode.OFF:
            await c.async_write(H_SPACE_ONOFF, 0)
            return
        op = {HVACMode.AUTO: 0, HVACMode.HEAT: 1, HVACMode.COOL: 2}[hvac_mode]
        await c.async_write(H_OPERATION_MODE, op)
        await c.async_write(H_SPACE_ONOFF, 1)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        # negative offsets are written as two's complement by the coordinator
        await self.coordinator.async_write(self._target_reg, int(round(temp)))


class DcomDhwClimate(DcomEntity, ClimateEntity):
    """Domestic hot water as a separate climate entity.

    off  -> DHW reheat off (reg 12 = 0)
    heat -> DHW reheat on  (reg 12 = 1)
    preset 'boost' -> DHW booster mode (reg 13)
    """

    _attr_name = "Domestic hot water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1
    _attr_min_temp = DHW_RANGE[0]
    _attr_max_temp = DHW_RANGE[1]

    def __init__(self, coordinator: DcomCoordinator) -> None:
        super().__init__(coordinator, "dhw_climate")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.value("h", H_DHW_ONOFF) is not None
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        return (
            HVACMode.HEAT
            if self.coordinator.value("h", H_DHW_ONOFF) == 1
            else HVACMode.OFF
        )

    @property
    def hvac_action(self) -> HVACAction | None:
        c = self.coordinator
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if c.value("i", I_THREE_WAY_VALVE) == 1 and (
            c.value("i", I_COMPRESSOR) == 1 or c.value("i", I_BOOSTER_HEATER) == 1
        ):
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.temp("i", I_DHW_TEMP)

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.value("h", H_DHW_SP)

    @property
    def preset_mode(self) -> str | None:
        booster = self.coordinator.value("h", H_DHW_BOOSTER)
        if booster is None:
            return None
        return PRESET_BOOST if booster == 1 else PRESET_NONE

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_write(
            H_DHW_ONOFF, 1 if hvac_mode == HVACMode.HEAT else 0
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self.coordinator.async_write(H_DHW_SP, int(round(temp)))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.coordinator.async_write(
            H_DHW_BOOSTER, 1 if preset_mode == PRESET_BOOST else 0
        )
