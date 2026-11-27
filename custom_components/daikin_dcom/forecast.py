"""Heat demand forecast: learns how much space heating the house needs per day.

Each day the integration records the heat the heat pump delivered for space
heating (from the COP tracker's thermal power) together with what drove it:

* degree-hours  dh  = sum over the day of max(0, room - outdoor) x hours
* wind degree-hours = sum of wind speed (m/s) x max(0, room - outdoor) x hours,
  in total and split into four wind-direction sectors (N, E, S, W)
* solar          = the day's solar forecast in kWh (e.g. Solcast), optional
* hours          = hours covered (acts as the daily constant: internal gains,
  standing losses)

and fits, by least squares (lightly regularised),

    heat_kWh = a*dh + b*wind_dh (or b_N..b_W per sector) - c*solar + d*hours

The model grows with the data: temperature (+ solar) from 7 days, + wind from
14 days, + wind direction from 28 days, and falls back to a simpler form when a
coefficient comes out physically impossible (a <= 0, more sun = more heat).

The forecast feeds the same features from the weather entity's hourly forecast
(and the solar forecast for tomorrow). Outdoor temperature and wind for the
history come from the same weather entity, so history and forecast match.

Nothing here controls the heat pump; it only predicts.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FORECAST_SOLAR_TODAY,
    CONF_FORECAST_SOLAR_TOMORROW,
    CONF_FORECAST_WEATHER,
    CONF_SMART_WEATHER_ENTITY,
    DOMAIN,
    H_SPACE_ONOFF,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_RECORDS = 180  # days kept
MAX_GAP = timedelta(minutes=5)  # longer gaps between samples are not counted
REFRESH = timedelta(minutes=30)  # forecast refresh
MIN_COVERAGE_H = 20.0  # a day needs this many measured hours to be learnt from
MAX_USER_OFF_H = 2.0  # ... and space heating not switched off by hand for longer
MIN_DAYS = 7  # model: temperature (+ solar)
WIND_DAYS = 14  # + wind
SECTOR_DAYS = 28  # + wind direction
SECTORS = ("n", "e", "s", "w")
ERROR_DAYS = 14  # mean error over this many days

_WIND_TO_MS = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444, "ft/s": 0.3048}
_COMPASS = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135,
    "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270,
    "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


# --- pure helpers (no Home Assistant) ---------------------------------------
def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def wind_ms(speed: Any, unit: str | None) -> float | None:
    s = _float(speed)
    if s is None:
        return None
    return max(0.0, s * _WIND_TO_MS.get(unit or "km/h", 1 / 3.6))


def bearing_deg(value: Any) -> float | None:
    if isinstance(value, str) and value.strip().upper() in _COMPASS:
        return _COMPASS[value.strip().upper()]
    b = _float(value)
    return None if b is None else b % 360


def sector_weights(bearing: float | None) -> dict[str, float]:
    """Wind from this bearing, shared between the two nearest sectors."""
    if bearing is None:
        return {s: 0.25 for s in SECTORS}
    pos = bearing / 90.0  # 0 = N, 1 = E, ...
    lo = int(math.floor(pos)) % 4
    frac = pos - math.floor(pos)
    w = {s: 0.0 for s in SECTORS}
    w[SECTORS[lo]] += 1 - frac
    w[SECTORS[(lo + 1) % 4]] += frac
    return w


def empty_features() -> dict[str, float]:
    f = {"dh": 0.0, "wdh": 0.0, "hours": 0.0}
    f.update({f"wdh_{s}": 0.0 for s in SECTORS})
    return f


def add_sample(f: dict[str, float], hours: float, room: float, outdoor: float,
               wind: float | None, bearing: float | None) -> None:
    """Add `hours` of weather to the feature sums `f` (in place)."""
    dt = max(0.0, room - outdoor)
    f["dh"] += dt * hours
    f["hours"] += hours
    if wind is not None:
        f["wdh"] += wind * dt * hours
        for s, w in sector_weights(bearing).items():
            f[f"wdh_{s}"] += w * wind * dt * hours


def _columns(tier: int, use_solar: bool) -> list[str]:
    cols = ["dh"]
    if tier == 2:
        cols.append("wdh")
    elif tier == 3:
        cols += [f"wdh_{s}" for s in SECTORS]
    if use_solar:
        cols.append("solar")
    cols.append("hours")
    return cols


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                k = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= k * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fit the largest model the data supports; None until MIN_DAYS usable days."""
    rows = [r for r in records if r.get("usable")]
    if len(rows) < MIN_DAYS:
        return None
    use_solar = all(r.get("solar") is not None for r in rows)
    tiers = [t for t, days in ((3, SECTOR_DAYS), (2, WIND_DAYS), (1, MIN_DAYS)) if len(rows) >= days]
    for tier in tiers:
        for solar in ([True, False] if use_solar else [False]):
            cols = _columns(tier, solar)
            x = [[float(r[c] if c != "solar" else r["solar"]) for c in cols] for r in rows]
            y = [float(r["heat"]) for r in rows]
            # scale columns, light ridge so short/collinear histories stay sane
            scale = [max(1e-9, math.sqrt(sum(v[j] ** 2 for v in x) / len(x))) for j in range(len(cols))]
            xs = [[v[j] / scale[j] for j in range(len(cols))] for v in x]
            lam = 1e-3 * len(rows)
            ata = [[sum(v[i] * v[j] for v in xs) + (lam if i == j else 0.0)
                    for j in range(len(cols))] for i in range(len(cols))]
            aty = [sum(v[i] * y[k] for k, v in enumerate(xs)) for i in range(len(cols))]
            sol = _solve(ata, aty)
            if sol is None:
                continue
            coef = {c: sol[j] / scale[j] for j, c in enumerate(cols)}
            if coef["dh"] <= 0:
                continue  # colder must mean more heat
            if solar and coef["solar"] > 0:
                continue  # more sun must not mean more heat
            if tier == 2 and coef["wdh"] < 0:
                continue
            pred = [predict(coef, r, r.get("solar")) for r in rows]
            mae = sum(abs(p - t) for p, t in zip(pred, y)) / len(y)
            return {"tier": tier, "coef": coef, "days": len(rows),
                    "fit_mae": round(mae, 2), "mean_heat": round(sum(y) / len(y), 2)}
    return None


def predict(coef: dict[str, float], f: dict[str, float], solar: float | None) -> float:
    total = 0.0
    for c, k in coef.items():
        if c == "solar":
            total += k * (solar or 0.0)
        else:
            total += k * f.get(c, 0.0)
    return max(0.0, total)


# --- Home Assistant side ----------------------------------------------------
class HeatForecaster:
    """Collects daily records, fits the model and forecasts today and tomorrow."""

    def __init__(self, hass: HomeAssistant, entry, coordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        o = entry.options
        self.weather: str = o.get(CONF_FORECAST_WEATHER) or o.get(CONF_SMART_WEATHER_ENTITY) or ""
        self.solar_today: str = o.get(CONF_FORECAST_SOLAR_TODAY) or ""
        self.solar_tomorrow: str = o.get(CONF_FORECAST_SOLAR_TOMORROW) or ""
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.heat_forecast")
        self.records: list[dict[str, Any]] = []
        self.day: dict[str, Any] = self._new_day(dt_util.now().date())
        self.predicted: dict[str, float] = {}  # date -> forecast made the day before
        self.model: dict[str, Any] | None = None
        self.today_kwh: float | None = None
        self.tomorrow_kwh: float | None = None
        self.tomorrow_inputs: dict[str, Any] = {}
        self.today_inputs: dict[str, Any] = {}
        self.hourly: list[dict[str, Any]] = []  # last hourly weather forecast
        self._last_time: datetime | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    # --- lifecycle -----------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.records = list(data.get("records", []))[-MAX_RECORDS:]
        self.predicted = {str(k): float(v) for k, v in (data.get("predicted") or {}).items()}
        day = data.get("day")
        if day and day.get("date") == dt_util.now().date().isoformat():
            self.day = day
        elif day:
            self._close_day(day)  # finished while Home Assistant was down
        self.model = fit(self.records)

    def start(self) -> None:
        self._unsubs.append(self.coordinator.cop.add_listener(self._on_update))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_timer, REFRESH))
        self.hass.async_create_task(self._async_refresh())

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._save(now=True)

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _save(self, now: bool = False) -> None:
        data = lambda: {  # noqa: E731
            "records": self.records[-MAX_RECORDS:],
            "day": self.day,
            "predicted": dict(list(self.predicted.items())[-60:]),
        }
        if now:
            self.hass.async_create_task(self._store.async_save(data()))
        else:
            self._store.async_delay_save(data, 120)

    # --- inputs ----------------------------------------------------------------
    @staticmethod
    def _new_day(d: date) -> dict[str, Any]:
        return {"date": d.isoformat(), "heat": 0.0, "user_off_h": 0.0, "cooling_h": 0.0,
                "solar": None, **empty_features()}

    def _room_temperature(self, target: bool = False) -> float:
        """Rooms that count (thermostats on, sensors): actual mean, or target mean."""
        smart = self.coordinator.smart
        key = "target" if target else "temperature"
        vals = [r[key] for r in getattr(smart, "rooms", []) if r.get("available") and r.get("on")
                and r.get(key) is not None]
        if vals:
            return sum(vals) / len(vals)
        return float(smart.settings.room_target)

    def _weather_now(self) -> tuple[float | None, float | None, float | None]:
        state = self.hass.states.get(self.weather) if self.weather else None
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None, None, None
        a = state.attributes
        t = _float(a.get("temperature"))
        if t is not None and a.get("temperature_unit") == "°F":
            t = (t - 32) / 1.8
        return t, wind_ms(a.get("wind_speed"), a.get("wind_speed_unit")), bearing_deg(a.get("wind_bearing"))

    def _sensor_kwh(self, entity_id: str) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        v = _float(state.state)
        if v is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        return v / 1000 if unit == "Wh" else v * 1000 if unit == "MWh" else v

    # --- daily accumulation ----------------------------------------------------
    @callback
    def _on_update(self) -> None:
        now = dt_util.utcnow()
        today = dt_util.now().date()
        if self.day["date"] != today.isoformat():
            self._close_day(self.day)
            self.day = self._new_day(today)
            self.model = fit(self.records)
            self.hass.async_create_task(self._async_refresh())
        last, self._last_time = self._last_time, now
        if last is None or now <= last or now - last > MAX_GAP:
            return
        hours = (now - last).total_seconds() / 3600
        cop = self.coordinator.cop
        day = self.day
        if cop.mode == "heating" and cop.thermal_kw is not None:
            day["heat"] += cop.thermal_kw * hours
        if cop.mode == "cooling":
            day["cooling_h"] += hours
        if self.coordinator.value("h", H_SPACE_ONOFF) == 0 and not self.coordinator.smart.paused_by_us:
            day["user_off_h"] += hours
        outdoor, wind, bearing = self._weather_now()
        if outdoor is None:
            self._notify()
            return
        add_sample(day, hours, self._room_temperature(), outdoor, wind, bearing)
        solar = self._sensor_kwh(self.solar_today)
        if solar is not None:
            day["solar"] = solar  # the day's estimate; the last value of the day is kept
        self._save()
        self._notify()

    def _close_day(self, day: dict[str, Any]) -> None:
        rec = dict(day)
        rec["heat"] = round(rec["heat"], 2)
        rec["usable"] = (rec["hours"] >= MIN_COVERAGE_H and rec["user_off_h"] <= MAX_USER_OFF_H
                         and rec["cooling_h"] <= 1.0)
        if rec["date"] in self.predicted:
            rec["predicted"] = round(self.predicted[rec["date"]], 2)
        self.records = [r for r in self.records if r["date"] != rec["date"]] + [rec]
        self.records = self.records[-MAX_RECORDS:]
        self._save(now=True)

    # --- forecast --------------------------------------------------------------
    @callback
    def _on_timer(self, _now: datetime) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_hourly_forecast(self) -> list[dict[str, Any]]:
        if not self.weather:
            return []
        for kind in ("hourly", "daily"):
            try:
                resp = await self.hass.services.async_call(
                    "weather", "get_forecasts", {"entity_id": self.weather, "type": kind},
                    blocking=True, return_response=True,
                )
            except Exception as err:  # noqa: BLE001 - provider may not offer this type
                _LOGGER.debug("Heat forecast: %s forecast from %s failed: %s", kind, self.weather, err)
                continue
            items = ((resp or {}).get(self.weather) or {}).get("forecast") or []
            if items:
                return self._to_hours(items, kind)
        return []

    def _to_hours(self, items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        state = self.hass.states.get(self.weather)
        attrs = state.attributes if state else {}
        unit = attrs.get("wind_speed_unit")
        fahrenheit = attrs.get("temperature_unit") == "°F"
        hours = []
        for it in items:
            start = dt_util.parse_datetime(str(it.get("datetime"))) if it.get("datetime") else None
            t = _float(it.get("temperature"))
            if start is None or t is None:
                continue
            if kind == "daily" and _float(it.get("templow")) is not None:
                t = (t + float(it["templow"])) / 2
            if fahrenheit:
                t = (t - 32) / 1.8
            w, b = wind_ms(it.get("wind_speed"), unit), bearing_deg(it.get("wind_bearing"))
            span = 24 if kind == "daily" else 1
            for h in range(span):
                hours.append({"start": dt_util.as_local(start) + timedelta(hours=h), "t": t, "w": w, "b": b})
        return hours

    async def _async_refresh(self) -> None:
        self.model = self.model or fit(self.records)
        hours = await self._async_hourly_forecast()
        if hours:
            self.hourly = hours
        now = dt_util.now()
        today, tomorrow = now.date(), now.date() + timedelta(days=1)
        target = self._room_temperature(target=True)

        rest = empty_features()
        nxt = empty_features()
        for h in hours:
            d = h["start"].date()
            if d == today and h["start"] >= now - timedelta(minutes=30):
                add_sample(rest, 1.0, target, h["t"], h["w"], h["b"])
            elif d == tomorrow:
                add_sample(nxt, 1.0, target, h["t"], h["w"], h["b"])

        # today = measured so far + forecast for the rest of the day
        full = empty_features()
        for k in full:
            full[k] = self.day.get(k, 0.0) + rest[k]
        missing = max(0.0, 24.0 - full["hours"])  # hours neither measured nor forecast
        full["hours"] += missing
        solar_today = self._sensor_kwh(self.solar_today)
        solar_tomorrow = self._sensor_kwh(self.solar_tomorrow)
        self.today_inputs = self._describe(full, solar_today)
        self.tomorrow_inputs = self._describe(nxt, solar_tomorrow)

        if self.model and full["hours"] > 0:
            self.today_kwh = round(max(predict(self.model["coef"], full, solar_today), self.day["heat"]), 1)
        else:
            self.today_kwh = None
        if self.model and nxt["hours"] >= 20:
            self.tomorrow_kwh = round(predict(self.model["coef"], nxt, solar_tomorrow), 1)
            self.predicted[tomorrow.isoformat()] = self.tomorrow_kwh
        else:
            self.tomorrow_kwh = None
        self._save()
        self._notify()

    def need_kwh(self, start: datetime, end: datetime) -> float | None:
        """Predicted space heating need between two (local) times, e.g. the night.

        None until the model has learnt or when the forecast doesn't cover most of
        the period. No sun is assumed (meant for the night).
        """
        if not self.model or end <= start:
            return None
        f = empty_features()
        target = self._room_temperature(target=True)
        for h in self.hourly:
            if start <= h["start"] < end:
                add_sample(f, 1.0, target, h["t"], h["w"], h["b"])
        if f["hours"] < 0.8 * (end - start).total_seconds() / 3600:
            return None
        return predict(self.model["coef"], f, 0.0)

    @staticmethod
    def _describe(f: dict[str, float], solar: float | None) -> dict[str, Any]:
        hours = f["hours"] or 1
        return {
            "degree_hours": round(f["dh"], 1),
            "mean_temperature_difference": round(f["dh"] / hours, 1),
            "mean_wind_ms": round(f["wdh"] / f["dh"], 1) if f["dh"] > 0 else None,
            "solar_kwh": None if solar is None else round(solar, 1),
            "hours": round(f["hours"], 1),
        }

    # --- reporting -------------------------------------------------------------
    @property
    def status(self) -> str:
        usable = sum(1 for r in self.records if r.get("usable"))
        if not self.weather:
            return "no weather entity configured"
        if self.model is None:
            return f"learning: {usable} of {MIN_DAYS} days"
        return {1: "temperature", 2: "temperature + wind", 3: "temperature + wind direction"}[
            self.model["tier"]] + (" + sun" if "solar" in self.model["coef"] else "")

    @property
    def accuracy(self) -> dict[str, Any]:
        recent = [r for r in self.records if r.get("predicted") is not None and r.get("usable")][-ERROR_DAYS:]
        if not recent:
            return {}
        err = [abs(r["predicted"] - r["heat"]) for r in recent]
        heat = sum(r["heat"] for r in recent)
        return {
            "days_compared": len(recent),
            "mean_error_kwh": round(sum(err) / len(err), 1),
            "mean_error_pct": round(100 * sum(err) / heat, 0) if heat > 0 else None,
        }

    @property
    def model_summary(self) -> dict[str, Any]:
        if not self.model:
            return {}
        c = self.model["coef"]
        out: dict[str, Any] = {
            "kwh_per_degree_day": round(24 * c["dh"], 2),
            "kwh_per_day_base": round(24 * c["hours"], 1),
        }
        if "wdh" in c:
            out["extra_per_m_s_wind_pct"] = round(100 * c["wdh"] / c["dh"], 1)
        for s in SECTORS:
            if f"wdh_{s}" in c:
                out[f"extra_per_m_s_wind_from_{s}_pct"] = round(100 * c[f"wdh_{s}"] / c["dh"], 1)
        if "solar" in c:
            out["kwh_saved_per_solar_kwh"] = round(-c["solar"], 2)
        out["training_days"] = self.model["days"]
        out["fit_error_kwh"] = self.model["fit_mae"]
        return out

    @property
    def predicted_today(self) -> float | None:
        """The forecast for today that was made yesterday (fixed for the day)."""
        return self.predicted.get(dt_util.now().date().isoformat())

    @property
    def last_error(self) -> dict[str, Any] | None:
        """Yesterday (or the last compared day): actual vs the day-before forecast."""
        for r in reversed(self.records):
            if r.get("predicted") is not None and r.get("usable"):
                p = r["predicted"]
                return {
                    "date": r["date"], "actual_kwh": r["heat"], "predicted_kwh": p,
                    "error_kwh": round(r["heat"] - p, 1),
                    "error_pct": round(100 * (r["heat"] - p) / p) if p > 0 else None,
                }
        return None

    @property
    def history(self) -> list[dict[str, Any]]:
        return [
            {"date": r["date"], "actual_kwh": r["heat"], "predicted_kwh": r.get("predicted"),
             "usable": r.get("usable")}
            for r in self.records[-7:]
        ]
