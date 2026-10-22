import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import DB  # noqa: E402
from app.engine import Engine  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def db():
    return DB(":memory:")


@pytest.fixture
def router(db):
    cur = db.execute(
        "INSERT INTO routers(name, host, port, username, password, updated_at) VALUES(?,?,?,?,?,?)",
        ("Core", "10.0.0.1", 8728, "u", "p", time.time()),
    )
    return {"id": cur.lastrowid, "name": "Core", "host": "10.0.0.1"}


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def engine(db, notifier):
    return Engine(db, notifier)


class Sim:
    """Genera lecturas con contadores coherentes a una velocidad dada."""

    def __init__(self, engine, router, start=1_000_000.0):
        self.engine = engine
        self.router = router
        self.t = start
        self.counters: dict[str, list[int]] = {}

    def step(self, dt, rx_mbps=0.0, tx_mbps=0.0, name="ether1", running=True, disabled=False, comment="Uplink"):
        self.t += dt
        c = self.counters.setdefault(name, [0, 0])
        c[0] += int(rx_mbps * 1e6 * dt / 8)
        c[1] += int(tx_mbps * 1e6 * dt / 8)
        row = {
            "name": name,
            "type": "ether",
            "running": running,
            "disabled": disabled,
            "rx-byte": c[0],
            "tx-byte": c[1],
            "comment": comment,
        }
        self.engine.process_poll(self.router, [row], now=self.t)

    def run(self, seconds, rx_mbps=0.0, tx_mbps=0.0, every=10, **kw):
        for _ in range(int(seconds // every)):
            self.step(every, rx_mbps, tx_mbps, **kw)


@pytest.fixture
def sim(engine, router):
    return Sim(engine, router)


def active_kinds(db):
    return sorted(r["kind"] for r in db.query("SELECT kind FROM alerts WHERE resolved_at IS NULL"))
