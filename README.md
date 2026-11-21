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
the smart heating thresholds and room correction settings described below. Saving the options reloads the integration.

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
| Rooms / room target / room mode / pause when all thermostats off / max correction | – / 22 °C / coldest / on / 3 °C | Optional room correction, see [Room correction](#room-correction-optional) |

### Room correction (optional)

The curve sets the leaving water from the outdoor temperature only. Room correction adds feedback from real
room temperatures, so the controller can make up for sun, wind, internal gains or a curve that is slightly off.
It shifts the leaving water setpoint in whole degrees on top of the curve.

You can select **one or more** rooms, as plain temperature sensors, room thermostats (climate entities, e.g.
the Daikin EKRTCTRL1 / EKWHCTRL1 wall controls) or a mix of both.

#### Setting it up

1. Pick the rooms. Good choices represent the house, e.g. the living room and bedrooms. Avoid rooms with a
   fireplace or strong sun and sensors near windows, doors or other heat sources.
2. Settings → Devices & services → *Daikin Altherma 3 (DCOM-LT/MB)* → **Configure**.
3. Fill in:

   | Option | Range / values | Meaning |
   |---|---|---|
   | Room temperature sensors and/or thermostats | – | Any number of `sensor` entities with the *temperature* device class (°C) and/or `climate` entities. Empty = room correction off |
   | Room target | 15–28 °C (default 22) | Target for **plain sensors**. Thermostats use their own target temperature |
   | Room that drives the correction | coldest (default), average, warmest | How the rooms are combined, see below |
   | Pause heating when all selected thermostats are off | on (default) / off | See [Thermostats switched off](#thermostats-switched-off) |
   | Maximum room correction | 0–5 °C (default 3) | How far the leaving water may be shifted up or down. 0 = room correction off |

4. Save. The integration reloads, and the correction runs whenever **Smart heating control** is on.

#### How rooms are read

| Entity | Temperature | Target | Counted when |
|---|---|---|---|
| `sensor.*` | its state | *Room target* option | always (if it has a number) |
| `climate.*` | `current_temperature` attribute | its `temperature` attribute (or `target_temp_low`; else the *Room target* option) | its mode is `heat`, `auto` or `heat_cool` |

For each counted room the controller works out the **shortfall** = target − temperature (positive = too cold).
The rooms are then combined according to *Room that drives the correction*:

* **coldest** – the room with the largest shortfall decides. Every room gets to its target; rooms that warm up
  faster may run a little above theirs. Best when all rooms must be comfortable.
* **average** – the average shortfall of all counted rooms decides. Smoother, but one cold room can stay below
  its target if the others are warm.
* **warmest** – the room with the smallest shortfall decides. Avoids overheating any room; colder rooms may
  stay below target.

Unavailable entities, switched-off thermostats and rooms without a reading are left out. If no room is left, the
correction holds its value.

#### How the correction moves

* On each evaluation (every poll and once a minute) the combined shortfall is checked:
  * 0.5 °C or more **too cold** → correction **+1 °C**,
  * 0.5 °C or more **too warm** → correction **−1 °C**,
  * within ±0.5 °C → the correction stays where it is (it does not drift back to 0 by itself).
* After a step it waits at least **45 minutes** before the next one, because underfloor heating reacts slowly.
  Steps accumulate up to the maximum: with a 3 °C maximum, a cold room goes +1 → +2 → +3 over about 1.5 hours.
* The setpoint written to the unit is `curve value + correction`, rounded to whole degrees and then limited to
  25 °C (register minimum) … *maximum LWT*. Near the warm end of the curve a negative correction can therefore
  have no effect.
* The correction keeps being evaluated while the controller has paused heating (mild-day window, warm
  cut-off). After a long pause in a cooling room it may therefore resume one or two degrees higher. It is not
  evaluated in cooling mode, during defrost or while space heating is switched off by you.
* The correction is kept across restarts and reset to 0 when smart heating control is switched off.

Example, mode *coldest*, three rooms:

| Room | Temperature | Target | Shortfall |
|---|---|---|---|
| Living room (sensor) | 23.2 °C | 24 °C (option) | +0.8 |
| Bedroom (sensor) | 24.4 °C | 24 °C (option) | −0.4 |
| Bathroom (thermostat, heat) | 23.0 °C | 25 °C (its own) | **+2.0** ← drives |

The bathroom is 2 °C short, so the correction steps up. With *average* the shortfall would be +0.8 (still a
step up); with *warmest* it would be −0.4 (no change).

#### Thermostats switched off

A thermostat in mode `off` (or `cool`) is **left out** of the correction: a room you switched off doesn't ask
for heat.

With *Pause heating when all selected thermostats are off* enabled, space heating is **paused** (reg 4 = 0,
status `paused_rooms_off`) while **every** selected thermostat that can be read is off, and resumes as soon as
one is switched back on. The usual minimum run (30 min) and pause (20 min) times apply. Notes:

* Only thermostats count for this rule. Plain sensors have no on/off and don't prevent the pause, so if you mix
  sensors with a single thermostat, switching that thermostat off pauses the heating.
* Unavailable thermostats are ignored. If none can be read, the pause is not applied.
* The pause comes before the warm and mild-day rules, and like them it only resumes heating that the
  controller paused itself.
* Turn the option off if you only want switched-off rooms ignored, without pausing the heat pump.

#### Where to see it

* **Smart heating target LWT** sensor attributes: `room_offset` (current correction), `room_mode`,
  `room_error` (the combined shortfall), `room_driver` (the room that decides in coldest/warmest mode),
  `rooms` (each room's temperature, target and on/off) and `curve_lwt` (the curve value before the correction).
* **Smart heating status**: `paused_rooms_off` while paused because all thermostats are off.
* **Heating curve** image: the dashed line and the "now" label show the correction, e.g. `now 9.4° → 29° (room +1)`.

#### Tips

* **Tune the curve first.** The correction is for fine-tuning. If it sits at the maximum (or minimum) for days,
  the curve itself is off: move the curve's LWT points by that amount and the correction will settle near 0.
* **Coldest room always at the maximum:** that room may need more flow on its manifold loop rather than a
  hotter house. Check its flow meter before raising the curve.
* **Room overshoots and undershoots around the target:** reduce the maximum to 1–2 °C. A large correction on a
  slow floor makes the rooms swing.
* **Mild-day window:** expect the correction to rise during long pauses. A maximum of 2 °C keeps the start of
  the next run from being too hot.
* **Turning it off:** clear the room list or set the maximum to 0 in the options.

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
| Smart heating status (sensor) | `heating`, `paused_mild_outside_window`, `paused_warm`, `paused_rooms_off` (all selected thermostats off), `space_heating_off` (switched off by you), `cooling_mode`, `defrost`, `waiting_for_data` or `disabled` |
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
