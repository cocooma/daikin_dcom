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

**Climate – Domestic hot water**: off/heat = DHW reheat (reg 12), target = DHW reheat setpoint 30–60 °C (reg 10),
current = DHW temperature (input 43), preset `boost` = DHW booster mode (reg 13).

Additional entities: temperature sensors, flow rate, error code/sub code, binary sensors
(error, pump, compressor, booster heater, disinfection, defrost, hot start, 3-way valve),
quiet mode switch, weather dependent mode select and the two weather dependent LWT offset numbers.

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
