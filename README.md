# Daikin Altherma 3 – DCOM-LT/MB (Modbus TCP) for Home Assistant

Custom integration for a Daikin Altherma 3 heat pump with a Voytech **DCOM-LT/MB** module, reached through
a Modbus TCP gateway. It exposes the DCOM's registers as Home Assistant entities and can optionally run its
own heating curve with mild-day scheduling (**smart heating control**).

Requires Home Assistant 2024.4 or newer and `pymodbus` 3.6.9 or newer (installed automatically).

## Install

**HACS:** HACS → ⋮ → *Custom repositories* → add `https://github.com/cocooma/daikin_dcom` as an
*Integration*, install *Daikin Altherma 3 (DCOM-LT/MB Modbus TCP)* and restart Home Assistant.

**Manual:** copy `custom_components/daikin_dcom` into your `config/custom_components/` and restart.

Then: Settings → Devices & services → Add integration → *Daikin Altherma 3 (DCOM-LT/MB Modbus TCP)*.

## Setup

| Field | Meaning |
|---|---|
| Host / Port | Modbus TCP gateway (default port 502) |
| Modbus address | Set with DIP switches SW1.3–SW1.8 on the DCOM (1–63, see manual p.3) |
| Framing | `Modbus TCP` for gateways that convert to Modbus RTU; `RTU over TCP` for transparent serial-to-Ethernet bridges (RTU frames inside TCP) |
| Temperature control | `Leaving water temperature` uses registers 1/2 (heating 25–55 °C, cooling 5–22 °C). `Room thermostat` uses registers 6/7 (heating 12–30 °C, cooling 15–35 °C) |

The DCOM's RS485 side is fixed at 9600 baud, 8N1.

**Options** (Configure on the integration): temperature control, polling interval (10–600 s, default 30 s) and
the smart heating thresholds and room sensor described below. Saving the options reloads the integration.

## Entities

### Climate – Space heating/cooling

One entity for both heating and cooling.

| HVAC mode | Registers written |
|---|---|
| off | 4 = 0 |
| heat | 3 = 1, 4 = 1 |
| cool | 3 = 2, 4 = 1 |
| auto | 3 = 0, 4 = 1 |

The target temperature changes the heating or the cooling setpoint, depending on the active mode (in auto,
the mode the unit is running in, input 38). Current temperature is the LWT (input 40) or the Remocon room
temperature (input 50), depending on the temperature control setting.

With *Leaving water temperature* control and weather dependent mode active (reg 53 = 1 or 3), the unit follows
its own curve and the DCOM has no register for the calculated setpoint. The target temperature then shows and
sets the weather dependent offset instead (reg 54 heating / 55 cooling, −10 to +10 °C); the attribute
`weather_dependent: true` indicates this.

### Climate – Domestic hot water

off / heat = DHW reheat (reg 12), target = DHW reheat setpoint 30–60 °C (reg 10), current = DHW temperature
(input 43), preset `boost` = DHW booster mode (reg 13).

### Other entities

| Type | Entities |
|---|---|
| Sensor | Leaving water (pre PHE, pre BUH), return water, DHW, outside air, liquid refrigerant and Remocon room temperatures; flow rate (l/min); unit error code and sub code |
| Binary sensor | Unit error, circulation pump, compressor, booster heater, disinfection, defrost/startup, hot start, 3-way valve (on = DHW) |
| Switch | Quiet mode (reg 9) |
| Select | Weather dependent mode (reg 53): fixed, weather dependent, fixed + scheduled, weather dependent + scheduled |
| Number | Weather dependent heating / cooling LWT offset (regs 54 / 55, −10 to +10 °C) |
| Smart heating | See the next section: control switch, heating curve points, mild-day window switch and times, target LWT and status sensors, heating curve image |

## Smart heating control (optional)

Runs a heating curve and mild-day scheduling inside Home Assistant instead of the unit's two-point weather
curve. It is aimed at oversized units on underfloor heating, where the compressor's minimum output is more
than the house needs on mild days: rather than short cycling all day, the unit heats in one long block during
the warmest hours and the floor slab carries the heat.

Turn it on with the **Smart heating control** switch. While it is on and the unit is heating, the controller:

1. sets the setpoint mode to *fixed* (reg 53 = 0); the previous mode is restored when you switch it off,
2. writes the leaving water setpoint (reg 1) from its own curve, using the outdoor temperature (input 44)
   averaged over about an hour, plus an optional room correction,
3. pauses space heating (reg 4 = 0) on mild days outside the time window and on warm days, and resumes it.

### Settings (integration options)

| Option | Default | Meaning |
|---|---|---|
| Maximum LWT | 40 °C | Safety cap |
| Mild day above | 12 °C | Above this, heat only inside the mild-day window |
| No space heating above | 16 °C | Heating paused |
| Room sensor / target / max correction | – / 22 °C / 3 °C | Optional: ±1 °C LWT step every 45 min while the room is 0.5 °C or more off target |

### Heating curve (dashboard)

The two curve points are number entities, so the curve can be tuned from the dashboard. Changes apply
immediately (the new setpoint is written straight away while smart heating is on), are redrawn in the
**Heating curve** image and are kept across restarts.

| Entity | Default | Range | Meaning |
|---|---|---|---|
| Heating curve cold point outdoor | −5 °C | −25 to 10 °C | Outdoor temperature of the cold point; the LWT stays flat below it |
| Heating curve cold point LWT | 34 °C | 25 to 55 °C | Leaving water at the cold point |
| Heating curve warm point outdoor | 15 °C | 0 to 25 °C | Outdoor temperature of the warm point; the LWT stays flat above it |
| Heating curve warm point LWT | 26 °C | 25 to 55 °C | Leaving water at the warm point (reg 1 minimum is 25 °C) |

The warm point's outdoor temperature must stay above the cold point's; other values are refused.
Tuning: if rooms are too cold everywhere, raise both LWT points by 1 °C; if only on cold days, raise the
cold point LWT; if only on mild days, raise the warm point LWT. Wait a day between changes with underfloor
heating.

### Mild-day window (dashboard)

Set from the dashboard rather than the options; changes apply immediately and are kept across restarts.

| Entity | Default | Meaning |
|---|---|---|
| Smart heating window start / end (time) | 11:00 / 17:00 | On mild days heating runs only in this window. Pick the warmest hours for the best COP. May cross midnight; start = end means all day |
| Smart heating mild-day window (switch) | on | Off: no time restriction on mild days; only the "no space heating above" limit applies |

### Status entities

| Entity | Meaning |
|---|---|
| Smart heating control (switch) | Enables the controller; its state is restored after a restart. Attribute `status` |
| Smart heating target LWT (sensor) | Setpoint the controller writes to reg 1. Attributes: averaged outdoor temperature, curve value, room correction, thresholds, window and `curve_points` |
| Smart heating status (sensor) | `heating`, `paused_mild_outside_window`, `paused_warm`, `space_heating_off` (switched off by you), `cooling_mode`, `defrost`, `waiting_for_data` or `disabled` |
| Heating curve (image) | The curve drawn as a picture; see below |

### Behaviour and limits

* Thresholds use 0.5 °C hysteresis. The controller keeps heating on for at least 30 min and off for at least
  20 min, and writes the setpoint at most every 10 min (immediately if it changes by 2 °C or more).
* It only resumes heating it paused itself: if you switch space heating off, it stays off.
* It does nothing in cooling mode or during defrost.
* While it is on, setpoint and weather mode changes made elsewhere (climate entity, select, Daikin user
  interface) are overwritten. Switch it off to take manual control.
* Overshoot ([9-04]) and the unit's own curve ([1-00]–[1-03]) are field settings with no DCOM register, so
  they cannot be changed from here. Set them on the unit's user interface.

### Heating curve on a dashboard

The **Heating curve** image entity is an SVG drawn by the integration: the curve with its two points, the
mild-day window and "off" zones, and the current operating point (averaged outdoor temperature → setpoint
written to the unit, with any room correction). While a room correction is active, the curve shifted by that
correction is drawn as a dashed line with a small legend: that is what the unit actually gets at each outdoor
temperature. It is redrawn whenever one of these changes and works with
light and dark themes. Use the built-in Picture entity card:

```yaml
type: picture-entity
entity: image.daikin_altherma_3_heating_curve
show_name: false
show_state: false
```

To plot the curve with another card, use the `curve_points` attribute of **Smart heating target LWT**
(`[outdoor, LWT]` pairs from −15 to +20 °C; not stored in the recorder).

## Notes

* Register numbers are used as Modbus addresses directly (manual: "Register Base 0"). If every value looks
  shifted by one, your gateway adds a +1 offset; please open an issue.
* The DCOM returns 32765 / 32766 / 32767 for "wait for value", "unavailable" and "unsupported"; these are
  shown as *unavailable*. Right after power-up the DCOM can need up to about 8 minutes to synchronise with the
  Altherma master (status LED).
* Only documented, contiguous register blocks are read, so no undocumented addresses are requested.
* Setpoints written outside the unit's configured range are clamped by the DCOM; other out-of-range writes
  are ignored.
* Written values are shown straight away and confirmed on the next poll.
* The DCOM has no power or energy registers, so COP cannot be calculated from it alone; use a separate
  energy meter for that.

## Testing status

Built from the DCOM-LT/MB reference manual (534-252-A3). The smart heating logic and the curve drawing have
been checked with simulated data; please report anything that behaves differently on your unit.
