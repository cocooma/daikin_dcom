"""COP metrics: thermal power / energy from the DCOM, electricity from a meter.

Thermal power (kW) = flow [l/min] / 60 * 4.186 * dT
  heating, DHW : dT = LWT - return   (negative during defrost, which
                                      correctly reduces the heat counted)
  cooling      : dT = return - LWT
LWT is "Leaving water temperature pre BUH" (input 41), falling back to
"pre PHE" (input 40). Heat from the DHW tank booster heater is not measured.

Electricity comes from an external sensor chosen in the options: an energy
sensor (kWh, preferred: deltas between polls) or a power sensor (W / kW,
integrated). It is assigned to the mode the unit is in at that moment, so
standby consumption counts too:
  DHW     : 3-way valve on DHW (input 37 = 1)
  cooling : operation mode cooling (input 38 = 2)
  heating : otherwise

Per mode it keeps lifetime and today's thermal / electrical kWh, plus a
rolling 15 minute COP. State is persisted by the thermal power sensor.
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COP_ENERGY_SENSOR,
    CONF_COP_POWER_SENSOR,
    I_FLOW_RATE,
    I_LWT_PRE_BUH,
    I_LWT_PRE_PHE,
    I_OPERATION_MODE,
    I_PUMP,
    I_RETURN_WATER,
    I_THREE_WAY_VALVE,
)

_LOGGER = logging.getLogger(__name__)

CP_WATER = 4.186  # kJ/(kg K), 1 l ~ 1 kg
MAX_GAP = timedelta(minutes=5)  # longer gaps (restart, outage) are skipped
WINDOW = timedelta(minutes=15)  # rolling COP
MIN_WINDOW_KWH = 0.02  # electricity needed in the window for a COP value

MODE_HEATING = "heating"
MODE_COOLING = "cooling"
MODE_DHW = "dhw"
MODES = (MODE_HEATING, MODE_COOLING, MODE_DHW)

_ENERGY_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_POWER_FACTORS = {"W": 0.001, "kW": 1.0}


class CopTracker:
    def __init__(self, hass: HomeAssistant, options: dict[str, Any], coordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.energy_sensor: str | None = options.get(CONF_COP_ENERGY_SENSOR) or None
        self.power_sensor: str | None = options.get(CONF_COP_POWER_SENSOR) or None

        self.total = {m: {"th": 0.0, "el": 0.0} for m in MODES}
        self.today = {m: {"th": 0.0, "el": 0.0} for m in MODES}
        self.today_date: date = dt_util.now().date()

        self.mode: str | None = None
        self.thermal_kw: float | None = None
        self.electrical_kw: float | None = None
        self._last_time: datetime | None = None
        self._last_energy: float | None = None
        self._window: deque[tuple[datetime, str, float, float]] = deque()

        self._unsub: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def has_meter(self) -> bool:
        return bool(self.energy_sensor or self.power_sensor)

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._unsub = self.coordinator.async_add_listener(self._on_update)

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def persist(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "today": self.today,
            "today_date": self.today_date.isoformat(),
        }

    def restore(self, data: dict[str, Any]) -> None:
        try:
            for m in MODES:
                for k in ("th", "el"):
                    self.total[m][k] = float(data["total"][m][k])
            if date.fromisoformat(data["today_date"]) == dt_util.now().date():
                for m in MODES:
                    for k in ("th", "el"):
                        self.today[m][k] = float(data["today"][m][k])
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug("COP: no usable stored state")

    # --- measurement -------------------------------------------------------
    def _current_mode(self) -> str:
        c = self.coordinator
        if c.value("i", I_THREE_WAY_VALVE) == 1:
            return MODE_DHW
        if c.value("i", I_OPERATION_MODE) == 2:
            return MODE_COOLING
        return MODE_HEATING

    def _thermal_kw(self, mode: str) -> float | None:
        c = self.coordinator
        flow = c.value("i", I_FLOW_RATE)
        if c.value("i", I_PUMP) == 0 or flow is not None and flow <= 0:
            return 0.0
        lwt = c.temp("i", I_LWT_PRE_BUH)
        if lwt is None:
            lwt = c.temp("i", I_LWT_PRE_PHE)
        ret = c.temp("i", I_RETURN_WATER)
        if flow is None or lwt is None or ret is None:
            return None
        delta = ret - lwt if mode == MODE_COOLING else lwt - ret
        return flow / 100 / 60 * CP_WATER * delta

    def _state_float(self, entity_id: str) -> tuple[float, str | None] | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return float(state.state), state.attributes.get("unit_of_measurement")
        except ValueError:
            return None

    def _electrical_kwh(self, hours: float) -> float | None:
        """Electricity used since the last poll (kWh), or None if unknown."""
        if self.energy_sensor:
            read = self._state_float(self.energy_sensor)
            if read is not None:
                value = read[0] * _ENERGY_FACTORS.get(read[1] or "kWh", 1.0)
                prev, self._last_energy = self._last_energy, value
                if prev is None or value < prev:  # first sample / meter reset
                    return None
                return value - prev
        if self.power_sensor:
            read = self._state_float(self.power_sensor)
            if read is not None:
                kw = read[0] * _POWER_FACTORS.get(read[1] or "W", 0.001)
                return max(kw, 0.0) * hours
        return None

    @callback
    def _on_update(self) -> None:
        now = dt_util.utcnow()
        local_day = dt_util.now().date()
        if local_day != self.today_date:
            self.today = {m: {"th": 0.0, "el": 0.0} for m in MODES}
            self.today_date = local_day

        mode = self._current_mode()
        thermal = self._thermal_kw(mode)
        self.mode = mode
        self.thermal_kw = None if thermal is None else round(thermal, 3)

        last, self._last_time = self._last_time, now
        if last is None or now - last > MAX_GAP or now <= last:
            # still read the energy meter so the next delta is valid
            self._electrical_kwh(0.0)
            self.electrical_kw = None
            self._notify()
            return
        hours = (now - last).total_seconds() / 3600

        th_kwh = 0.0 if thermal is None else thermal * hours
        el_kwh = self._electrical_kwh(hours)
        self.electrical_kw = None if el_kwh is None else round(el_kwh / hours, 3)

        for bucket in (self.total[mode], self.today[mode]):
            bucket["th"] += th_kwh
            if el_kwh is not None:
                bucket["el"] += el_kwh

        self._window.append((now, mode, th_kwh, el_kwh or 0.0))
        while self._window and now - self._window[0][0] > WINDOW:
            self._window.popleft()
        self._notify()

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # --- values for entities ------------------------------------------------
    @staticmethod
    def _ratio(th: float, el: float, min_el: float = 0.05) -> float | None:
        return round(th / el, 2) if el >= min_el else None

    def cop_total(self, mode: str) -> float | None:
        return self._ratio(self.total[mode]["th"], self.total[mode]["el"])

    def cop_today(self, mode: str) -> float | None:
        return self._ratio(self.today[mode]["th"], self.today[mode]["el"])

    @property
    def cop_now(self) -> float | None:
        """COP over the last 15 minutes, for the mode the unit is in now."""
        if not self._window or self.mode is None:
            return None
        th = sum(w[2] for w in self._window if w[1] == self.mode)
        el = sum(w[3] for w in self._window if w[1] == self.mode)
        return self._ratio(th, el, MIN_WINDOW_KWH)
