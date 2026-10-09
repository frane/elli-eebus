# elli-eebus

Control an Elli wallbox over **EEBUS** from Python: status, power limits, failsafe settings and measurements. Built on [pyeebus](https://github.com/frane/pyeebus).

[Deutsch](README.de.md) · Modbus instead of EEBUS: [elli-2-modbus](https://github.com/frane/elli-2-modbus) · Home Assistant: [ha-elli-2-modbus](https://github.com/frane/ha-elli-2-modbus)

> **Status: 0.1.0, early.** Tested with a real Elli Charger 2 (also in use behind [elli-eebus-proxy](https://github.com/frane/elli-eebus-proxy) with Solar Manager) and against simulators.

## Supported wallboxes

| Wallbox | What EEBUS offers | elli-eebus |
|---|---|---|
| Elli Charger 2 (current firmware) | power limit (LPC), total power (MPC), failsafe settings | ✅ tested with real hardware |
| Elli Charger Connect/Pro (first generation) | per phase current limits (OPEV), EV measurements (EVCEM), EV data (EVCC) | ✅ tested with simulators only |

The Elli Charger 2 does not report whether a car is connected, and it reports neither the current per phase nor an energy meter over EEBUS. Use Modbus ([elli-2-modbus](https://github.com/frane/elli-2-modbus)) if you need those.

## Pairing

1. Run any `elli-eebus` command with the wallbox's SKI (see `elli-eebus discover` or the wallbox UI). elli-eebus announces itself on the network as "elli-eebus".
2. In the wallbox's web UI, open **Connections → HEMS connection** (*EEBUS energy manager*). Under **Found EEBUS devices**, select **elli-eebus** and pair. Its SKI is printed by elli-eebus.
3. elli-eebus connects within 15 seconds. The identity is kept in `~/.config/elli-eebus/`, so pairing is needed once.

## Command line

```bash
pip install git+https://github.com/frane/elli-eebus.git    # or: uvx --from git+https://github.com/frane/elli-eebus.git elli-eebus ...

elli-eebus discover
elli-eebus status <SKI> --host 192.168.1.50 [--watch]
elli-eebus set-limit <SKI> 4200 --duration 3600            # W, lifted after an hour
elli-eebus clear-limit <SKI>
elli-eebus set-failsafe <SKI> --watts 4200 --duration 7200
```

Without `--host` the wallbox is found via mDNS.

## Library

```python
from ellieebus import ElliEebus

async with ElliEebus("<SKI>", state_dir="~/.config/elli-eebus", host="192.168.1.50") as elli:
    await elli.wait_connected(60)
    elli.add_listener(print)                       # called with an ElliStatus on every change
    print(elli.status().power, elli.status().power_limit)
    await elli.set_power_limit(4200, duration=3600)
    await elli.set_current_limit(10)               # first generation: per phase; Elli Charger 2: 10 A x 230 V x 3
    await elli.clear_power_limit()
```

## Elli firmware bugs and how elli-eebus handles them

Found with recordings of an Elli Charger 2 (October 2026):

- **Limits with a duration are ignored.** A power limit that carries a duration (`timePeriod`) gets no answer and is not applied. elli-eebus sends limits without a duration and lifts them itself when the time is up, also after a restart (the end time is kept in `state.json`).
- **Lifting a limit only works with 0 W.** `isLimitActive: false` is rejected ("Write failed") unless the value is 0. elli-eebus always lifts limits that way.
- **Pausing needs the EV charging services.** An active limit below the minimum charging power (e.g. 0 W to pause) is rejected ("Write failed") unless the energy manager announces self-consumption (OSCEV) and cost-optimized charging (CEVC). Without them the Elli also logs errors 0x401029 and 0x40102A. elli-eebus announces both, so `set_power_limit(0)` pauses charging.
- **Bindings outlive the pairing.** After removing an energy manager in the Elli UI, the Elli keeps its binding to the power limit and refuses it to the next one ("Adding binding failed") until the Elli restarts. Keep elli-eebus' state directory: a new certificate is a new energy manager.

If elli-eebus stops while a limit is active, the wallbox notices the missing heartbeat and switches to its failsafe limit (by default 22 kW, so no restriction) for the failsafe duration (at least 2 hours).

## Simulator

`elli-eebus simulate --trust <SKI of your client>` runs a simulated Elli Charger 2 with the same device model and the same firmware bugs. `demand <W>` sets what a car would draw; the reported power respects the limit.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest
```

*EEBUS is a trademark of EEBus Initiative e.V., Elli is a brand of Elli Mobility GmbH. This project is not affiliated with either.*
