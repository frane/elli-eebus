"""Elli Charger over EEBUS.

Elli Charger 2 (current firmware) offers power limitation (LPC) and power
monitoring (MPC). The first generation (Elli Charger Connect/Pro) offers the
EV charging use cases with per phase current limits (OPEV, EVCEM, EVCC).
:class:`ElliEebus` hides the difference and works around known firmware bugs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from pyeebus.service import EebusService
from pyeebus.ship import Identity, RemoteAbortError, ShipError, TrustStore, normalize_ski
from pyeebus.spine import Change, Event, EventType, RemoteDevice, RemoteEntity, Role
from pyeebus.spine.device import partial_filter
from pyeebus.spine.model import scaled_number, scaled_value
from pyeebus.usecases import (
    EVCC,
    EVCEM,
    EVSECC,
    LPC,
    MPC,
    OPEV,
    ClientFeature,
    DataNotAvailable,
    LoadLimit,
    PhaseLimit,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 4711  # the Elli's SHIP port
NOMINAL_VOLTAGE = 230.0


class ElliError(Exception):
    """The wallbox is not connected or does not support the request."""


@dataclass(frozen=True)
class ElliStatus:
    """A snapshot of what the wallbox reported over EEBUS. ``None`` = not reported."""

    connected: bool = False
    profile: str | None = None  # "lpc" (Elli Charger 2) or "opev" (first generation)
    brand: str | None = None
    model: str | None = None
    serial: str | None = None
    software: str | None = None
    operating_state: str | None = None  # normalOperation, standby, failure, ...
    error_code: str | None = None
    power: float | None = None  # W, total
    currents: tuple[float, ...] | None = None  # A per phase
    voltages: tuple[float, ...] | None = None  # V per phase
    energy: float | None = None  # Wh (meter or this session, see energy_scope)
    energy_scope: str | None = None  # "total" or "session"
    power_limit: float | None = None  # W (LPC)
    power_limit_active: bool | None = None
    power_limit_until: float | None = None  # epoch seconds, set by this client
    current_limits: tuple[float, ...] | None = None  # A per phase (OPEV)
    failsafe_power: float | None = None  # W
    failsafe_duration: float | None = None  # s
    nominal_max_power: float | None = None  # W
    min_power: float | None = None  # W, lowest power the wallbox can charge with
    max_power: float | None = None  # W
    min_current: float | None = None  # A per phase
    max_current: float | None = None  # A per phase
    vehicle_connected: bool | None = None  # only the first generation reports the EV
    charge_state: str | None = None  # active, paused, finished, error, unplugged, unknown
    heartbeat_ok: bool | None = None  # the wallbox is alive (EEBUS heartbeat)


Listener = Callable[[ElliStatus], None]


@dataclass
class _State:
    """Persisted between restarts (state.json)."""

    limit_until: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ElliEebus:
    """Connection to one Elli wallbox as EEBUS energy manager.

        elli = ElliEebus("<SKI of the wallbox>", state_dir="~/.config/elli-eebus", host="192.168.1.50")
        await elli.start()                 # pair "elli-eebus" in the wallbox UI the first time
        await elli.wait_connected()
        print(elli.status())
        await elli.set_power_limit(4200, duration=3600)
        await elli.clear_power_limit()
        await elli.stop()
    """

    def __init__(
        self,
        ski: str,
        *,
        state_dir: str | Path,
        host: str | None = None,
        port: int = DEFAULT_PORT,
        local_port: int = 4712,
        name: str = "elli-eebus",
        zeroconf: Any = None,
        announce: bool = True,
    ) -> None:
        self.remote_ski = normalize_ski(ski)
        self.host, self.port = host, port
        self.state_dir = Path(state_dir).expanduser()
        identity = Identity.load_or_create(self.state_dir, name)
        trust = TrustStore.load(self.state_dir / "trust.json")
        self.service = EebusService(
            identity, brand=name, model=name, serial=identity.ski[:8], ship_id=f"{name}-{identity.ski[:8]}",
            port=local_port, trust=trust, auto_connect=host is None, zeroconf=zeroconf,
            announce=announce, discover=host is None)
        cem = self.service.entities[0]
        self._evsecc = EVSECC(cem, self._on_event).setup()
        self._evcc = EVCC(cem, self._on_event).setup()
        self._evcem = EVCEM(cem, self._on_event).setup()
        self._opev = OPEV(cem, self._on_event).setup()
        self._lpc = LPC(cem, self._on_event).setup()
        self._mpc = MPC(cem, self._on_event).setup()
        self._listeners: list[Listener] = []
        self._connected = asyncio.Event()
        self._dialer: asyncio.Task | None = None
        self._release: asyncio.Task | None = None
        self._state = self._load_state()
        self._last_status: ElliStatus | None = None
        self.service.device.subscribe_events(self._on_spine_event)
        self.service.node.on_connected = self._wrap_connected(self.service.node.on_connected)
        self.service.node.on_disconnected = self._wrap_disconnected(self.service.node.on_disconnected)

    # --- lifecycle ------------------------------------------------------------------------

    @property
    def our_ski(self) -> str:
        """Our SKI, to find "elli-eebus" in the wallbox UI when pairing."""
        return self.service.ski

    @property
    def connected(self) -> bool:
        return self._device() is not None

    async def start(self) -> None:
        await self.service.start()
        self.service.trust(self.remote_ski)
        self._resume_release()
        if self.host:
            self._dialer = asyncio.create_task(self._dial_loop())

    async def stop(self) -> None:
        for task in (self._dialer, self._release):
            if task is not None:
                task.cancel()
        await self.service.stop()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until the wallbox has described itself (SPINE discovery done)."""
        async def ready() -> None:
            while self._evse() is None:
                await asyncio.sleep(0.1)
        await asyncio.wait_for(ready(), timeout)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Called with a new :class:`ElliStatus` whenever the wallbox reports something."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)
        return remove

    async def _dial_loop(self) -> None:
        delay = 5.0
        while True:
            if self.remote_ski in self.service.node.connections:
                await self.service.node.connections[self.remote_ski].wait_closed()
                delay = 5.0
            try:
                conn = await self.service.node.connect(self.host, self.port, self.remote_ski)
                delay = 5.0
                await conn.wait_closed()
            except RemoteAbortError:
                _LOGGER.info("wallbox rejected us: pair %s (SKI %s) in its UI", "elli-eebus", self.our_ski)
                delay = 15.0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - network, TLS, SHIP: retry
                _LOGGER.debug("connecting to the wallbox failed: %r", err)
                delay = min(delay * 2, 60.0)
            await asyncio.sleep(delay)

    def _wrap_connected(self, inner):
        def on_connected(conn):
            result = inner(conn)
            self._connected.set()
            return result
        return on_connected

    def _wrap_disconnected(self, inner):
        def on_disconnected(ski):
            inner(ski)
            self._connected.clear()
            self._notify()
        return on_disconnected

    # --- entities ---------------------------------------------------------------------------

    def _device(self) -> RemoteDevice | None:
        return self.service.remote_devices.get(self.remote_ski)

    def _evse(self) -> RemoteEntity | None:
        device = self._device()
        return next((e for e in device.entities if e.type == "EVSE"), None) if device else None

    def _ev(self) -> RemoteEntity | None:
        device = self._device()
        return next((e for e in device.entities if e.type == "EV"), None) if device else None

    def _require_evse(self) -> RemoteEntity:
        evse = self._evse()
        if evse is None:
            raise ElliError("wallbox not connected")
        return evse

    @property
    def profile(self) -> str | None:
        """"lpc" if the wallbox offers power limitation (Elli Charger 2), "opev" if it
        offers per phase current limits for the EV (first generation), else None."""
        evse, ev = self._evse(), self._ev()
        if evse is not None and evse.feature("LoadControl", Role.SERVER) is not None:
            return "lpc"
        if ev is not None and ev.feature("LoadControl", Role.SERVER) is not None:
            return "opev"
        device = self._device()
        if device is not None and any(
                info.get("actor") == "EV" and any(s.get("useCaseName") == OPEV.name
                                                  for s in info.get("useCaseSupport") or [])
                for info in device.use_cases()):
            return "opev"
        return None

    # --- status -------------------------------------------------------------------------------

    def _on_event(self, _ski: str, _entity: RemoteEntity | None, _event: str) -> None:
        self._notify()

    def _on_spine_event(self, event: Event) -> None:
        # The use cases do not read the EVSE's permitted values (power/current
        # range); do it here.
        if event.type == EventType.ENTITY and event.change == Change.ADD and event.entity is not None \
                and event.entity.type == "EVSE":
            f = ClientFeature.find("ElectricalConnection", self.service.entities[0], event.entity)
            if f is not None:
                f.request("electricalConnectionParameterDescriptionListData",
                          "electricalConnectionPermittedValueSetListData")
        elif event.type == EventType.DATA and event.function and event.function.startswith("electricalConnection"):
            self._notify()

    def _notify(self) -> None:
        status = self.status()
        if status == self._last_status:
            return
        self._last_status = status
        for listener in list(self._listeners):
            try:
                listener(status)
            except Exception:
                _LOGGER.exception("status listener failed")

    def status(self) -> ElliStatus:
        evse, ev = self._evse(), self._ev()
        if evse is None:
            return ElliStatus(connected=False)
        values: dict[str, Any] = {"connected": True, "profile": self.profile}

        def get(fn, *args):
            try:
                return fn(*args)
            except (DataNotAvailable, KeyError, IndexError, TypeError, ValueError):
                return None

        if (m := get(self._evsecc.manufacturer_data, evse)) is not None:
            values.update(brand=m.get("brandName") or m.get("vendorName"), model=m.get("deviceName"),
                          serial=m.get("serialNumber"), software=m.get("softwareRevision") or None)
        if (s := get(self._evsecc.operating_state, evse)) is not None:
            values.update(operating_state=s[0], error_code=s[1] or None)
        if (v := get(self._mpc.power, evse)) is not None:
            values["power"] = v
        if v := get(self._mpc.current_per_phase, evse):
            values["currents"] = tuple(v)
        if v := get(self._mpc.voltage_per_phase, evse):
            values["voltages"] = tuple(v)
        if (v := get(self._mpc.energy_consumed, evse)) is not None:
            values.update(energy=v, energy_scope="total")
        if (lim := get(self._lpc.consumption_limit, evse)) is not None:
            values.update(power_limit=lim.value, power_limit_active=lim.is_active)
            if lim.is_active and self._state.limit_until:
                values["power_limit_until"] = self._state.limit_until
        if (v := get(self._lpc.failsafe_consumption_limit, evse)) is not None:
            values["failsafe_power"] = v
        if (v := get(self._lpc.failsafe_duration_minimum, evse)) is not None:
            values["failsafe_duration"] = v
        if (v := get(self._lpc.consumption_nominal_max, evse)) is not None:
            values["nominal_max_power"] = v
        if (rng := get(self._power_range, evse)) is not None:
            values.update(min_power=rng[0], max_power=rng[1])
        if (rng := get(self._current_range, evse)) is not None:
            values.update(min_current=rng[0], max_current=rng[1])
        values["heartbeat_ok"] = self._lpc.is_heartbeat_within(evse) if evse.feature(
            "DeviceDiagnosis", Role.SERVER) else None

        if ev is not None:
            values["vehicle_connected"] = True
            values["charge_state"] = self._evcc.charge_state(ev)
            if (v := get(self._evcem.current_per_phase, ev)) and "currents" not in values:
                values["currents"] = tuple(v)
            if (v := get(self._evcem.power_per_phase, ev)) and "power" not in values:
                values["power"] = sum(v)
            if (v := get(self._evcem.energy_charged, ev)) is not None and "energy" not in values:
                values.update(energy=v, energy_scope="session")
            if (lims := get(self._opev.load_control_limits, ev)) is not None:
                values["current_limits"] = tuple(lim.value for lim in lims)
            if (rng := get(self._opev.current_limits, ev)) is not None and "min_current" not in values:
                values.update(min_current=min(rng[0]), max_current=max(rng[1]))
        elif self.profile == "opev":
            values.update(vehicle_connected=False, charge_state="unplugged")
        return ElliStatus(**values)

    def _parameters(self, evse: RemoteEntity) -> tuple[ClientFeature, list[dict], list[dict]]:
        f = ClientFeature("ElectricalConnection", self.service.entities[0], evse)
        params = f.items("electricalConnectionParameterDescriptionListData",
                         "electricalConnectionParameterDescriptionData")
        sets = f.items("electricalConnectionPermittedValueSetListData", "electricalConnectionPermittedValueSetData")
        return f, params, sets

    def _range(self, sets: list[dict], parameter_id: Any) -> tuple[float, float] | None:
        for item in sets:
            if item.get("parameterId") != parameter_id:
                continue
            for value_set in item.get("permittedValueSet") or []:
                for rng in value_set.get("range") or []:
                    low, high = scaled_value(rng.get("min")), scaled_value(rng.get("max"))
                    if low is not None and high is not None:
                        return low, high
        return None

    def _power_range(self, evse: RemoteEntity) -> tuple[float, float] | None:
        _f, params, sets = self._parameters(evse)
        for p in params:
            if p.get("scopeType") in ("acPowerTotal",) and (rng := self._range(sets, p.get("parameterId"))):
                return rng
        return None

    def _current_range(self, evse: RemoteEntity) -> tuple[float, float] | None:
        _f, _params, sets = self._parameters(evse)
        for item in sets:
            rng = self._range(sets, item.get("parameterId"))
            if rng and rng[1] <= 80:  # amps, not watts
                return rng
        return None

    # --- control ------------------------------------------------------------------------------

    async def set_power_limit(self, watts: float, duration: float | None = None) -> None:
        """Limit the charging power (W). With ``duration`` (s) the limit is lifted afterwards.

        Elli firmware ignores limits that carry a duration, so the limit is sent
        without one and lifted by this client when the time is up (also after a
        restart). If this client is gone, the wallbox falls back to its failsafe
        limit after missing heartbeats.
        """
        evse = self._require_evse()
        if self.profile == "opev":
            await self.set_current_limit(watts / NOMINAL_VOLTAGE / max(self._phases(), 1), duration)
            return
        await self._lpc.write_consumption_limit(evse, LoadLimit(watts, True))
        self._schedule_release(duration)

    async def clear_power_limit(self) -> None:
        """Lift the limit (charge with the maximum power)."""
        evse = self._require_evse()
        self._schedule_release(None)
        if self.profile == "opev":
            ev = self._ev()
            if ev is not None:
                _low, high, _default = self._opev.current_limits(ev)
                await self._opev.write_load_control_limits(
                    ev, [PhaseLimit(p, h, is_active=False) for p, h in zip("abc", high)])
            return
        # Elli firmware rejects isLimitActive=false unless the value is 0
        desc = self._lpc._limit_description(evse)
        feature = ClientFeature("LoadControl", self.service.entities[0], evse)
        await feature.write("loadControlLimitListData", {"loadControlLimitData": [
            {"limitId": desc["limitId"], "isLimitActive": False, "value": scaled_number(0)}]},
            [partial_filter()])

    async def set_current_limit(self, amps: float, duration: float | None = None) -> None:
        """Limit the charging current per phase (A).

        First generation: per phase current limits (OPEV), needs a connected EV.
        Elli Charger 2: converted to a power limit (A x 230 V x phases).
        """
        if self.profile == "opev":
            ev = self._ev()
            if ev is None:
                raise ElliError("no vehicle connected")
            await self._opev.write_load_control_limits(ev, [PhaseLimit(p, amps) for p in "abc"])
            self._schedule_release(duration)
            return
        await self.set_power_limit(amps * NOMINAL_VOLTAGE * self._phases(), duration)

    async def set_failsafe(self, watts: float | None = None, duration: float | None = None) -> None:
        """Set the failsafe power limit (W) and/or its minimum duration (s, 2 to 24 h)."""
        evse = self._require_evse()
        if watts is not None:
            await self._lpc.write_failsafe_consumption_limit(evse, watts)
        if duration is not None:
            await self._lpc.write_failsafe_duration_minimum(evse, duration)

    def _phases(self) -> int:
        evse = self._evse()
        if evse is None:
            return 3
        try:
            f = ClientFeature("ElectricalConnection", self.service.entities[0], evse)
        except DataNotAvailable:
            return 3
        for item in f.items("electricalConnectionDescriptionListData", "electricalConnectionDescriptionData"):
            if item.get("acConnectedPhases"):
                return int(item["acConnectedPhases"])
        return 3

    # --- limit release ----------------------------------------------------------------------

    def _schedule_release(self, duration: float | None) -> None:
        if self._release is not None:
            self._release.cancel()
            self._release = None
        self._state.limit_until = time.time() + duration if duration else None
        self._save_state()
        if duration:
            self._release = asyncio.create_task(self._release_later(self._state.limit_until))

    async def _release_later(self, until: float) -> None:
        await asyncio.sleep(max(until - time.time(), 0))
        while True:
            await self.wait_connected()
            try:
                await self.clear_power_limit()
                return
            except (TimeoutError, ElliError, DataNotAvailable, ShipError) as err:
                _LOGGER.debug("lifting the limit failed (%s), retrying", err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("lifting the limit failed: %s", err)
            await asyncio.sleep(2)

    def _resume_release(self) -> None:
        """After a restart: lift a limit whose time is up, or keep waiting for it."""
        until = self._state.limit_until
        if until and (self._release is None or self._release.done()):
            self._release = asyncio.create_task(self._release_later(until))

    def _load_state(self) -> _State:
        path = self.state_dir / "state.json"
        try:
            data = json.loads(path.read_text())
            return _State(limit_until=data.get("limit_until"))
        except (OSError, ValueError):
            return _State()

    def _save_state(self) -> None:
        with contextlib.suppress(OSError):
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "state.json").write_text(json.dumps({"limit_until": self._state.limit_until}))
