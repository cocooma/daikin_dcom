"""Image entity: the smart heating curve drawn as SVG (no custom card needed).

When a room correction is active, the curve shifted by that correction is
drawn as a dashed line (what the unit actually gets). When the compressor rest
is on, its two-point rest curve is drawn dotted against a minutes scale on
the right. A legend row below the chart names the lines.

Show it with the built-in Picture entity card. The picture is redrawn when
the curve, thresholds, window or operating point change.

A second image draws the heat demand forecast: predicted against actual heat
per day for the last 14 days, today (so far) and tomorrow.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import DcomCoordinator
from .smart import LWT_FLOOR, curve_lwt

W, H = 640, 404
ML, MR, MT, MB = 48, 44, 16, 84  # margins (right: rest scale; bottom: axis title, 'now' row, legend row)
X_MIN, X_MAX = -15, 20

TEXT = "#9e9e9e"  # readable on light and dark themes
GRID = "rgba(158,158,158,0.25)"
CURVE = "#ff9800"
NOW = "#03a9f4"
MILD = "rgba(255,193,7,0.12)"
OFF = "rgba(158,158,158,0.18)"
REST = "#26a69a"  # compressor rest curve (right-hand scale)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DcomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DcomCurveImage(hass, coordinator), DcomForecastImage(hass, coordinator)])


def render_curve_svg(smart) -> str:
    s = smart.settings
    points = smart.curve_points
    offset = smart.room_offset
    corrected = (
        [
            [t, max(LWT_FLOOR, min(s.max_lwt, curve_lwt(s, t) + offset))]
            for t, _ in points
        ]
        if offset
        else []
    )
    lwts = [p[1] for p in points] + [p[1] for p in corrected]
    if smart.target_lwt is not None:
        lwts.append(smart.target_lwt)
    y_min = math.floor(min(lwts) / 2) * 2 - 2
    y_max = math.ceil(max(lwts) / 2) * 2 + 2

    pw, ph = W - ML - MR, H - MT - MB

    def x(t: float) -> float:
        t = min(max(t, X_MIN), X_MAX)
        return ML + (t - X_MIN) / (X_MAX - X_MIN) * pw

    def y(v: float) -> float:
        return MT + (y_max - v) / (y_max - y_min) * ph

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="sans-serif" font-size="12">'
    ]

    # zones: mild (window) and heating off
    if s.mild_temp < X_MAX:
        x0, x1 = x(s.mild_temp), x(min(s.off_temp, X_MAX))
        if s.window_enabled and x1 > x0:
            out.append(f'<rect x="{x0:.1f}" y="{MT}" width="{x1 - x0:.1f}" height="{ph}" fill="{MILD}"/>')
            out.append(
                f'<text x="{(x0 + x1) / 2:.1f}" y="{MT + 14}" fill="{TEXT}" text-anchor="middle">'
                f"{s.window_start:%H:%M}–{s.window_end:%H:%M}</text>"
            )
    if s.off_temp < X_MAX:
        x0 = x(s.off_temp)
        out.append(f'<rect x="{x0:.1f}" y="{MT}" width="{ML + pw - x0:.1f}" height="{ph}" fill="{OFF}"/>')
        out.append(
            f'<text x="{(x0 + ML + pw) / 2:.1f}" y="{MT + 14}" fill="{TEXT}" text-anchor="middle">off</text>'
        )

    # grid + axis labels
    for t in range(X_MIN, X_MAX + 1, 5):
        out.append(f'<line x1="{x(t):.1f}" y1="{MT}" x2="{x(t):.1f}" y2="{MT + ph}" stroke="{GRID}"/>')
        out.append(f'<text x="{x(t):.1f}" y="{MT + ph + 16}" fill="{TEXT}" text-anchor="middle">{t}</text>')
    for v in range(y_min, y_max + 1, 2):
        out.append(f'<line x1="{ML}" y1="{y(v):.1f}" x2="{ML + pw}" y2="{y(v):.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{ML - 6}" y="{y(v) + 4:.1f}" fill="{TEXT}" text-anchor="end">{v}</text>')
    out.append(
        f'<text x="{ML + pw / 2:.1f}" y="{MT + ph + 32}" fill="{TEXT}" text-anchor="middle">'
        "Outdoor temperature (°C)</text>"
    )
    out.append(
        f'<text x="12" y="{MT + ph / 2:.1f}" fill="{TEXT}" text-anchor="middle" '
        f'transform="rotate(-90 12 {MT + ph / 2:.1f})">Leaving water (°C)</text>'
    )

    # curve
    line = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in points)
    out.append(f'<polyline points="{line}" fill="none" stroke="{CURVE}" stroke-width="3"/>')
    legend: list[tuple[str, str, str]] = []  # (dasharray, width, text)
    if corrected:
        dashed = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in corrected)
        out.append(
            f'<polyline points="{dashed}" fill="none" stroke="{CURVE}" stroke-width="2" '
            'stroke-dasharray="7 5" opacity="0.85"/>'
        )
        legend.append(("7 5", "2", f"with room correction {offset:+d} °C"))

    # compressor rest curve on its own scale (minutes, right-hand axis)
    if s.rest:
        rest_pts = [
            (X_MIN, s.rest_cold_min if s.rest_cold_out >= X_MIN else s.rest_cold_min),
            (s.rest_cold_out, s.rest_cold_min),
            (s.rest_mild_out, s.rest_mild_min),
            (X_MAX, s.rest_mild_min),
        ]
        r_max = max(30, math.ceil((max(s.rest_cold_min, s.rest_mild_min) + 5) / 10) * 10)

        def yr(m: float) -> float:
            return MT + (r_max - m) / r_max * ph

        rest_line = " ".join(
            f"{x(t):.1f},{yr(m):.1f}"
            for t, m in sorted(rest_pts)
        )
        out.append(
            f'<polyline points="{rest_line}" fill="none" stroke="{REST}" stroke-width="2" '
            'stroke-dasharray="2 4" stroke-linecap="round"/>'
        )
        for m in range(0, r_max + 1, 10):
            out.append(
                f'<text x="{ML + pw + 6}" y="{yr(m) + 4:.1f}" fill="{REST}" text-anchor="start">{m}</text>'
            )
        out.append(
            f'<text x="{W - 6}" y="{MT + ph / 2:.1f}" fill="{REST}" text-anchor="middle" '
            f'transform="rotate(90 {W - 6} {MT + ph / 2:.1f})">Rest (min)</text>'
        )
        legend.append(("2 4", "2", "compressor rest (right scale)"))

    # legend row below the chart (only when there is more than the curve)
    if legend:
        items = [("", "3", "curve")] + legend
        lx, ly = ML, H - 10
        for dash, width, text in items:
            color = REST if text.startswith("compressor") else CURVE
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            out.append(
                f'<line x1="{lx}" y1="{ly - 4}" x2="{lx + 24}" y2="{ly - 4}" stroke="{color}" '
                f'stroke-width="{width}"{dash_attr} stroke-linecap="round"/>'
            )
            out.append(f'<text x="{lx + 30}" y="{ly}" fill="{TEXT}">{text}</text>')
            lx += 30 + 7 * len(text) + 24

    for t, v in ((s.cold_out, s.cold_lwt), (s.warm_out, s.warm_lwt)):
        if X_MIN <= t <= X_MAX:
            out.append(f'<circle cx="{x(t):.1f}" cy="{y(v):.1f}" r="4" fill="{CURVE}"/>')
            out.append(
                f'<text x="{x(t):.1f}" y="{y(v) - 9:.1f}" fill="{CURVE}" text-anchor="middle">'
                f"{t:g}° → {v:g}°</text>"
            )

    # operating point: averaged outdoor vs. target written to the unit.
    # The dot sits on the chart; its text gets its own row below the axis title,
    # because with several lines there is no reliably free spot inside the plot.
    if smart.outdoor_avg is not None and smart.target_lwt is not None:
        cx, cy = x(smart.outdoor_avg), y(smart.target_lwt)
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="{NOW}"/>')
        label = f"now {smart.outdoor_avg:.1f} °C outside → {smart.target_lwt} °C water"
        if smart.room_offset:
            label += f" (room {smart.room_offset:+d})"
        if s.rest:
            label += f" · rest {int(smart.rest_time.total_seconds() // 60)} min"
        ny = MT + ph + 52
        out.append(f'<circle cx="{ML + 6}" cy="{ny - 4}" r="5" fill="{NOW}"/>')
        out.append(f'<text x="{ML + 18}" y="{ny}" fill="{NOW}">{label}</text>')

    out.append("</svg>")
    return "".join(out)


class DcomCurveImage(ImageEntity):
    """The heating curve with the current operating point."""

    _attr_has_entity_name = True
    _attr_name = "Heating curve"
    _attr_content_type = "image/svg+xml"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, coordinator: DcomCoordinator) -> None:
        super().__init__(hass)
        self._smart = coordinator.smart
        self._attr_unique_id = f"{coordinator.entry.entry_id}_heating_curve"
        self._attr_device_info = coordinator.device_info
        self._svg = render_curve_svg(self._smart)
        self._attr_image_last_updated: datetime = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._smart.add_listener(self._refresh))

    @callback
    def _refresh(self) -> None:
        svg = render_curve_svg(self._smart)
        if svg != self._svg:
            self._svg = svg
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._svg.encode()


# --- heat demand forecast chart ------------------------------------------------------
FW, FH = 640, 340
FML, FMR, FMT, FMB = 48, 12, 30, 74  # bottom: day labels, legend row, summary row
UNUSABLE = "rgba(158,158,158,0.55)"


def _nice_step(top: float) -> float:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200):
        if top / step <= 6:
            return step
    return 500


def render_forecast_svg(fc) -> str:
    """Predicted (orange) vs actual (blue) heat per day; today so far and tomorrow."""
    today = dt_util.now().date()
    days: list[dict] = []
    for r in fc.records[-14:]:
        days.append({"d": date.fromisoformat(r["date"]), "pred": r.get("predicted"),
                     "act": r["heat"], "usable": r.get("usable", True), "kind": "past"})
    days.append({"d": today, "pred": fc.predicted_today if fc.predicted_today is not None else fc.today_kwh,
                 "act": round(fc.day["heat"]), "usable": True, "kind": "today"})
    days.append({"d": today + timedelta(days=1), "pred": fc.tomorrow_kwh, "act": None,
                 "usable": True, "kind": "tomorrow"})

    values = [v for d in days for v in (d["pred"], d["act"]) if v is not None]
    top = max(values + [1.0])
    step = _nice_step(top)
    y_max = math.ceil(top / step) * step
    pw, ph = FW - FML - FMR, FH - FMT - FMB
    slot = pw / len(days)
    bar = min(14.0, slot * 0.34)

    def y(v: float) -> float:
        return FMT + ph - v / y_max * ph

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {FW} {FH}" '
        f'font-family="sans-serif" font-size="12">',
        f'<text x="{FML}" y="18" fill="{TEXT}">Heat for space heating per day (kWh): predicted vs actual</text>',
    ]
    v = 0.0
    while v <= y_max + 1e-9:
        out.append(f'<line x1="{FML}" y1="{y(v):.1f}" x2="{FML + pw}" y2="{y(v):.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{FML - 6}" y="{y(v) + 4:.1f}" fill="{TEXT}" text-anchor="end">{v:g}</text>')
        v += step
    for i, d in enumerate(days):
        cx = FML + slot * (i + 0.5)
        if d["kind"] == "today":
            out.append(f'<rect x="{FML + slot * i:.1f}" y="{FMT}" width="{slot:.1f}" height="{ph}" fill="{MILD}"/>')
        if d["pred"] is not None:
            dash = ' stroke-dasharray="4 3"' if d["kind"] == "tomorrow" else ""
            fill = "none" if d["kind"] == "tomorrow" else CURVE
            out.append(
                f'<rect x="{cx - bar - 1:.1f}" y="{y(d["pred"]):.1f}" width="{bar:.1f}" '
                f'height="{FMT + ph - y(d["pred"]):.1f}" fill="{fill}" fill-opacity="0.85" '
                f'stroke="{CURVE}"{dash}/>'
            )
        if d["act"] is not None:
            color = NOW if d["usable"] else UNUSABLE
            opacity = "0.5" if d["kind"] == "today" else "0.9"
            out.append(
                f'<rect x="{cx + 1:.1f}" y="{y(d["act"]):.1f}" width="{bar:.1f}" '
                f'height="{FMT + ph - y(d["act"]):.1f}" fill="{color}" fill-opacity="{opacity}"/>'
            )
        if d["kind"] == "past" and d["pred"] and d["act"] is not None and d["usable"] and slot >= 30:
            err = 100 * (d["act"] - d["pred"]) / d["pred"]
            out.append(
                f'<text x="{cx:.1f}" y="{min(y(d["pred"]), y(d["act"])) - 4:.1f}" fill="{TEXT}" '
                f'font-size="10" text-anchor="middle">{err:+.0f}%</text>'
            )
        label = "today" if d["kind"] == "today" else "tmrw" if d["kind"] == "tomorrow" else f'{d["d"].day}'
        out.append(f'<text x="{cx:.1f}" y="{FMT + ph + 15}" fill="{TEXT}" text-anchor="middle" '
                   f'font-size="11">{label}</text>')

    ly = FMT + ph + 38
    legend = [(CURVE, "predicted (day before)", False), (NOW, "actual", False),
              (UNUSABLE, "not learnt from", False), (CURVE, "tomorrow", True)]
    lx = FML
    for color, text, dashed in legend:
        if dashed:
            out.append(f'<rect x="{lx}" y="{ly - 10}" width="12" height="10" fill="none" stroke="{color}" '
                       f'stroke-dasharray="3 2"/>')
        else:
            out.append(f'<rect x="{lx}" y="{ly - 10}" width="12" height="10" fill="{color}"/>')
        out.append(f'<text x="{lx + 17}" y="{ly}" fill="{TEXT}">{text}</text>')
        lx += 17 + 7 * len(text) + 18

    acc = fc.accuracy
    parts = []
    if acc.get("mean_error_pct") is not None:
        parts.append(f'mean error {acc["mean_error_pct"]:.0f} % ({acc["days_compared"]} days)')
    if fc.today_kwh is not None:
        parts.append(f'today {fc.today_kwh:.0f} kWh ({fc.day["heat"]:.0f} so far)')
    if fc.tomorrow_kwh is not None:
        parts.append(f"tomorrow {fc.tomorrow_kwh:.0f} kWh")
    if not parts or fc.model is None:
        parts.insert(0, fc.status)
    out.append(f'<text x="{FML}" y="{ly + 22}" fill="{NOW}">{" · ".join(parts)}</text>')
    out.append("</svg>")
    return "".join(out)


class DcomForecastImage(ImageEntity):
    """Heat demand: predicted vs actual per day (last 14 days, today, tomorrow)."""

    _attr_has_entity_name = True
    _attr_name = "Heat demand forecast chart"
    _attr_content_type = "image/svg+xml"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, coordinator: DcomCoordinator) -> None:
        super().__init__(hass)
        self._fc = coordinator.forecast
        self._attr_unique_id = f"{coordinator.entry.entry_id}_heat_forecast_chart"
        self._attr_device_info = coordinator.device_info
        self._svg = render_forecast_svg(self._fc)
        self._attr_image_last_updated: datetime = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._fc.add_listener(self._refresh))

    @callback
    def _refresh(self) -> None:
        svg = render_forecast_svg(self._fc)  # rounded to whole kWh: redrawn now and then, not every poll
        if svg != self._svg:
            self._svg = svg
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._svg.encode()
