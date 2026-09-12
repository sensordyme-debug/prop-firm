"""Tests for the I/O adapter. No network, no credentials, no live suite.

These use the real SDK model classes rather than hand-rolled stubs, so the
tests fail if project-x-py changes the shape the adapter depends on.

Async helpers are driven with ``asyncio.run`` to avoid adding an async test
plugin for three coroutines.
"""

from __future__ import annotations

import asyncio

import pytest
from project_x_py.models import Account, Instrument, Position

from governor_adapter import (
    KILL_FILE_NAME,
    SnapshotUnavailable,
    build_snapshot,
    kill_switch_present,
    mll_floor_from_env,
    point_value,
    value_positions,
)

# MNQ: 0.25 tick, $0.50 a tick -> $2.00 a point.
MNQ = Instrument(
    id="CON.F.US.MNQ.Z25",
    name="MNQ",
    description="Micro E-mini Nasdaq-100",
    tickSize=0.25,
    tickValue=0.50,
    activeContract=True,
)

ACCOUNT = Account(
    id=1, name="Combine", balance=50_000.0, canTrade=True, isVisible=True,
    simulated=True,
)


def long_mnq(size: int = 2, avg: float = 20_000.0) -> Position:
    return Position(
        id=1, accountId=1, contractId=MNQ.id, creationTimestamp="2026-09-16T13:31:00Z",
        type=1, size=size, averagePrice=avg,  # type 1 == LONG
    )


def short_mnq(size: int = 2, avg: float = 20_000.0) -> Position:
    return Position(
        id=2, accountId=1, contractId=MNQ.id, creationTimestamp="2026-09-16T13:31:00Z",
        type=2, size=size, averagePrice=avg,  # type 2 == SHORT
    )


class FakePrice:
    def __init__(self, price: float | None) -> None:
        self._price = price

    async def get_current_price(self) -> float | None:
        return self._price


# ---------------------------------------------------------------------------
# Contract value
# ---------------------------------------------------------------------------


def test_point_value_for_mnq_is_two_dollars():
    """Position.unrealized_pnl defaults tick_value to 1.0, which would halve
    every MNQ P&L figure. The adapter must always pass 2.0."""
    assert point_value(MNQ) == 2.0


def test_point_value_rejects_unusable_tick_geometry():
    broken = Instrument(
        id="X", name="X", description="", tickSize=0.0, tickValue=0.5,
        activeContract=True,
    )
    with pytest.raises(SnapshotUnavailable, match="tick geometry"):
        point_value(broken)


# ---------------------------------------------------------------------------
# Valuing the open book
# ---------------------------------------------------------------------------


def test_flat_book_values_to_zero_without_needing_a_price():
    v = asyncio.run(value_positions([], FakePrice(None), MNQ))
    assert (v.size, v.unrealised_pnl) == (0, 0.0)


def test_open_position_without_a_price_refuses_to_produce_a_snapshot():
    """An unknown net liq must stop trading, never be guessed as balance."""
    with pytest.raises(SnapshotUnavailable, match="no current price"):
        asyncio.run(value_positions([long_mnq()], FakePrice(None), MNQ))


def test_long_position_pnl_uses_the_point_value():
    """2 MNQ long, 10 points up = 2 * 10 * $2 = $40."""
    v = asyncio.run(value_positions([long_mnq()], FakePrice(20_010.0), MNQ))
    assert v.size == 2
    assert v.unrealised_pnl == pytest.approx(40.0)


def test_short_position_reports_negative_size_and_profits_when_price_falls():
    v = asyncio.run(value_positions([short_mnq()], FakePrice(19_990.0), MNQ))
    assert v.size == -2
    assert v.unrealised_pnl == pytest.approx(40.0)


def test_short_position_loses_when_price_rises():
    v = asyncio.run(value_positions([short_mnq()], FakePrice(20_010.0), MNQ))
    assert v.unrealised_pnl == pytest.approx(-40.0)


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def build(positions, price, **kw):
    return asyncio.run(
        build_snapshot(
            account=ACCOUNT,
            positions=positions,
            price_source=FakePrice(price),
            instrument=MNQ,
            mll_floor=kw.pop("mll_floor", 48_000.0),
            session_start_balance=kw.pop("session_start_balance", 50_000.0),
            **kw,
        )
    )


def test_snapshot_when_flat_has_net_liq_equal_to_balance():
    s = build([], None)
    assert s.net_liq == 50_000.0 == s.balance
    assert s.open_position_size == 0
    assert s.now.tzinfo is not None


def test_snapshot_net_liq_includes_open_loss_while_balance_does_not():
    """The distinction CLAUDE.md constraint 3 turns on: balance is unchanged,
    net liq is down $400, and the governor must see the $400."""
    s = build([long_mnq()], 19_900.0)
    assert s.balance == 50_000.0
    assert s.net_liq == pytest.approx(49_600.0)
    assert s.open_position_size == 2


def test_snapshot_propagates_the_refusal_when_price_is_missing():
    with pytest.raises(SnapshotUnavailable):
        build([long_mnq()], None)


def test_snapshot_carries_the_kill_switch_state(tmp_path):
    s = build([], None, root=tmp_path)
    assert s.kill_switch_active is False

    (tmp_path / KILL_FILE_NAME).write_text("stop")
    s2 = build([], None, root=tmp_path)
    assert s2.kill_switch_active is True


# ---------------------------------------------------------------------------
# Kill switch and MLL floor
# ---------------------------------------------------------------------------


def test_kill_switch_detects_the_file(tmp_path):
    assert kill_switch_present(tmp_path) is False
    (tmp_path / KILL_FILE_NAME).write_text("")
    assert kill_switch_present(tmp_path) is True


def test_kill_switch_ignores_a_directory_of_the_same_name(tmp_path):
    (tmp_path / KILL_FILE_NAME).mkdir()
    assert kill_switch_present(tmp_path) is False


def test_mll_floor_refuses_to_default_to_zero(monkeypatch):
    """A zero floor would make the buffer check pass unconditionally and
    silently disable the only permanent-failure guard."""
    monkeypatch.delenv("MLL_FLOOR", raising=False)
    with pytest.raises(SnapshotUnavailable, match="MLL_FLOOR"):
        mll_floor_from_env()


def test_mll_floor_reads_the_environment(monkeypatch):
    monkeypatch.setenv("MLL_FLOOR", "48250.5")
    assert mll_floor_from_env() == 48_250.5


def test_mll_floor_accepts_an_explicit_default(monkeypatch):
    monkeypatch.delenv("MLL_FLOOR", raising=False)
    assert mll_floor_from_env(default=48_000.0) == 48_000.0


# ---------------------------------------------------------------------------
# Architectural guard
# ---------------------------------------------------------------------------


def test_governor_module_imports_no_sdk():
    """The purity contract, enforced rather than documented.

    If governor.py ever grows an SDK import it stops being unit-testable
    without credentials, and the trip-wires stop being provable offline.
    """
    import pathlib

    source = (pathlib.Path(__file__).parent.parent / "src" / "governor.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("project_x_py", "aiohttp", "httpx", "requests"):
        assert forbidden not in source, f"governor.py must not import {forbidden}"
    for forbidden in ("datetime.now(", "time.time(", ".utcnow("):
        assert forbidden not in source, f"governor.py must not read the clock: {forbidden}"


def test_no_order_transmission_path_exists_yet():
    """Build order gate: no order may be transmittable before the executor and
    the DRY_RUN rehearsal are built."""
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "src"
    for path in src.glob("*.py"):
        body = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        )
        # Strip docstrings crudely: only call sites matter, and calls end in "(".
        for forbidden in ("place_bracket_order(", "place_order(", "close_all_positions(",
                          "close_position_by_contract(", "submit_order("):
            assert forbidden not in body, f"{path.name} contains a call to {forbidden}"
