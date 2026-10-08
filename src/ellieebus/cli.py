"""elli-eebus command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from pyeebus.ship import Identity, TrustStore, is_ski_valid
from pyeebus.ship.mdns import ShipMdns

from .client import DEFAULT_PORT, ElliEebus, ElliStatus

CONFIG_DIR = Path.home() / ".config" / "elli-eebus"


def _print_status(status: ElliStatus) -> None:
    if not status.connected:
        print("not connected")
        return
    for key, value in asdict(status).items():
        if value is None:
            continue
        if key == "power_limit_until":
            value = time.strftime("%H:%M:%S", time.localtime(value))
        print(f"  {key:20} {value}")


async def discover(seconds: float) -> int:
    mdns = ShipMdns()
    await mdns.browse()
    print(f"Looking for EEBUS devices for {seconds:g} s ...")
    await asyncio.sleep(seconds)
    await mdns.close()
    for s in mdns.services.values():
        print(f"- {s.brand} {s.model} ({s.device_type}) at {', '.join(s.addresses) or s.host}:{s.port}")
        print(f"    SKI {s.ski}")
    if not mdns.services:
        print("No EEBUS devices found.")
    return 0 if mdns.services else 1


async def run(args: argparse.Namespace) -> int:
    elli = ElliEebus(args.ski, state_dir=args.config, host=args.host, port=args.port, local_port=args.local_port)
    print(f"Our SKI: {elli.our_ski} - pair 'elli-eebus' in the wallbox UI "
          "(Connections > HEMS connection > found EEBUS devices) if not done yet.")
    await elli.start()
    try:
        try:
            await elli.wait_connected(args.timeout)
        except TimeoutError:
            print("The wallbox did not connect. Paired? Same network?")
            return 1
        await asyncio.sleep(2)  # let the first data arrive
        if args.cmd == "status":
            _print_status(elli.status())
            if args.watch:
                elli.add_listener(lambda s: (print(), _print_status(s)))
                await asyncio.Event().wait()
        elif args.cmd == "set-limit":
            await elli.set_power_limit(args.watts, args.duration)
            print(f"Limit {args.watts:g} W set.")
            if args.duration:
                print(f"Lifting it in {args.duration:g} s (Ctrl+C keeps it; it is lifted on the next run).")
                await asyncio.sleep(args.duration + 2)
                print("Limit lifted.")
        elif args.cmd == "clear-limit":
            await elli.clear_power_limit()
            print("Limit lifted.")
        elif args.cmd == "set-failsafe":
            await elli.set_failsafe(args.watts, args.duration)
            print("Failsafe values written.")
            await asyncio.sleep(1)
            _print_status(elli.status())
    finally:
        await elli.stop()
    return 0


async def simulate(args: argparse.Namespace) -> int:
    from .simulator import SimulatedElli

    state = args.config / "simulator"
    trust = TrustStore.load(state / "trust.json")
    sim = SimulatedElli(Identity.load_or_create(state, "simulated-elli"), port=args.port, trust=trust)
    if args.trust:
        sim.service.trust(args.trust)
    await sim.start()
    print(f"Simulated Elli Charger 2 on port {sim.service.node.port}, SKI {sim.ski}")
    print("Commands: demand <W> (power a car would draw), status, quit")
    loop = asyncio.get_running_loop()
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if not line or line == "quit":
            break
        cmd, *rest = line.split()
        if cmd == "demand" and rest:
            sim.set_demand(float(rest[0]))
        if cmd in ("demand", "status"):
            value, active = sim.limit
            print(f"power {sim.power:g} W, limit {value:g} W {'active' if active else 'inactive'}")
    await sim.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="elli-eebus", description="Elli Charger over EEBUS")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("discover", help="list EEBUS devices on the network")
    sp.add_argument("--seconds", type=float, default=5)

    def device(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("ski", help="SKI of the wallbox (see discover or the wallbox UI)")
        p.add_argument("--host", help="IP of the wallbox (default: find it via mDNS)")
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--local-port", type=int, default=4712)
        p.add_argument("--config", type=Path, default=CONFIG_DIR)
        p.add_argument("--timeout", type=float, default=60)
        return p

    device("status", "show the wallbox status").add_argument("--watch", action="store_true")
    p = device("set-limit", "limit the charging power")
    p.add_argument("watts", type=float)
    p.add_argument("--duration", type=float, help="seconds, then the limit is lifted")
    device("clear-limit", "lift the power limit")
    p = device("set-failsafe", "set the failsafe power and/or minimum duration")
    p.add_argument("--watts", type=float)
    p.add_argument("--duration", type=float, help="seconds (7200 to 86400)")
    sp = sub.add_parser("simulate", help="run a simulated Elli Charger 2")
    sp.add_argument("--port", type=int, default=4711)
    sp.add_argument("--trust", help="SKI of the energy manager to accept")
    sp.add_argument("--config", type=Path, default=CONFIG_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")
    if getattr(args, "ski", None) and not is_ski_valid(args.ski):
        parser.error("SKI must be 40 hex characters")
    try:
        if args.cmd == "discover":
            return asyncio.run(discover(args.seconds))
        if args.cmd == "simulate":
            return asyncio.run(simulate(args))
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
