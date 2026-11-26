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
  * avg >= heating off temperature           -> heating paused ("warm"); optionally
    the selected thermostats are switched off too, and back on when heating restarts
  * avg >= mild temperature, outside window  -> heating paused ("mild"),
    unless it is cloudy and a thermostat is calling for heat (cloudy-day
    override: no sun to wait for, so the window gains nothing)
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
    DOMAIN,
    CONF_SMART_COLD_LWT,
    CONF_SMART_COLD_OUT,
    CONF_SMART_EARLY_DELAY,
    CONF_SMART_EARLY_MIN_RUN,
    CONF_SMART_EARLY_SHORTFALL,
    CONF_SMART_EARLY_START,
    CONF_SMART_WEATHER_ENTITY,
    CONF_SMART_CLOUD_COVERAGE,
    CONF_SMART_WARM_STATS_OFF,
    CONF_SMART_REST,
    CONF_SMART_REST_MINUTES,
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
    I_COMPRESSOR,
    I_DEFROST,
    I_THREE_WAY_VALVE,
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
STATUS_EARLY_START = "heating_early_start"
STATUS_CLOUDY = "heating_cloudy_override"
STATUS_COMPRESSOR_REST = "compressor_rest"

EARLY_STOP_BAND = 0.3  # degC: an early start ends once the coldest room is this close to target

# cloudy-day override of the mild-day window
CLOUDY_STATES = (
    "cloudy", "fog", "rainy", "pouring", "snowy", "snowy-rainy",
    "hail", "lightning", "lightning-rainy",
)
CLOUDY_HOLD = timedelta(minutes=15)  # override ends after this long without cloud + demand
CALLING_ACTIONS = ("heating", "preheating")  # thermostat hvac_action = asking for heat
CALLING_BAND = 0.3  # degC below target = calling, for thermostats without hvac_action

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
    early_start: bool
    early_shortfall: float
    early_delay: timedelta
    early_min_run: timedelta
    weather_entity: str
    cloud_coverage: float  # % cloud_coverage that counts as cloudy (e.g. a grey "partlycloudy")
    warm_stats_off: bool
    cloudy_enabled: bool  # runtime, "Smart heating cloudy-day override" switch
    rest: bool
    rest_cold_out: float  # rest curve: outdoor °C → minutes, flat outside the points
    rest_cold_min: float
    rest_mild_out: float
    rest_mild_min: float

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
            early_start=bool(o[CONF_SMART_EARLY_START]),
            early_shortfall=float(o[CONF_SMART_EARLY_SHORTFALL]),
            early_delay=timedelta(minutes=float(o[CONF_SMART_EARLY_DELAY])),
            early_min_run=timedelta(minutes=float(o[CONF_SMART_EARLY_MIN_RUN])),
            weather_entity=str(o.get(CONF_SMART_WEATHER_ENTITY) or ""),
            cloud_coverage=float(o[CONF_SMART_CLOUD_COVERAGE]),
            warm_stats_off=bool(o[CONF_SMART_WARM_STATS_OFF]),
            cloudy_enabled=True,
            rest=bool(o[CONF_SMART_REST]),
            rest_cold_out=0.0,
            rest_cold_min=float(o[CONF_SMART_REST_MINUTES]),
            rest_mild_out=15.0,
            rest_mild_min=float(o[CONF_SMART_REST_MINUTES]),
        )


def _hm(when: datetime) -> str:
    return dt_util.as_local(when).strftime("%H:%M")


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
        # early start for cold rooms (mild-day pause only)
        self.early_active = False
        self.early_room: str | None = None
        self._cold_since: datetime | None = None
        self._early_started: datetime | None = None
        # cloudy-day override of the mild-day window
        self.cloudy: bool | None = None
        self.calling_rooms: list[str] = []
        self.cloudy_active = False
        self._cloudy_clear_since: datetime | None = None
        # thermostats we switched off for the warm pause: entity -> hvac mode to restore
        self.stats_off_by_us: dict[str, str] = {}
        # compressor: start counting and rest after the unit's own stops
        self._comp_prev: int | None = None
        self._valve_prev: int | None = None
        self._defrost_prev: int | None = None
        self._comp_stop_at: datetime | None = None  # unit's own stop, not yet handled
        self.last_stop: tuple[datetime, str] | None = None  # diagnostics
        self.status_entity_id: str | None = None  # set by the status sensor, for the logbook
        self.rest_until: datetime | None = None
        self.starts_today = 0
        self.heating_starts_today = 0
        self._starts_day = None
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
        self.stats_off_by_us = {
            str(k): str(v) for k, v in (data.get("thermostats_off") or {}).items()
        }
        prev = data.get("previous_weather_mode")
        self.previous_weather_mode = None if prev is None else int(prev)
        starts = data.get("starts") or {}
        if starts.get("day") == dt_util.as_local(dt_util.utcnow()).date().isoformat():
            self._starts_day = dt_util.as_local(dt_util.utcnow()).date()
            self.starts_today = int(starts.get("all", 0))
            self.heating_starts_today = int(starts.get("heating", 0))
        # no immediate evaluation: the window time / switch entities restore
        # after this one. The next poll or minute tick evaluates.
        self._notify()

    def persist(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "paused_by_us": self.paused_by_us,
            "room_offset": self.room_offset,
            "previous_weather_mode": self.previous_weather_mode,
            "thermostats_off": self.stats_off_by_us,
            "starts": {
                "day": None if self._starts_day is None else self._starts_day.isoformat(),
                "all": self.starts_today,
                "heating": self.heating_starts_today,
            },
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
            await self._thermostats_on("smart heating switched off")
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
        self._track_compressor(dt_util.utcnow())
        if self.enabled and not self._lock.locked():
            self.hass.async_create_task(self._async_evaluate())
        else:
            self._notify()

    def _track_compressor(self, now: datetime) -> None:
        """Count compressor starts and note the unit's own space heating stops.

        A stop counts for the rest only when it happens in space heating: not
        while producing hot water (now or on the previous reading), not while
        space heating is off and not while the controller has paused heating
        itself (space heating off). Input 35 is "defrost / startup": it is also set while the
        compressor starts up, so it must not exclude stops - a short run that
        ends during startup is exactly the stop the rest is for.
        """
        c = self.coordinator
        comp = c.value("i", I_COMPRESSOR)
        valve = c.value("i", I_THREE_WAY_VALVE)
        defrost = c.value("i", I_DEFROST)
        if comp is None:
            return
        today = dt_util.as_local(now).date()
        if self._starts_day != today:
            self._starts_day = today
            self.starts_today = 0
            self.heating_starts_today = 0
        prev = self._comp_prev
        if prev == 0 and comp == 1:
            self.starts_today += 1
            if valve != 1:
                self.heating_starts_today += 1
        elif prev == 1 and comp == 0:
            in_dhw = 1 in (valve, self._valve_prev)
            heating_on = c.value("h", H_SPACE_ONOFF) == 1
            if in_dhw:
                outcome = "ignored (hot water)"
            elif not heating_on:
                outcome = (
                    "ignored (paused by controller)"
                    if self.paused_by_us
                    else "ignored (space heating off)"
                )
            elif not self.settings.rest:
                outcome = "ignored (rest switched off)"
            else:
                outcome = "rest"
                self._comp_stop_at = now
            if not self.enabled and outcome == "rest":
                outcome = "ignored (smart heating off)"
            self.last_stop = (now, outcome)
        self._comp_prev, self._valve_prev, self._defrost_prev = comp, valve, defrost

    def _update_rest(self, now: datetime) -> bool:
        """Start, keep or end the compressor rest; True while resting."""
        s = self.settings
        c = self.coordinator
        stop, self._comp_stop_at = self._comp_stop_at, None
        if stop is not None and now - stop > timedelta(minutes=5):
            stop = None  # stale: noted while the controller wasn't evaluating
        if not s.rest:
            if self.rest_until is not None:
                self._log(
                    f"Compressor rest switched off; LWT target back to {self.target_lwt} °C"
                )
            self.rest_until = None
            return False
        if stop is not None and c.value("h", H_SPACE_ONOFF) == 1:
            self.rest_until = stop + self.rest_time
            minutes = int(self.rest_time.total_seconds() // 60)
            self._log(
                f"Compressor stopped at {_hm(stop)}: resting {minutes} min until "
                f"{_hm(self.rest_until)}; LWT target lowered from {self.target_lwt} °C "
                f"to {LWT_FLOOR} °C"
            )
        if self.rest_until is None:
            return False
        if now >= self.rest_until or c.value("i", I_COMPRESSOR) == 1:
            # rest over, or the water fell below 25 °C and the unit restarted
            why = (
                "unit restarted the compressor during the rest (water below "
                f"{LWT_FLOOR} °C)"
                if c.value("i", I_COMPRESSOR) == 1
                else "compressor rest over"
            )
            self._log(f"{why[0].upper()}{why[1:]}; LWT target back to {self.target_lwt} °C")
            self.rest_until = None
            self._last_setpoint_write = None  # put the curve back straight away
            return False
        return True

    def _log(self, message: str) -> None:
        """Logbook / Activity entry on the status sensor, plus the HA log."""
        _LOGGER.info("Smart heating: %s", message)
        try:
            from homeassistant.components.logbook import async_log_entry

            async_log_entry(
                self.hass, "Smart heating", message, DOMAIN, self.status_entity_id
            )
        except Exception:  # noqa: BLE001 - logbook not loaded: the HA log is enough
            pass

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
                    mode is heat, auto or heat_cool; calling for heat when its
                    hvac_action is heating, or (no hvac_action) it is 0.3 degC below target.
        """
        s = self.settings
        rooms: list[dict[str, Any]] = []
        for entity_id in s.room_sensors:
            state = self.hass.states.get(entity_id)
            room: dict[str, Any] = {"entity": entity_id}
            room["thermostat"] = entity_id.startswith("climate.")
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                room.update(available=False, on=False, temperature=None, target=None, calling=False)
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
                action = attrs.get("hvac_action")
                if action is not None:
                    calling = action in CALLING_ACTIONS
                else:
                    calling = (
                        room["temperature"] is not None
                        and room["temperature"] <= room["target"] - CALLING_BAND
                    )
                room["calling"] = room["on"] and calling
            else:
                room.update(
                    available=True,
                    on=True,
                    temperature=_float(state.state),
                    target=s.room_target,
                    calling=False,  # plain sensors don't ask for heat
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

    def _coldest_room(self) -> tuple[float, str] | None:
        """Largest shortfall (target - temperature) of the counted rooms."""
        usable = [
            (r["target"] - r["temperature"], r["entity"])
            for r in self.rooms
            if r["available"] and r["on"]
            and r["temperature"] is not None and r["target"] is not None
        ]
        return max(usable) if usable else None

    def _early_start(self, now: datetime, allowed: bool, reason: str | None) -> bool:
        """Let a cold room override the mild-day pause; returns the new allowed.

        Starts when the coldest room has been >= early_shortfall below its
        target for early_delay. Runs at least early_min_run and ends once the
        coldest room is within EARLY_STOP_BAND of its target. Any other state
        (heating allowed anyway, warm cut-off, all thermostats off) cancels it.
        """
        s = self.settings
        if not s.early_start or allowed or reason != "mild":
            self.early_active = False
            self._early_started = None
            self._cold_since = None
            self.early_room = None
            return allowed

        coldest = self._coldest_room()
        if coldest is not None and coldest[0] >= s.early_shortfall:
            if self._cold_since is None:
                self._cold_since = now
        else:
            self._cold_since = None

        if self.early_active:
            done = coldest is None or coldest[0] <= EARLY_STOP_BAND
            ran = self._early_started is not None and now - self._early_started >= s.early_min_run
            if done and ran:
                self.early_active = False
                self._early_started = None
                self.early_room = None
                return False
            return True

        if self._cold_since is not None and now - self._cold_since >= s.early_delay:
            self.early_active = True
            self.early_room = coldest[1] if coldest else None
            self._cold_since = None
            return True
        return False

    def _is_cloudy(self) -> bool | None:
        """Cloudy per the weather entity: overcast / wet / foggy state, or a
        cloud_coverage attribute of at least the cloud coverage option (%). None = unknown."""
        state = self.hass.states.get(self.settings.weather_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        if state.state in CLOUDY_STATES:
            return True
        coverage = _float(state.attributes.get("cloud_coverage"))
        return coverage is not None and coverage >= self.settings.cloud_coverage

    def _cloudy_override(self, now: datetime, allowed: bool, reason: str | None) -> bool:
        """Skip the mild-day pause while it is cloudy and a thermostat calls for heat.

        The window exists to heat in the warm, sunny hours; under cloud there is
        little to gain by waiting. Starts as soon as both hold, ends once they
        have not held together for CLOUDY_HOLD (thermostats cycle, the weather
        state flips). Only the mild-day pause is overridden: the warm cut-off and
        the all-thermostats-off pause still apply. Returns the new allowed.
        """
        s = self.settings
        self.calling_rooms = [
            r["entity"] for r in self.rooms if r["thermostat"] and r["available"] and r.get("calling")
        ]
        self.cloudy = self._is_cloudy() if s.weather_entity else None
        if not (s.cloudy_enabled and s.weather_entity) or allowed or reason != "mild":
            self.cloudy_active = False
            self._cloudy_clear_since = None
            return allowed

        if self.cloudy and self.calling_rooms:
            self._cloudy_clear_since = None
            if not self.cloudy_active:
                self.cloudy_active = True
                self._log(
                    "Mild-day window not enforced: cloudy and "
                    + ", ".join(self.calling_rooms)
                    + " calling for heat"
                )
            return True
        if self.cloudy_active:
            if self._cloudy_clear_since is None:
                self._cloudy_clear_since = now
            if now - self._cloudy_clear_since < CLOUDY_HOLD:
                return True
            self.cloudy_active = False
            self._cloudy_clear_since = None
            self._log(
                "Mild-day window enforced again: "
                + ("no thermostat calling for heat" if self.cloudy else "no longer cloudy")
            )
        return False

    async def _sync_thermostats(self) -> None:
        """Warm pause: switch the selected thermostats off; heating back on: restore them."""
        if self.paused_by_us and self._paused_reason == "warm":
            if self.settings.warm_stats_off and not self.stats_off_by_us:
                await self._thermostats_off()
        elif not self.paused_by_us and self.stats_off_by_us:
            await self._thermostats_on("heating restarted")

    async def _thermostats_off(self) -> None:
        saved: dict[str, str] = {}
        for entity_id in self.settings.room_sensors:
            if not entity_id.startswith("climate."):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("off", STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue
            saved[entity_id] = state.state
        if not saved:
            return
        await self._set_modes({e: "off" for e in saved})
        self.stats_off_by_us = saved
        self._log("Warm weather pause: thermostats switched off (" + ", ".join(saved) + ")")

    async def _thermostats_on(self, why: str) -> None:
        """Back to the saved mode; thermostats someone changed meanwhile are left alone."""
        if not self.stats_off_by_us:
            return
        restore = {}
        for entity_id, mode in self.stats_off_by_us.items():
            state = self.hass.states.get(entity_id)
            if state is not None and state.state == "off":
                restore[entity_id] = mode
        self.stats_off_by_us = {}
        if restore:
            await self._set_modes(restore)
            self._log(f"{why[0].upper()}{why[1:]}: thermostats switched back on (" + ", ".join(restore) + ")")

    async def _set_modes(self, modes: dict[str, str]) -> None:
        for entity_id, mode in modes.items():
            try:
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": mode},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001 - one bad thermostat must not stop the rest
                _LOGGER.warning("Smart heating: setting %s to %s failed: %s", entity_id, mode, err)

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
        if s.rooms_off_pause and not self.stats_off_by_us and self._all_thermostats_off():
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
        if onoff == 1 and self.paused_by_us:
            # Heating is on although we think we paused it: someone else switched it
            # back on (climate entity, Daikin UI, restored state after a restart).
            # Drop the stale flag, otherwise stops are ignored and we never manage it.
            self._log("Space heating found on while marked as paused; taking over again")
            self.paused_by_us = False
            self._paused_reason = None
        if onoff == 0 and not self.paused_by_us:
            self.status = STATUS_USER_OFF  # user switched it off: respect it
            return
        # input 35 is "defrost / startup": keep handling the rest and the
        # setpoint, only leave the pause / resume decisions alone meanwhile
        defrost_or_startup = c.value("i", I_DEFROST) == 1

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
        resting = self._update_rest(now)
        if resting:
            # keep space heating (and the pump) on, but below the water temperature
            target = LWT_FLOOR
        current = c.value("h", H_LWT_HEAT_SP)
        due = (
            resting
            or self._last_setpoint_write is None
            or now - self._last_setpoint_write >= SETPOINT_MIN_INTERVAL
            or current is None
            or abs(target - current) >= 2
        )
        if current != target and due:
            await c.async_write(H_LWT_HEAT_SP, target)
            self._last_setpoint_write = now

        if defrost_or_startup:
            self.status = STATUS_COMPRESSOR_REST if self.rest_until is not None else STATUS_DEFROST
            return

        # 3. pause / resume
        allowed, reason = self._heating_allowed(now)
        allowed = self._cloudy_override(now, allowed, reason)
        allowed = self._early_start(now, allowed, reason)
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
        await self._sync_thermostats()
        # the early start's minimum run counts from when heating is really on
        if self.early_active and self._early_started is None and c.value("h", H_SPACE_ONOFF) == 1:
            self._early_started = now

        if self.paused_by_us:
            self.status = {
                "warm": STATUS_PAUSED_WARM,
                "rooms_off": STATUS_PAUSED_ROOMS_OFF,
            }.get(self._paused_reason, STATUS_PAUSED_MILD)
        elif self.rest_until is not None:
            self.status = STATUS_COMPRESSOR_REST
        elif self.cloudy_active:
            self.status = STATUS_CLOUDY
        elif self.early_active:
            self.status = STATUS_EARLY_START
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

    def set_cloudy(self, enabled: bool) -> None:
        """Cloudy-day override on / off (switch entity)."""
        self.settings.cloudy_enabled = enabled
        self._schedule()

    # --- runtime curve settings (number entities) --------------------------
    CURVE_FIELDS = ("cold_out", "cold_lwt", "warm_out", "warm_lwt")
    REST_FIELDS = ("rest_cold_out", "rest_cold_min", "rest_mild_out", "rest_mild_min")

    @property
    def rest_time(self) -> timedelta:
        """Compressor rest from the averaged outdoor temperature (two-point curve)."""
        s = self.settings
        t = self.outdoor_avg
        if t is None or s.rest_mild_out == s.rest_cold_out:
            return timedelta(minutes=s.rest_cold_min)
        lo, hi = sorted((s.rest_cold_out, s.rest_mild_out))
        t = min(max(t, lo), hi)
        frac = (t - s.rest_cold_out) / (s.rest_mild_out - s.rest_cold_out)
        minutes = s.rest_cold_min + frac * (s.rest_mild_min - s.rest_cold_min)
        return timedelta(minutes=round(minutes))

    def rest_valid(self, field: str, value: float) -> bool:
        """The mild point must stay warmer (outdoor) than the cold point."""
        s = self.settings
        cold = value if field == "rest_cold_out" else s.rest_cold_out
        mild = value if field == "rest_mild_out" else s.rest_mild_out
        return mild > cold

    @callback
    def set_rest(self, field: str, value: float) -> None:
        """Change a rest curve point; applies to the next rest, no writes."""
        if field not in self.REST_FIELDS:
            raise ValueError(field)
        setattr(self.settings, field, float(value))
        self._notify()


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
            "compressor_rest_minutes": int(self.rest_time.total_seconds() // 60)
            if s.rest
            else None,
            "last_compressor_stop": None
            if self.last_stop is None
            else f"{dt_util.as_local(self.last_stop[0]):%H:%M} {self.last_stop[1]}",
            "compressor_rest_until": None
            if self.rest_until is None
            else dt_util.as_local(self.rest_until).strftime("%H:%M"),
            "cloudy_override_enabled": s.cloudy_enabled and bool(s.weather_entity),
            "cloudy_override": self.cloudy_active,
            "cloudy": self.cloudy,
            "cloud_coverage_threshold": s.cloud_coverage,
            "rooms_calling_for_heat": self.calling_rooms,
            "early_start": s.early_start,
            "early_start_room": self.early_room,
            "cold_room_since": None
            if self._cold_since is None
            else dt_util.as_local(self._cold_since).strftime("%H:%M"),
            "rooms": [
                {
                    "entity": r["entity"],
                    "temperature": r["temperature"],
                    "target": r["target"],
                    "on": r["on"],
                    "calling": r.get("calling", False),
                }
                for r in self.rooms
            ],
            "paused_by_controller": self.paused_by_us,
            "warm_thermostats_off": s.warm_stats_off,
            "thermostats_off_by_controller": list(self.stats_off_by_us),
            "curve": f"{s.cold_out:g}°C→{s.cold_lwt:g}°C, {s.warm_out:g}°C→{s.warm_lwt:g}°C",
            "mild_temp": s.mild_temp,
            "heating_off_temp": s.off_temp,
            "mild_window": f"{s.window_start:%H:%M}–{s.window_end:%H:%M}"
            if s.window_enabled
            else "disabled",
        }
