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
* No power, energy, compressor frequency or current registers, so COP needs an external sensor. COP was
  built, removed at the user's request, and re-added later on request (`cop.py`): one option
  `cop_electricity_sensor` (power W/kW integrated, or energy Wh/kWh/MWh as deltas, chosen by its unit; the
  older `cop_energy_sensor` / `cop_power_sensor` keys are still honoured). Sensors: one **COP** (15 min, current
  mode; per-mode today/lifetime/15 min as attributes), **Thermal power** (always; persists the counters) and
  **Heat pump electrical power**. The user preferred a single COP sensor over per-mode sensors.
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
| `smart.py` | `SmartHeatingController`: HA-side curve, mild-day window, warm cut-off, room correction, early start, compressor rest |
| `image.py` | `render_curve_svg()` + image entity drawing the curve and operating point |
| `number.py` | Weather offsets (54/55) and the four smart heating curve point numbers |
| `time.py` / `switch.py` | Window start/end; smart heating switch, window switch, cloudy-day override switch, demand-only switch, quiet mode |
| `sensor.py` | Temperatures, flow, error codes, smart target LWT, status, compressor starts, COP, thermal and electrical power |
| `cop.py` | `CopTracker`: thermal power from flow × ΔT, electricity from the configured sensor, per-mode COP |
| `config_flow.py` | Setup and options (control mode, poll interval, max LWT, mild/off thresholds, room sensor) |

`coordinator.smart` holds the controller; it is created in `__init__.py` before the platforms are set up.

## Smart heating controller rules (keep consistent with README and `docs/smart-heating-logic.md`)

`docs/smart-heating-logic.md` holds Mermaid flowcharts of the logic (rendered by GitHub; check syntax with
`mmdc`). Update them when a rule below changes.


* Only acts when enabled and the unit is heating; in cooling it only keeps the thermostats off (option); nothing during defrost.
* Takes over the setpoint: reg 53 → 0 (previous mode remembered and restored on disable), reg 1 = curve
  value + room offset, clamped to 25..max LWT. Outdoor temperature (input 44) is an EMA with ~60 min τ.
* Pauses space heating (reg 4 = 0) above the off temperature, and above the mild temperature outside the
  window; 0.5 °C hysteresis; min on 30 min, min off 20 min.
* Only resumes heating it paused itself (`paused_by_us`); a user "off" is respected. If space heating is found
  **on** while `paused_by_us` is set (someone switched it back on, or a restored flag), the flag is cleared on
  the next evaluation (logbook entry) - a stale flag made every compressor stop "ignored (paused by
  controller)" and the controller never managed heating again. Stop detection uses the real reg 4 state.
* Setpoint writes at most every 10 min unless the change is ≥ 2 °C or the curve was just changed.
* Room correction: ±1 °C step every 45 min when the room is ≥ 0.5 °C off target, limited to ±max.
* Room correction can use several `sensor`/`climate` entities (thermostats: own target, skipped while off),
  combined as coldest/average/warmest; optional pause (`paused_rooms_off`) when every readable thermostat is off.
  That pause ignores MIN_ON_TIME (user's choice: switch off at once); re-applied every evaluation, so heating
  switched on elsewhere while all are off goes off again (stale flag cleared, then paused in the same run).
* Cooling mode (option `smart_cooling_thermostats_off`, default on): the unit is not touched, but every
  evaluation switches any selected `climate` entity that is not off to `off` (`_keep_thermostats_off`, logbook).
  Not restored on return to heating (user's choice), so heating then stays `paused_rooms_off` until one is on.
* Early start (option, off by default): during a mild-day pause outside the window, the coldest counted room
  ≥ shortfall (1.0 °C) below target for a delay (30 min) resumes heating (`heating_early_start`); it ends when
  that room is within 0.3 °C and the minimum early run (45 min) has passed. Not applied to warm or rooms-off pauses.
* Cloudy-day override (option `smart_cloudy_weather_entity`, empty = off): during a mild-day pause outside the
  window, heating resumes while the weather entity is cloudy (CLOUDY_STATES or `cloud_coverage` ≥ the `smart_cloudy_coverage` option, default 80 %) and a
  selected thermostat is on and calling (`hvac_action` heating/preheating; without hvac_action ≥ 0.3 °C below
  target). Ends after 15 min without both (`heating_cloudy_override`, logbook entries). Not applied to warm or
  rooms-off pauses. Runs before early start in `_evaluate`. Both conditions are required (user's explicit choice: no override
  on a clear day). On/off via the "Smart heating cloudy-day override" restore switch (`set_cloudy`, default on).
* Heat only on thermostat demand (restore switch, default off, `set_demand_only`): `_demand_gate` runs after
  the other rules and can only take heating away: with heating allowed, pause (`no_demand`,
  `paused_no_demand`) once no selected thermostat has called (same calling test as the cloudy override) for
  DEMAND_HOLD = 10 min. Inactive when no thermostat is readable, and while `stats_off_by_us` (else the warm-weather
  thermostats could never be restored). `calling_rooms` is computed once per evaluation in `_evaluate`.
* Thermostats off in warm weather (option `smart_warm_thermostats_off`, default off): when the controller has
  paused for "warm", the selected `climate` entities that are on are set to `off` via `climate.set_hvac_mode`
  (previous mode saved in `stats_off_by_us`, persisted as `thermostats_off`); restored when `paused_by_us`
  clears (heating restarted, stale flag cleared, or controller disabled), only those still `off`. A warm pause
  that becomes a mild pause keeps them off until heating restarts (user's wording: "when the heating
  restarts"). The rooms-off pause is skipped while `stats_off_by_us` is set.
* Compressor rest (option, on by default; length from a two-point rest curve on the dashboard, outdoor EMA →
  minutes, `rest_cold_*`/`rest_mild_*` restore numbers seeded from the old 20 min option): a compressor 1→0 transition (input 31) while space heating
  is on, not in DHW (input 37, now or on the previous reading) and not paused by the controller, starts a
  rest. Input 35 is "defrost / startup" and is also set during compressor start-up, so it must NOT exclude
  stops or skip the evaluation (short runs end during start-up); it only suspends pause/resume decisions.
  `last_compressor_stop` attribute records each stop and its outcome. Rest start/end setpoint changes are written to the
  logbook via `homeassistant.components.logbook.async_log_entry`, attached to the status sensor (its entity
  id is handed to the controller in `async_added_to_hass`); manifest has `after_dependencies: [logbook]`. Space heating stays on (so the pump keeps running with [F-0D] = 0) and reg 1 is set
  to 25 °C; the curve target is written back when the rest ends or the unit restarts by itself. Stops older than
  5 min when handled are ignored. While the controller holds space heating off (`paused_by_us` and reg 4 = 0) a running rest is
  dropped silently and reg 1 is not written at all (user: no setpoint activity during the mild-day pause); the resume
  branch writes the curve target to reg 1 before reg 4 = 1. Starts are counted in `_track_compressor`, called from `_schedule` on every
  update even when the controller is disabled; counts persist in the switch's restore data.
* Runtime state (enabled, paused_by_us, paused_reason, room offset, previous mode) persists through the switch's restore
  data. The status sensor is a RestoreEntity: `hold_status()` shows its last state until `restore()` has run and an
  evaluation gives something other than `waiting_for_data` (max STATUS_HOLD = 2 min; cleared by enable/disable). Window times/switch and curve points are their own restore entities; restoring them must not
  trigger writes (`set_curve(..., evaluate=False)`, `restore()` only notifies).
* Settings split: curve points and window on the dashboard (entities, no reload); max LWT, thresholds and
  room sensor in the options (reload). Old option values for the curve/window act as initial values.
* Defaults: −5 °C → 34 °C, 15 °C → 26 °C, max 40 °C, mild 12 °C, off 16 °C, window 11:00–17:00,
  room target 22 °C, max correction 3 °C.

## Decisions

* apexcharts-card was dropped (not maintained recently); the integration draws the curve itself as an SVG
  image entity shown with the built-in Picture entity card. Colours are chosen to work on light and dark
  themes. With the room-corrected (dashed) and rest (dotted, right-hand minutes scale) curves there is no
  reliably free spot inside the plot, so the "now" text and the legend are rows below the chart; only the dot
  stays on the chart.
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

## HACS / packaging

* Repo layout follows HACS: `custom_components/daikin_dcom/`, `hacs.json` at the root (name, minimum HA
  2024.4.0, render_readme), brand images in `custom_components/daikin_dcom/brand/` (`icon.png` 256 px,
  `icon@2x.png` 512 px; no logo, HA falls back to the icon; shown from HA 2026.3).
* `manifest.json`: `domain`, `name` first, then keys alphabetical (hassfest); codeowners `@cocooma`;
  documentation / issue_tracker point at the GitHub repo. Bump `version` with each release and tag the release
  `v<version>` (HACS shows release tags; the manifest version is what it reports as installed).
* `.github/workflows/validate.yml` runs the HACS action and hassfest on push, PRs and weekly.
* HACS needs the repo to be public and to have a description and topics on GitHub.
* Releases: the claude.ai cloud session can push branches and open PRs but may not push tags, create
  releases or change repo settings. `.github/workflows/claude.yml` (anthropics/claude-code-action@v1, secret
  `CLAUDE_CODE_OAUTH_TOKEN`, Claude GitHub App installed) lets `@claude` in an issue/PR do those from Actions,
  e.g. "@claude publish release v1.1.0 from main".

## Working conventions

* One feature per branch, merged via pull request on GitHub (`cocooma/daikin_dcom`); don't commit to `main`.
* Commits are authored as the repo owner (`Oszkar Nagy <oszkar@na.gy>`) with the Claude co-author trailer.
* The owner's local checkout is `~/repos/home-automation/ha_custom_components/daikin_dcom` on a Mac. Its
  `origin` uses SSH, which the desktop bridge shell cannot reach; push from a cloud clone over HTTPS
  instead (a `git bundle` from the Mac can carry local commits across).
* Don't commit `__pycache__/` or scratch folders.
