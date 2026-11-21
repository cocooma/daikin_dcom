# Project context: daikin_dcom

Home Assistant custom integration for a Daikin Altherma 3 heat pump with a Voytech **DCOM-LT/MB** module,
reached over Modbus TCP. User-facing documentation is in `README.md`; this file holds the background,
design decisions and working conventions behind it.

## The installation it was built for

* Daikin Altherma 3, 16 kW, 3-phase (split ERLA-type outdoor unit), DCOM-LT/MB on the P1P2 bus, Modbus TCP
  gateway.
* Underfloor heating only: 6 loops (3 at 5 cm spacing, e.g. bathrooms; 3 at 15 cm), about 80 l in the
  loops. All loops are kept open (no zone valves closing) to keep water volume and flow up.
* Flow about 25 l/min (1.5 m³/h). Daikin's installer guide for ERLA11–16 gives 22 l/min minimum flow for
  heating/defrost and 20 l minimum water volume, so there is little margin.
* Heating ΔT target [9-09] = 3 °C. Rooms at about 24 °C. Local winter lows around 0 °C.
* Overshoot [9-04] = 2 °C, set on the unit's user interface (installer level).
* Measured at +10 °C outdoor: about 4.8–5.2 kW heat for about 0.83 kW electricity (COP roughly 5.8–6.3).
* The unit is oversized for the house: its minimum output is about 5 kW (owners report the 11–16 kW units
  only modulate down to roughly 5 kW at +7 to +10 °C), while the house needs about 2–3 kW at +15 °C. It
  therefore cycles on mild days; no setting can make it run continuously there.

## Heat-pump findings that shaped the design

* A larger ΔT setting does not lower the output here: the flow can't drop much below ~22–25 l/min, so a
  5 °C target forces about 8.7 kW instead of ~5.2 kW, heating the slab and raising the return temperature.
  Keep ΔT at 3 °C.
* Higher loop flow (3–5 l/min per loop) reduces cycling rather than causing it; the limit is noise above
  ~0.7 m/s (16×2 mm pipe: 5 l/min ≈ 0.74 m/s).
* Floor output ≈ 1.9 kW per °C of (average water − room) for this house, average water = LWT − ΔT/2. So
  1 °C of LWT changes floor output a lot near room temperature.
* Lower overshoot means more restarts, and each restart ramps the compressor hard. 2 °C is the compromise.
* On mild days the best option is one long run in the warmest hours (better COP, slab stores the heat).
  This is what the smart heating controller's mild-day window does.
* A buffer tank (series, on the return, ~50–100 l) was considered; keeping all loops open came first.

## DCOM-LT/MB facts (manual 534-252-A3)

* Register base 0: register number = Modbus address.
* Writable: 1 LWT heat setpoint (25–55), 2 LWT cool (5–22), 3 operation mode, 4 space heating on/off,
  6/7 room setpoints, 9 quiet mode, 10 DHW setpoint, 12/13 DHW reheat/booster, 53 setpoint mode
  (0 fixed, 1 weather dependent, 2/3 scheduled variants), 54/55 weather dependent offsets (−10..+10).
* No access to field settings: overshoot [9-04], curve points [1-00]–[1-03], ΔT [9-09] etc. can only be set
  on the unit. That is why the controller runs its own curve via reg 53 = 0 + reg 1.
* No power, energy, compressor frequency or current registers, so COP cannot be calculated from the DCOM
  alone. A COP feature with an external meter was built and then removed at the user's request; don't
  re-add it unless asked.
* Special values 32765/32766/32767 = wait / unavailable / unsupported → entity unavailable.
* In weather dependent mode the DCOM exposes no calculated setpoint; the climate entity then shows and sets
  the offset (reg 54/55).

## Code layout

| File | Purpose |
|---|---|
| `modbus_client.py` | Async pymodbus wrapper; TCP or RTU-over-TCP framing; serialised with a lock |
| `coordinator.py` | Polls documented register blocks (`READ_BLOCKS` in `const.py`); `value`/`temp`/`text` helpers; `async_write` (optimistic) |
| `const.py` | Register numbers, ranges, option keys, `SMART_DEFAULTS` |
| `climate.py` | Space heating/cooling climate (regs 3/4 + setpoint or weather offset) and DHW climate |
| `smart.py` | `SmartHeatingController`: HA-side curve, mild-day window, warm cut-off, room correction |
| `image.py` | `render_curve_svg()` + image entity drawing the curve and operating point |
| `number.py` | Weather offsets (54/55) and the four smart heating curve point numbers |
| `time.py` / `switch.py` | Window start/end; smart heating switch, window switch, quiet mode |
| `sensor.py` | Temperatures, flow, error codes, smart target LWT and status sensors |
| `config_flow.py` | Setup and options (control mode, poll interval, max LWT, mild/off thresholds, room sensor) |

`coordinator.smart` holds the controller; it is created in `__init__.py` before the platforms are set up.

## Smart heating controller rules (keep consistent with README)

* Only acts when enabled and the unit is heating; does nothing in cooling or during defrost.
* Takes over the setpoint: reg 53 → 0 (previous mode remembered and restored on disable), reg 1 = curve
  value + room offset, clamped to 25..max LWT. Outdoor temperature (input 44) is an EMA with ~60 min τ.
* Pauses space heating (reg 4 = 0) above the off temperature, and above the mild temperature outside the
  window; 0.5 °C hysteresis; min on 30 min, min off 20 min.
* Only resumes heating it paused itself (`paused_by_us`); a user "off" is respected.
* Setpoint writes at most every 10 min unless the change is ≥ 2 °C or the curve was just changed.
* Room correction: ±1 °C step every 45 min when the room is ≥ 0.5 °C off target, limited to ±max.
* Runtime state (enabled, paused_by_us, room offset, previous mode) persists through the switch's restore
  data. Window times/switch and curve points are their own restore entities; restoring them must not
  trigger writes (`set_curve(..., evaluate=False)`, `restore()` only notifies).
* Settings split: curve points and window on the dashboard (entities, no reload); max LWT, thresholds and
  room sensor in the options (reload). Old option values for the curve/window act as initial values.
* Defaults: −5 °C → 34 °C, 15 °C → 26 °C, max 40 °C, mild 12 °C, off 16 °C, window 11:00–17:00,
  room target 22 °C, max correction 3 °C.

## Decisions

* apexcharts-card was dropped (not maintained recently); the integration draws the curve itself as an SVG
  image entity shown with the built-in Picture entity card. Colours are chosen to work on light and dark
  themes; the "now" label is placed on the side of the curve that keeps it readable.
* One combined climate entity for heating and cooling, separate DHW climate.
* Everything user-facing is in English; strings live in `strings.json` and are copied to
  `translations/en.json` (keep them identical).

## Testing

There is no Home Assistant test setup in the repo. Changes have been checked by:

* `python3 -m py_compile custom_components/daikin_dcom/*.py`
* importing `smart.py`, `const.py` and `image.py` with small stub `homeassistant` modules and a fake
  coordinator (dict-backed registers, controllable clock) to simulate days: mild-day pausing and resuming
  at the window edges, warm cut-off with hysteresis, user-off respected, cooling ignored, room correction,
  restore of the previous weather mode on disable, curve changes;
* rendering the SVG with `rsvg-convert` on light and dark backgrounds and looking at it.

Nothing has been verified on real hardware by these checks; say so when reporting.

## Working conventions

* One feature per branch, merged via pull request on GitHub (`cocooma/daikin_dcom`); don't commit to `main`.
* Commits are authored as the repo owner (`Oszkar Nagy <oszkar_nagy@yahoo.co.uk>`) with the Claude co-author trailer.

* Don't commit `__pycache__/` or scratch folders.
