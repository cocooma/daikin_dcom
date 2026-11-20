# Daikin Altherma 3 – DCOM-LT/MB (Modbus TCP) for Home Assistant

Custom integration for a Daikin Altherma 3 heat pump with a Voytech
**DCOM-LT/MB** module, reached through a Modbus TCP gateway.

## Install

1. Copy `custom_components/daikin_dcom` into your HA `config/custom_components/`.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → *Daikin Altherma 3 (DCOM-LT/MB Modbus TCP)*.

## Setup options

| Field | Meaning |
|---|---|
| Host / Port | Modbus TCP gateway (default port 502) |
| Modbus address | Set with DIP switches SW1.3–SW1.8 on the DCOM (1–63 range, see manual p.3) |
| Framing | `Modbus TCP` for gateways that convert to Modbus RTU; `RTU over TCP` for transparent serial-to-Ethernet bridges (RTU frames inside TCP) |
| Temperature control | `Leaving water temperature` uses registers 1/2 (heating 25–55 °C, cooling 5–22 °C). `Room thermostat` uses registers 6/7 (heating 12–30 °C, cooling 15–35 °C) |

Serial settings of the DCOM RS485 side are fixed: 9600 baud, 8N1.
The control mode and polling interval can be changed later in the integration options.

## Entities

**Climate – Space heating/cooling** (one entity for heating and cooling)

| HVAC mode | Registers written |
|---|---|
| off | 4 = 0 |
| heat | 3 = 1, 4 = 1 |
| cool | 3 = 2, 4 = 1 |
| auto | 3 = 0, 4 = 1 |

The target temperature changes the heating or the cooling setpoint depending on the active mode
(in auto mode, the mode the unit is currently running in, input register 38).
Current temperature: LWT (reg 40) or Remocon room temperature (reg 50), depending on control mode.

With *Leaving water temperature* control and weather dependent mode active (reg 53 = 1 or 3), the unit
follows its weather curve and the DCOM has no register for the calculated setpoint. The target temperature
then shows and sets the weather dependent offset instead (reg 54 heating / 55 cooling, -10 to +10 °C);
the attribute `weather_dependent: true` indicates this. Room thermostat control is unaffected.

**Climate – Domestic hot water**: off/heat = DHW reheat (reg 12), target = DHW reheat setpoint 30–60 °C (reg 10),
current = DHW temperature (input 43), preset `boost` = DHW booster mode (reg 13).

Additional entities: temperature sensors, flow rate, error code/sub code, binary sensors
(error, pump, compressor, booster heater, disinfection, defrost, hot start, 3-way valve),
quiet mode switch, weather dependent mode select and the two weather dependent LWT offset numbers.

## Smart heating control (optional)

Runs a heating curve and mild-day scheduling inside Home Assistant instead of the unit's two-point
weather curve. Turn it on with the **Smart heating control** switch; settings are in the integration options.

While the switch is on and the unit is heating, the controller:

1. sets the setpoint mode to *fixed* (reg 53 = 0); the previous mode is restored when you switch it off,
2. writes the leaving water setpoint (reg 1) from its own curve, using the outdoor temperature (input 44)
   averaged over about an hour, plus an optional room correction,
3. pauses space heating (reg 4 = 0) on mild days outside the time window and on warm days, and resumes it.

| Option | Default | Meaning |
|---|---|---|
| Cold point | −5 °C → 34 °C | Curve point; the LWT stays flat below it |
| Warm point | 15 °C → 26 °C | Curve point; the LWT stays flat above it (reg 1 minimum is 25 °C) |
| Maximum LWT | 40 °C | Safety cap |
| Mild day above | 12 °C | Above this, heat only inside the window |
| No space heating above | 16 °C | Heating paused |
| Room sensor / target / max correction | – / 22 °C / 3 °C | Optional: ±1 °C LWT step every 45 min when the room is 0.5 °C off target |

The **mild-day window** is set from the dashboard, not in the options; changes apply immediately and are kept
across restarts:

| Entity | Default | Meaning |
|---|---|---|
| Smart heating window start / end (time) | 11:00 / 17:00 | On mild days heating runs only in this window — pick the warmest hours (better COP), the floor slab stores the heat. May cross midnight; start = end means all day |
| Smart heating mild-day window (switch) | on | Off: no time restriction on mild days (only the "no space heating above" limit applies) |

Behaviour and limits:

* Thresholds use 0.5 °C hysteresis. The controller keeps heating on for at least 30 min and off for at least
  20 min, and writes the setpoint at most every 10 min (immediately if it changes by 2 °C or more).
* It only resumes heating it paused itself: if you switch space heating off, it stays off.
* It does nothing in cooling mode or during defrost.
* While it is on, setpoint and weather mode changes made elsewhere (climate entity, select, Daikin UI) are
  overwritten. Switch it off to take manual control.
* Overshoot ([9-04]) and the unit's own curve ([1-00]–[1-03]) are field settings with no DCOM register,
  so they cannot be changed from here.

Entities: **Smart heating control** (switch, state restored after restart), the window time entities and switch above, **Smart heating target LWT** and
**Smart heating status** (sensors; attributes show the averaged outdoor temperature, curve value and room correction).

### Heating curve on a dashboard

The **Smart heating target LWT** sensor has a `curve_points` attribute (`[outdoor, LWT]` pairs from −15 to
+20 °C, not stored in the recorder). With [apexcharts-card](https://github.com/RomRider/apexcharts-card)
(HACS) the curve and the current operating point can be shown like this:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Heating curve
apex_config:
  xaxis:
    type: numeric
    title: { text: Outdoor °C }
  yaxis:
    - min: 24
      max: 40
      title: { text: Leaving water °C }
  markers:
    size: [0, 7]
series:
  - entity: sensor.daikin_altherma_3_smart_heating_target_lwt
    name: Curve
    type: line
    data_generator: |
      return entity.attributes.curve_points;
  - entity: sensor.daikin_altherma_3_smart_heating_target_lwt
    name: Now
    type: scatter
    data_generator: |
      return [[entity.attributes.outdoor_average, Number(entity.state)]];
```

Adjust the entity id if yours differs. "Now" is the averaged outdoor temperature and the target written
to the unit, including any room correction.

## COP metrics

The DCOM has no power or energy registers, so heat is calculated from the water side and electricity comes
from your own meter (integration options: **heat pump energy meter** in kWh, preferred, or **power sensor** in
W/kW). Without a meter only the thermal sensors are created.

* Thermal power = flow (input 49) × 4.186 × ΔT. Heating / DHW: LWT − return; cooling: return − LWT.
  LWT is input 41 (pre BUH), falling back to input 40. Defrost gives negative values, which correctly
  reduces the heat counted. Heat from the DHW tank booster heater is not measured.
* Electricity is assigned to the mode the unit is in (3-way valve on DHW → DHW, operation mode cooling →
  cooling, otherwise heating), standby included.
* Gaps longer than 5 min (restart, outage) are skipped. Counters are restored after a restart; "today" resets
  at local midnight.

| Entity | Meaning |
|---|---|
| Thermal power | kW delivered (heating/DHW) or extracted (cooling); attribute `mode` |
| Heating / Cooling / DHW thermal energy | lifetime kWh |
| Heat pump electrical power | kW from the meter |
| Heating / Cooling / DHW electrical energy | lifetime kWh |
| COP (15 min) | rolling COP for the current mode |
| Heating / Cooling / DHW COP today | attributes: today's thermal and electrical kWh |
| Heating / Cooling / DHW COP (lifetime) | since the counters started |

The energy sensors can be added to the HA Energy dashboard or a `utility_meter` for monthly figures.
Accuracy depends on the flow and temperature sensors: at a 3 °C ΔT, a 0.3 °C error is about 10 %.

## Notes

* Register numbers are used as Modbus addresses directly (manual: "Register Base 0").
  If every value looks shifted by one, your gateway adds a +1 offset; tell me and I will add an option.
* The DCOM returns 32765 / 32766 / 32767 for "wait for value", "unavailable" and "unsupported";
  these are mapped to *unavailable* entity states. Right after power-up the DCOM can need up to ~8 minutes
  to synchronise with the Altherma master (Status LED).
* Only documented, contiguous register blocks are read, so no undocumented addresses are requested.
* Setpoints written outside the unit's configured range are clamped by the DCOM; other out-of-range writes are ignored.
* Written values are shown optimistically and confirmed on the next poll.

## Not tested against hardware

This was built from the DCOM-LT/MB manual (534-252-A3) and has been syntax-checked only.
Please test with your unit and report issues.
