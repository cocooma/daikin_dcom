# Smart heating – logic

How the smart heating controller (`custom_components/daikin_dcom/smart.py`) decides what to do. It runs on every
poll (30 s by default) and at least once a minute. Nothing here acts unless **Smart heating control** is on.
Settings and switches are explained in the [README](../README.md#smart-heating-control-optional).

## 1. Each evaluation

```mermaid
flowchart TD
    start(["Poll / minute tick"]) --> en{"Smart heating<br/>control on?"}
    en -- no --> idle["Do nothing<br/>unit runs on its own settings"]
    en -- yes --> data{"Data ready and<br/>unit heating?"}
    data -- "no data" --> idle2["Do nothing<br/>status: waiting_for_data"]
    data -- cooling --> cool["Leave the unit alone<br/>switch any selected thermostat that is on to off<br/>(option, logbook)<br/>status: cooling_mode"]
    data -- yes --> stale{"Space heating on but<br/>marked paused by us?"}
    stale -- yes --> clear["Clear the stale flag<br/>(logbook)"] --> user
    stale -- no --> user{"Space heating off<br/>and not paused by us?"}
    user -- yes --> respect["Leave it off<br/>status: space_heating_off"]
    user -- no --> sp["Setpoint mode fixed (reg 53 = 0)<br/>target = curve(outdoor avg) + room correction<br/>clamped 25 °C … max LWT"]
    sp --> rest{"Compressor rest<br/>running?"}
    rest -- yes --> r25["reg 1 = 25 °C<br/>(space heating and pump stay on)"] --> dfr
    rest -- no --> write["reg 1 = target<br/>(max every 10 min unless Δ ≥ 2 °C;<br/>not written while paused by us)"] --> dfr
    dfr{"Defrost /<br/>start-up?"}
    dfr -- yes --> hold["Keep current on/off<br/>status: defrost / compressor_rest"]
    dfr -- no --> decide[["Pause decision (section 2)"]]
    decide --> apply{"Allowed?"}
    apply -- "no, heating on, on ≥ 30 min<br/>(or at once if all thermostats off)" --> pause["reg 4 = 0 (pause)"]
    apply -- "yes, paused by us,<br/>off ≥ 20 min" --> resume["reg 1 = curve target, then<br/>reg 4 = 1 (resume)"]
    apply -- "otherwise" --> keep["No change"]
    pause --> stats
    resume --> stats
    keep --> stats
    stats["Thermostats (section 3)"] --> status(["Update status, attributes, logbook"])
```

## 2. Pause decision

Checked in this order; the first match wins. The **heat only on thermostat demand** check comes last and can only
turn "heat" into a pause, never the other way round. Thresholds use the outdoor temperature averaged over about an hour,
with 0.5 °C hysteresis.

```mermaid
flowchart TD
    q0{"Pause when all thermostats off<br/>is on, every readable thermostat off,<br/>and not switched off by us?"}
    q0 -- yes --> roomsoff["Pause at once<br/>(no 30 min minimum run)<br/>paused_rooms_off"]
    q0 -- no --> q1{"Outdoor avg ≥<br/>no space heating above?"}
    q1 -- yes --> warm["Pause<br/>paused_warm"]
    q1 -- no --> q2{"Window switch on,<br/>outdoor avg ≥ mild day above,<br/>outside the window?"}
    q2 -- no --> dem
    q2 -- yes --> q3{"Cloudy-day override switch on,<br/>weather cloudy (state or coverage ≥ option)<br/>and a thermostat calling for heat?"}
    q3 -- yes --> cloudy["heating_cloudy_override<br/>(ends 15 min after the conditions stop)"] --> dem
    q3 -- no --> q4{"Early start on and the coldest room<br/>≥ shortfall below target for the delay?"}
    q4 -- yes --> early["heating_early_start<br/>(until within 0.3 °C and min run done)"] --> dem
    q4 -- no --> mild["Pause<br/>paused_mild_outside_window"]
    dem{"Heat only on thermostat demand switch on,<br/>a thermostat readable, none held off by us,<br/>and none calling for 10 min?"}
    dem -- yes --> nodemand["Pause<br/>paused_no_demand"]
    dem -- no --> heat["Heat<br/>heating / override status"]
```

## 3. Thermostats in warm weather

Only with the option **switch the selected thermostats off while heating is paused for warm weather**.

```mermaid
flowchart TD
    t0{"Paused by us<br/>for warm weather?"}
    t0 -- yes --> t1{"Option on and none<br/>switched off yet?"}
    t1 -- yes --> off["Switch the selected thermostats that are on to off<br/>remember each mode (persisted, logbook)"]
    t1 -- no --> nop["No change"]
    t0 -- no --> t2{"Any switched off by us and<br/>heating resumed / control switched off?"}
    t2 -- yes --> on["Restore each saved mode<br/>(only those still off, logbook)"]
    t2 -- no --> nop
```

A warm pause that turns into a mild-day pause keeps them off until heating restarts.

## 4. Compressor rest

Runs alongside section 1 (option, on by default).

```mermaid
flowchart TD
    c0(["Compressor stops (input 31: 1 → 0)"]) --> c1{"Space heating on, not hot water,<br/>not paused by us, stop &lt; 5 min ago?"}
    c1 -- no --> ign["Ignore<br/>(last_compressor_stop records why)"]
    c1 -- yes --> c2["Rest: reg 1 = 25 °C<br/>length from the rest curve (outdoor avg → minutes)<br/>logbook"]
    c2 --> cp{"Smart heating paused<br/>space heating meanwhile?"}
    cp -- yes --> cq["Rest ends silently<br/>(no setpoint write, no logbook)"]
    cp -- no --> c3{"Rest time over, or the<br/>compressor restarted by itself?"}
    c3 -- yes --> c4["Write the curve target back<br/>logbook"]
```
