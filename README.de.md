# elli-eebus

Elli-Wallbox per **EEBUS** aus Python steuern: Status, Leistungslimit, Failsafe-Einstellungen und Messwerte. Basiert auf [pyeebus](https://github.com/frane/pyeebus).

[English](README.md) · Modbus statt EEBUS: [elli-2-modbus](https://github.com/frane/elli-2-modbus) · Home Assistant: [ha-elli-2-modbus](https://github.com/frane/ha-elli-2-modbus)

> **Status: früh.** Getestet mit einer Elli Charger 2 und mit Simulatoren. Noch nichts veröffentlicht.

## Unterstützte Wallboxen

| Wallbox | Was EEBUS bietet | elli-eebus |
|---|---|---|
| Elli Charger 2 (aktuelle Firmware) | Leistungslimit (LPC), Gesamtleistung (MPC), Failsafe-Einstellungen | ✅ mit echter Hardware getestet |
| Elli Charger Connect/Pro (erste Generation) | Stromlimit pro Phase (OPEV), Fahrzeug-Messwerte (EVCEM), Fahrzeugdaten (EVCC) | ✅ nur mit Simulatoren getestet |

Die Elli Charger 2 meldet über EEBUS nicht, ob ein Auto angesteckt ist, und liefert weder Ströme pro Phase noch einen Energiezähler. Dafür gibt es Modbus ([elli-2-modbus](https://github.com/frane/elli-2-modbus)).

## Koppeln

1. Einen beliebigen `elli-eebus`-Befehl mit der SKI der Wallbox starten (siehe `elli-eebus discover` oder die Weboberfläche der Wallbox). elli-eebus meldet sich im Netz als „elli-eebus“.
2. In der Weboberfläche der Wallbox **EEBUS-Energiemanager** öffnen (englische Oberfläche: *Connections → HEMS connection*). Unter **Gefundene EEBUS-Geräte** „elli-eebus“ auswählen und koppeln. Die SKI gibt elli-eebus aus.
3. elli-eebus verbindet sich innerhalb von 15 Sekunden. Die Identität liegt in `~/.config/elli-eebus/`, gekoppelt werden muss nur einmal.

## Kommandozeile

```bash
pip install git+https://github.com/frane/elli-eebus.git    # oder: uvx --from git+https://github.com/frane/elli-eebus.git elli-eebus ...

elli-eebus discover
elli-eebus status <SKI> --host 192.168.1.50 [--watch]
elli-eebus set-limit <SKI> 4200 --duration 3600            # W, wird nach einer Stunde aufgehoben
elli-eebus clear-limit <SKI>
elli-eebus set-failsafe <SKI> --watts 4200 --duration 7200
```

Ohne `--host` wird die Wallbox per mDNS gefunden.

## Bibliothek

```python
from ellieebus import ElliEebus

async with ElliEebus("<SKI>", state_dir="~/.config/elli-eebus", host="192.168.1.50") as elli:
    await elli.wait_connected(60)
    elli.add_listener(print)                       # bei jeder Änderung mit einem ElliStatus
    print(elli.status().power, elli.status().power_limit)
    await elli.set_power_limit(4200, duration=3600)
    await elli.set_current_limit(10)               # erste Generation: pro Phase; Elli Charger 2: 10 A x 230 V x 3
    await elli.clear_power_limit()
```

## Firmware-Fehler der Elli und wie elli-eebus damit umgeht

Gefunden mit Mitschnitten einer Elli Charger 2 (Oktober 2026):

- **Limits mit Dauer werden ignoriert.** Ein Leistungslimit mit Dauer (`timePeriod`) wird nicht beantwortet und nicht angewendet. elli-eebus schickt Limits ohne Dauer und hebt sie nach Ablauf selbst auf, auch nach einem Neustart (das Ende steht in `state.json`).
- **Aufheben geht nur mit 0 W.** `isLimitActive: false` wird abgelehnt („Write failed“), außer der Wert ist 0. elli-eebus hebt Limits immer so auf.

Stoppt elli-eebus bei aktivem Limit, bemerkt die Wallbox den fehlenden Heartbeat und wechselt für die Failsafe-Dauer (mindestens 2 Stunden) auf ihr Failsafe-Limit (Standard 22 kW, also keine Einschränkung).

## Simulator

`elli-eebus simulate --trust <SKI deines Clients>` startet eine simulierte Elli Charger 2 mit demselben Gerätemodell und denselben Firmware-Fehlern. `demand <W>` setzt, was ein Auto ziehen würde; die gemeldete Leistung hält das Limit ein.

## Entwicklung

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest
```

*EEBUS ist eine Marke der EEBus Initiative e.V., Elli eine Marke der Elli Mobility GmbH. Dieses Projekt ist mit keiner von beiden verbunden.*
