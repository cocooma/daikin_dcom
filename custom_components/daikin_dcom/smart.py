"""Smart heating control running inside Home Assistant.

Replaces the unit's two-point weather curve with an HA-side curve and adds
mild-day block scheduling and optional room temperature feedback.

What it writes (only while enabled and the unit is heating):
  reg 53 = 0          fixed LWT setpoint mode (restored when disabled)
  reg 1  = curve LWT  integer degC, clamped to 25..max
  reg 4  = 0 / 1      space heating paused / resumed by the controller

Rules
  * Outdoor temperature is smoothed (exponential moving average) so short
    gusts / sun on the sensor do not move the setpoint.
  * avg >= heating off temperature           -> heating paused ("warm")
  * avg >= mild temperature, outside window  -> heating paused ("mild")
  * otherwise heating allowed, LWT from the curve (+ room offset)
  * Thresholds have a 0.5 degC hysteresis; on/off changes are rate limited
    (minimum on / off time) and setpoint writes are rate limited too, so the
    DCOM / unit is not written to on every poll.
  * The controller only resumes heating it paused itself. If space heating
    is switched off by the user, it stays off.
  * Nothing is touched in cooling mode or while defrosting.

The curve points are set at runtime through the "Heating curve ..." number
entities (initial values from the integration options); thresholds and the
room sensors / thermostats live in the integration options.
The mild-day window (start, end, on/off) is set at runtime through the
"Smart heating window start/end" time entities and the "Smart heating
mild-day window" switch, which restore their own state. Runtime state (enabled, paused-by-us, room offset, previous mode)
is persisted by the "Smart heating control" switch.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import logging
import math
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SMART_COLD_LWT,
    CONF_SMART_COLD_OUT,
    CONF_SMART_MAX_LWT,
    CONF_SMART_MILD_TEMP,
    CONF_SMART_OFF_TEMP,
    CONF_SMART_ROOM_MAX_OFFSET,
    CONF_SMART_ROOM_MODE,
    CONF_SMART_ROOM_SENSOR,
    CONF_SMART_ROOMS_OFF_PAUSE,
    ROOM_MODE_AVERAGE,
    ROOM_MODE_WARMEST,
    CONF_SMART_ROOM_TARGET,
    CONF_SMART_WARM_LWT,
    CONF_SMART_WARM_OUT,
    CONF_SMART_WINDOW_END,
    CONF_SMART_WINDOW_START,
    H_LWT_HEAT_SP,
    H_OPERATION_MODE,
    H_SPACE_ONOFF,
    H_WEATHER_MODE,
    I_DEFROST,
    I_OPERATION_MODE,
    I_OUTSIDE_TEMP,
    RANGES,
    CONTROL_LWT,
    SMART_DEFAULTS,
)

_LOGGER = logging.getLogger(__name__)

HYSTERESIS = 0.5  # degC on the warm / mild thresholds
EMA_TAU = timedelta(minutes=60)  # outdoor temperature smoothing
SETPOINT_MIN_INTERVAL = timedelta(minutes=10)  # between reg 1 writes
MIN_ON_TIME = timedelta(minutes=30)  # before the controller pauses again
MIN_OFF_TIME = timedelta(minutes=20)  # before the controller resumes
ROOM_STEP_INTERVAL = timedelta(minutes=45)  # between room offset steps
ROOM_DEADBAND = 0.5  # degC room error before the offset moves
TICK = timedelta(minutes=1)  # re-evaluate even without a poll (window edges)

LWT_FLOOR = RANGES[CONTROL_LWT]["heat"][0]  # reg 1 lower limit (25 degC)

STATUS_DISABLED = "disabled"
STATUS_WAITING = "waiting_for_data"
STATUS_COOLING = "cooling_mode"
STATUS_USER_OFF = "space_heating_off"
STATUS_DEFROST = "defrost"
STATUS_ACTIVE = "heating"
STATUS_PAUSED_WARM = "paused_warm"
STATUS_PAUSED_MILD = "paused_mild_outside_window"
STATUS_PAUSED_ROOMS_OFF = "paused_rooms_off"

# thermostat modes that count as "on" for heating
HEAT_MODES = ("heat", "auto", "heat_cool")


@dataclass
class SmartSettings:
    cold_out: float
    cold_lwt: float
    warm_out: float
    warm_lwt: float
    max_lwt: float
    mild_temp: float
    off_temp: float
    window_start: time
    window_end: time
    window_enabled: bool
    room_sensors: list[str]
    room_target: float
    room_max_offset: int
    room_mode: str
    rooms_off_pause: bool

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> SmartSettings:
        o = {**SMART_DEFAULTS, **options}
        return cls(
            cold_out=float(o[CONF_SMART_COLD_OUT]),
            cold_lwt=float(o[CONF_SMART_COLD_LWT]),
            warm_out=float(o[CONF_SMART_WARM_OUT]),
            warm_lwt=float(o[CONF_SMART_WARM_LWT]),
            max_lwt=float(o[CONF_SMART_MAX_LWT]),
            mild_temp=float(o[CONF_SMART_MILD_TEMP]),
            off_temp=float(o[CONF_SMART_OFF_TEMP]),
            window_start=_parse_time(o[CONF_SMART_WINDOW_START]),
            window_end=_parse_time(o[CONF_SMART_WINDOW_END]),
            window_enabled=True,
            room_sensors=_as_list(o.get(CONF_SMART_ROOM_SENSOR)),
            room_target=float(o[CONF_SMART_ROOM_TARGET]),
            room_max_offset=int(o[CONF_SMART_ROOM_MAX_OFFSET]),
            room_mode=str(o[CONF_SMART_ROOM_MODE]),
            rooms_off_pause=bool(o[CONF_SMART_ROOMS_OFF_PAUSE]),
        )


def _as_list(value: Any) -> list[str]:
    """Room entities: a list now, a single id (or "") in older options."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [v for v in value if v]


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    parts = [int(p) for p in str(value).split(":")]
    return time(parts[0], parts[1] if len(parts) > 1 else 0)


def curve_lwt(s: SmartSettings, outdoor: float) -> float:
    """Straight line through the two points, flat outside them."""
    if s.warm_out == s.cold_out:
        return s.cold_lwt
    lo, hi = sorted((s.cold_out, s.warm_out))
    t = min(max(outdoor, lo), hi)
    frac = (t - s.cold_out) / (s.warm_out - s.cold_out)
    return s.cold_lwt + frac * (s.warm_lwt - s.cold_lwt)


def in_window(now: time, start: time, end: time) -> bool:
    if start == end:
        return True  # empty window setting = always allowed
    if start < end:
        return start <= now < end
    return now >= start or now < end  # window crosses midnight


class SmartHeatingController:
    """Evaluates the rules after every poll and once a minute."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.settings = SmartSettings.from_options(entry.options)

        # persisted through the switch entity
        self.enabled = False
        self.paused_by_us = False
        self.room_offset = 0
        self.previous_weather_mode: int | None = None

        # runtime
        self.outdoor_avg: float | None = None
        self._last_sample: datetime | None = None
        self._last_setpoint_write: datetime | None = None
        self._last_onoff_change: datetime | None = None
        self._last_room_step: datetime | None = None
        self.rooms: list[dict[str, Any]] = []
        self.room_error: float | None = None
        self.room_driver: str | None = None
        self._paused_reason: str | None = None
        self.status = STATUS_DISABLED
        self.target_lwt: int | None = None
        self.curve_value: float | None = None

        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._unsubs.append(self.coordinator.async_add_listener(self._schedule))
        self._unsubs.append(
            async_track_time_interval(self.hass, lambda _now: self._schedule(), TICK)
        )

    def stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def restore(self, data: dict[str, Any]) -> None:
        self.enabled = bool(data.get("enabled", False))
        self.paused_by_us = bool(data.get("paused_by_us", False))
        self.room_offset = int(data.get("room_offset", 0))
        prev = data.get("previous_weather_mode")
        self.previous_weather_mode = None if prev is None else int(prev)
        # no immediate evaluation: the window time / switch entities restore
        # after this one. The next poll or minute tick evaluates.
        self._notify()

    def persist(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "paused_by_us": self.paused_by_us,
            "room_offset": self.room_offset,
            "previous_weather_mode": self.previous_weather_mode,
        }

    # --- enable / disable --------------------------------------------------
    async def async_enable(self) -> None:
        async with self._lock:
            mode = self.coordinator.value("h", H_WEATHER_MODE)
            if mode not in (None, 0):
                self.previous_weather_mode = mode
            self.enabled = True
        await self._async_evaluate()

    async def async_disable(self) -> None:
        async with self._lock:
            self.enabled = False
            c = self.coordinator
            try:
                if self.paused_by_us:
                    await c.async_write(H_SPACE_ONOFF, 1)
                if self.previous_weather_mode is not None:
                    await c.async_write(H_WEATHER_MODE, self.previous_weather_mode)
            except UpdateFailed as err:
                _LOGGER.warning("Smart heating: restoring unit state failed: %s", err)
            self.paused_by_us = False
            self.previous_weather_mode = None
            self.room_offset = 0
            self._paused_reason = None
            self.status = STATUS_DISABLED
            self.target_lwt = None
        self._notify()

    # --- evaluation --------------------------------------------------------
    @callback
    def _schedule(self) -> None:
        if self.enabled and not self._lock.locked():
            self.hass.async_create_task(self._async_evaluate())
        else:
            self._notify()

    def _update_outdoor(self, now: datetime) -> None:
        raw = self.coordinator.temp("i", I_OUTSIDE_TEMP)
        if raw is None:
            return
        if self.outdoor_avg is None or self._last_sample is None:
            self.outdoor_avg = raw
        else:
            dt = (now - self._last_sample).total_seconds()
            if dt <= 0:
                return
            alpha = 1 - math.exp(-dt / EMA_TAU.total_seconds())
            self.outdoor_avg += alpha * (raw - self.outdoor_avg)
        self._last_sample = now

    def _read_rooms(self) -> list[dict[str, Any]]:
        """Current temperature, target and on/off of every selected room.

        sensor.*  : temperature from the state, target from the options,
                    always counted as on.
        climate.* : current_temperature / temperature attributes (falls back
                    to target_temp_low, then the options target); on when its
                    mode is heat, auto or heat_cool.
        """
        s = self.settings
        rooms: list[dict[str, Any]] = []
        for entity_id in s.room_sensors:
            state = self.hass.states.get(entity_id)
            room: dict[str, Any] = {"entity": entity_id}
            room["thermostat"] = entity_id.startswith("climate.")
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                room.update(available=False, on=False, temperature=None, target=None)
            elif room["thermostat"]:
                attrs = state.attributes
                target = _float(attrs.get("temperature"))
                if target is None:
                    target = _float(attrs.get("target_temp_low"))
                room.update(
                    available=True,
                    on=state.state in HEAT_MODES,
                    temperature=_float(attrs.get("current_temperature")),
                    target=s.room_target if target is None else target,
                )
            else:
                room.update(
                    available=True,
                    on=True,
                    temperature=_float(state.state),
                    target=s.room_target,
                )
            rooms.append(room)
        return rooms

    def _aggregate_room_error(self) -> None:
        """target - temperature over the rooms that count, per room mode."""
        usable = [
            r
            for r in self.rooms
            if r["available"] and r["on"]
            and r["temperature"] is not None and r["target"] is not None
        ]
        if not usable:
            self.room_error = None
            self.room_driver = None
            return
        errors = {r["entity"]: r["target"] - r["temperature"] for r in usable}
        mode = self.settings.room_mode
        if mode == ROOM_MODE_AVERAGE:
            self.room_error = sum(errors.values()) / len(errors)
            self.room_driver = None
        else:
            pick = min if mode == ROOM_MODE_WARMEST else max  # coldest = largest shortfall
            self.room_driver = pick(errors, key=errors.get)
            self.room_error = errors[self.room_driver]

    def _all_thermostats_off(self) -> bool:
        """True if at least one selected thermostat is readable and none is on."""
        stats = [r for r in self.rooms if r["thermostat"] and r["available"]]
        return bool(stats) and not any(r["on"] for r in stats)

    def _update_room_offset(self, now: datetime) -> None:
        s = self.settings
        if self.room_error is None:
            return  # no usable room: keep the correction where it is
        if self._last_room_step and now - self._last_room_step < ROOM_STEP_INTERVAL:
            return
        error = self.room_error
        step = 0
        if error >= ROOM_DEADBAND:
            step = 1
        elif error <= -ROOM_DEADBAND:
            step = -1
        new = max(-s.room_max_offset, min(s.room_max_offset, self.room_offset + step))
        if new != self.room_offset:
            self.room_offset = new
            self._last_room_step = now

    def _heating_allowed(self, now: datetime) -> tuple[bool, str | None]:
        """Threshold logic with hysteresis around the current pause reason."""
        s = self.settings
        t = self.outdoor_avg
        reason = self._paused_reason
        if s.rooms_off_pause and self._all_thermostats_off():
            return False, "rooms_off"
        warm_limit = s.off_temp - (HYSTERESIS if reason == "warm" else 0)
        if t >= warm_limit:
            return False, "warm"
        mild_limit = s.mild_temp - (HYSTERESIS if reason == "mild" else 0)
        local = dt_util.as_local(now).time()
        if (
            s.window_enabled
            and t >= mild_limit
            and not in_window(local, s.window_start, s.window_end)
        ):
            return False, "mild"
        return True, None

    async def _async_evaluate(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._evaluate()
            except UpdateFailed as err:
                _LOGGER.warning("Smart heating: write failed: %s", err)
        self._notify()

    async def _evaluate(self) -> None:
        c = self.coordinator
        now = dt_util.utcnow()
        if not self.enabled:
            self.status = STATUS_DISABLED
            return
        self._update_outdoor(now)

        op = c.value("h", H_OPERATION_MODE)
        onoff = c.value("h", H_SPACE_ONOFF)
        if op is None or onoff is None or self.outdoor_avg is None:
            self.status = STATUS_WAITING
            return
        cooling = op == 2 or (op == 0 and c.value("i", I_OPERATION_MODE) == 2)
        if cooling:
            self.status = STATUS_COOLING
            return
        if onoff == 0 and not self.paused_by_us:
            self.status = STATUS_USER_OFF  # user switched it off: respect it
            return
        if c.value("i", I_DEFROST) == 1:
            self.status = STATUS_DEFROST
            return

        # 1. take over the setpoint: fixed LWT mode
        if c.value("h", H_WEATHER_MODE) not in (None, 0):
            self.previous_weather_mode = c.value("h", H_WEATHER_MODE)
            await c.async_write(H_WEATHER_MODE, 0)

        # 2. setpoint from curve + room offset
        s = self.settings
        self.rooms = self._read_rooms()
        self._aggregate_room_error()
        self._update_room_offset(now)
        self.curve_value = curve_lwt(s, self.outdoor_avg)
        target = round(self.curve_value + self.room_offset)
        target = int(max(LWT_FLOOR, min(s.max_lwt, target)))
        self.target_lwt = target
        current = c.value("h", H_LWT_HEAT_SP)
        due = (
            self._last_setpoint_write is None
            or now - self._last_setpoint_write >= SETPOINT_MIN_INTERVAL
            or current is None
            or abs(target - current) >= 2
        )
        if current != target and due:
            await c.async_write(H_LWT_HEAT_SP, target)
            self._last_setpoint_write = now

        # 3. pause / resume
        allowed, reason = self._heating_allowed(now)
        since = (
            None if self._last_onoff_change is None else now - self._last_onoff_change
        )
        if not allowed and onoff == 1:
            if since is None or since >= MIN_ON_TIME:
                await c.async_write(H_SPACE_ONOFF, 0)
                self.paused_by_us = True
                self._paused_reason = reason
                self._last_onoff_change = now
        elif allowed and onoff == 0 and self.paused_by_us:
            if since is None or since >= MIN_OFF_TIME:
                await c.async_write(H_SPACE_ONOFF, 1)
                self.paused_by_us = False
                self._paused_reason = None
                self._last_onoff_change = now
        elif not allowed and onoff == 0:
            self._paused_reason = reason

        if self.paused_by_us:
            self.status = {
                "warm": STATUS_PAUSED_WARM,
                "rooms_off": STATUS_PAUSED_ROOMS_OFF,
            }.get(self._paused_reason, STATUS_PAUSED_MILD)
        else:
            self.status = STATUS_ACTIVE

    # --- runtime window settings (time / switch entities) -----------------
    @callback
    def set_window(
        self,
        start: time | None = None,
        end: time | None = None,
        enabled: bool | None = None,
    ) -> None:
        s = self.settings
        if start is not None:
            s.window_start = start
        if end is not None:
            s.window_end = end
        if enabled is not None:
            s.window_enabled = enabled
        self._schedule()

    # --- runtime curve settings (number entities) --------------------------
    CURVE_FIELDS = ("cold_out", "cold_lwt", "warm_out", "warm_lwt")

    def curve_valid(self, field: str, value: float) -> bool:
        """The warm point must stay warmer (outdoor) than the cold point."""
        s = self.settings
        cold = value if field == "cold_out" else s.cold_out
        warm = value if field == "warm_out" else s.warm_out
        return warm > cold

    @callback
    def set_curve(self, field: str, value: float, evaluate: bool = True) -> None:
        """Change a curve point; evaluate=False while restoring at startup."""
        if field not in self.CURVE_FIELDS:
            raise ValueError(field)
        setattr(self.settings, field, float(value))
        if evaluate:
            self._last_setpoint_write = None  # apply the new curve straight away
            self._schedule()
        else:
            self._notify()

    # --- for entities ------------------------------------------------------
    @property
    def curve_points(self) -> list[list[float]]:
        """[outdoor, lwt] pairs from -15 to +20 degC, for dashboard charts."""
        s = self.settings
        return [
            [t, round(max(LWT_FLOOR, min(s.max_lwt, curve_lwt(s, t))), 1)]
            for t in range(-15, 21)
        ]

    @property
    def attributes(self) -> dict[str, Any]:
        s = self.settings
        return {
            "outdoor_average": None
            if self.outdoor_avg is None
            else round(self.outdoor_avg, 1),
            "curve_lwt": None if self.curve_value is None else round(self.curve_value, 1),
            "room_offset": self.room_offset,
            "room_mode": s.room_mode if s.room_sensors else None,
            "room_error": None if self.room_error is None else round(self.room_error, 1),
            "room_driver": self.room_driver,
            "rooms": [
                {
                    "entity": r["entity"],
                    "temperature": r["temperature"],
                    "target": r["target"],
                    "on": r["on"],
                }
                for r in self.rooms
            ],
            "paused_by_controller": self.paused_by_us,
            "curve": f"{s.cold_out:g}°C→{s.cold_lwt:g}°C, {s.warm_out:g}°C→{s.warm_lwt:g}°C",
            "mild_temp": s.mild_temp,
            "heating_off_temp": s.off_temp,
            "mild_window": f"{s.window_start:%H:%M}–{s.window_end:%H:%M}"
            if s.window_enabled
            else "disabled",
        }
