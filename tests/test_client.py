"""ElliEebus against the simulated Elli Charger 2 and a first generation style EVSE."""

from __future__ import annotations

import asyncio

import pytest
from pyeebus.ship import Identity
from pyeebus.simulator import SimulatedEVSE
from pyeebus.usecases import LPC, LoadLimit

from ellieebus import ElliEebus
from ellieebus.simulator import SimulatedElli

NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


async def wait_for(condition, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if result := condition():
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def client(ski: str, port: int, state_dir) -> ElliEebus:
    return ElliEebus(ski, state_dir=state_dir, host="127.0.0.1", port=port, local_port=0, announce=False)


def trust_each_other(server_service, elli: ElliEebus) -> None:
    # pyeebus servers need the client's certificate (Python ssl limitation)
    server_service.trust(elli.our_ski, elli.service.identity.cert_pem)


@pytest.fixture
async def elli2(tmp_path):
    sim = SimulatedElli(Identity.create("elli"), port=0, **NODE)
    await sim.start()
    elli = client(sim.ski, sim.service.node.port, tmp_path)
    trust_each_other(sim.service, elli)
    await elli.start()
    await elli.wait_connected(5)
    yield sim, elli
    await elli.stop()
    await sim.stop()


async def test_status_of_elli_charger_2(elli2):
    _sim, elli = elli2
    status = await wait_for(lambda: (s := elli.status()).failsafe_power is not None and s.heartbeat_ok
                            and s.max_power is not None and s)
    assert status.connected and status.profile == "lpc"
    assert (status.brand, status.model, status.serial) == ("Elli", "EVSE", "00099999")
    assert status.operating_state == "normalOperation"
    assert status.power == 0
    assert (status.power_limit, status.power_limit_active) == (0, False)
    assert (status.failsafe_power, status.failsafe_duration) == (22000, 7200)
    assert status.nominal_max_power == 11040
    assert (status.min_power, status.max_power) == (4140, 11040)
    assert (status.min_current, status.max_current) == (6, 16)
    assert status.vehicle_connected is None  # the Elli Charger 2 does not report the car


async def test_power_limit_with_duration_is_lifted_by_the_client(elli2):
    sim, elli = elli2
    await wait_for(lambda: elli.status().power_limit is not None)
    updates = []
    elli.add_listener(updates.append)
    sim.set_demand(11000)
    await wait_for(lambda: elli.status().power == 11000)

    await elli.set_power_limit(4200, duration=1)
    assert sim.limit == (4200, True)
    await wait_for(lambda: elli.status().power == 4200)
    assert elli.status().power_limit_until is not None
    await wait_for(lambda: sim.limit == (0, False), timeout=5)  # lifted the Elli way: inactive, 0 W
    await wait_for(lambda: elli.status().power == 11000)
    assert any(u.power == 4200 for u in updates)


async def test_simulator_reproduces_the_firmware_bugs(elli2):
    sim, elli = elli2
    evse = await wait_for(lambda: elli._evse())
    lpc: LPC = elli._lpc
    await wait_for(lambda: lpc.consumption_limit(evse))
    with pytest.raises(TimeoutError):  # a limit with duration gets no answer
        await lpc.write_consumption_limit(evse, LoadLimit(4200, True, duration=60), timeout=1)
    assert sim.limit == (0, False)
    await lpc.write_consumption_limit(evse, LoadLimit(4200, True))
    with pytest.raises(Exception, match="Write failed"):  # lifting with a value is rejected
        await lpc.write_consumption_limit(evse, LoadLimit(4200, False))
    await elli.clear_power_limit()
    assert sim.limit == (0, False)


async def test_failsafe_and_current_limit(elli2):
    sim, elli = elli2
    await wait_for(lambda: elli.status().failsafe_power is not None)
    await elli.set_failsafe(watts=6000, duration=3 * 3600)
    await wait_for(lambda: elli.status().failsafe_power == 6000)
    assert elli.status().failsafe_duration == 3 * 3600
    await elli.set_current_limit(10)  # 10 A x 230 V x 3 phases
    assert sim.limit == (6900, True)
    await elli.clear_power_limit()


async def test_pending_release_survives_a_restart(tmp_path):
    sim = SimulatedElli(Identity.create("elli"), port=0, **NODE)
    await sim.start()
    try:
        elli = client(sim.ski, sim.service.node.port, tmp_path)
        trust_each_other(sim.service, elli)
        await elli.start()
        await elli.wait_connected(5)
        await wait_for(lambda: elli.status().power_limit is not None)
        await elli.set_power_limit(5000, duration=1)
        await elli.stop()  # gone before the limit ends
        assert sim.limit == (5000, True)
        await asyncio.sleep(1.2)

        again = client(sim.ski, sim.service.node.port, tmp_path)  # same identity and state
        await again.start()
        await wait_for(lambda: sim.limit == (0, False), timeout=10)
        await again.stop()
    finally:
        await sim.stop()


async def test_first_generation_current_limits(tmp_path):
    evse = SimulatedEVSE(Identity.create("gen1"), port=0, **NODE)
    await evse.start()
    elli = client(evse.service.ski, evse.service.node.port, tmp_path)
    trust_each_other(evse.service, elli)
    await elli.start()
    try:
        await elli.wait_connected(5)
        evse.plug_in()
        status = await wait_for(lambda: (s := elli.status()).current_limits and s)
        assert status.profile == "opev"
        assert status.vehicle_connected and status.charge_state == "active"
        assert status.current_limits == (16, 16, 16)
        assert (status.min_current, status.max_current) == (6, 16)

        await elli.set_current_limit(10)
        assert evse.limits["obligation"] == [10, 10, 10]
        await elli.set_power_limit(3 * 230 * 8)  # converted to 8 A per phase
        assert evse.limits["obligation"] == [8, 8, 8]
        evse.set_charging(8, 1500)
        await wait_for(lambda: elli.status().energy == 1500)
        assert elli.status().energy_scope == "session"
        assert elli.status().currents == (8, 8, 8)
        await elli.clear_power_limit()
        await wait_for(lambda: all(not lim.is_active for lim in elli._opev.load_control_limits(elli._ev())))
    finally:
        await elli.stop()
        await evse.stop()
