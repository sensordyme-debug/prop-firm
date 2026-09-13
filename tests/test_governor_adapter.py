"""Tests for the I/O adapter. No network, no credentials, no live suite.

These use the real SDK model classes rather than hand-rolled stubs, so the
tests fail if project-x-py changes the shape the adapter depends on.

Async helpers are driven with ``asyncio.run`` to avoid adding an async test
plugin for three coroutines.
"""

from __future__ import annotations

import asyncio
from datetime import UTC

import pytest
from project_x_py.models import Account, Instrument, Position

from governor_adapter import (
    KILL_FILE_NAME,
    MNQ_POINT_VALUE,
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
    assert MNQ_POINT_VALUE == 2.00
    assert point_value(MNQ) == MNQ_POINT_VALUE, "constant and derivation must agree"


def test_two_mnq_down_ten_points_is_a_forty_dollar_loss():
    """The exact figure the tick_value default would get wrong.

    2 MNQ x 10 points x $2.00 = -$40.00. With the SDK's default tick_value of
    1.0 this reads as -$20.00, so a real $40 loss would look like $20 and the
    loss trip-wire would let the position run to twice the intended drawdown.
    """
    v = asyncio.run(value_positions([long_mnq(size=2, avg=20_000.0)],
                                    FakePrice(19_990.0), MNQ))
    assert v.unrealised_pnl == pytest.approx(-40.00)
    assert v.unrealised_pnl != pytest.approx(-20.00), "tick_value default leaked in"


def test_the_default_tick_value_would_halve_the_loss():
    """Pins the trap itself, so the reason for the constant stays visible."""
    position = long_mnq(size=2, avg=20_000.0)
    assert position.unrealized_pnl(19_990.0) == pytest.approx(-20.00)  # default 1.0
    assert position.unrealized_pnl(19_990.0, MNQ_POINT_VALUE) == pytest.approx(-40.00)


def test_point_value_rejects_mnq_geometry_that_does_not_match_the_constant():
    """If the gateway ever reports different MNQ geometry, refuse to value."""
    altered = Instrument(
        id=MNQ.id, name="MNQ", description="", tickSize=0.25, tickValue=1.00,
        activeContract=True,
    )
    with pytest.raises(SnapshotUnavailable, match="point value"):
        point_value(altered)


def test_no_call_site_omits_the_tick_value():
    """Structural guard: every unrealized_pnl call must pass the point value.

    A source scan rather than a behavioural test, because the failure mode is
    someone adding a new call site that silently takes the 1.0 default. Reading
    right and being wrong is exactly what this catches.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "src"
    call_sites = 0
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "unrealized_pnl"):
                continue
            call_sites += 1
            passes_value = len(node.args) >= 2 or any(
                kw.arg == "tick_value" for kw in node.keywords
            )
            assert passes_value, (
                f"{path.name}:{node.lineno} calls unrealized_pnl without an "
                "explicit tick value; it would default to 1.0 and halve the P&L"
            )
    assert call_sites >= 1, "expected at least one call site to guard"


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


def _src_dir():
    import pathlib

    return pathlib.Path(__file__).parent.parent / "src"


def _parse(path):
    import ast

    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_governor_module_imports_no_sdk():
    """The purity contract, enforced rather than documented.

    If governor.py ever grows an SDK import it stops being unit-testable
    without credentials, and the trip-wires stop being provable offline.

    Checked against the AST, not the text: a substring scan also matches
    prose, and a docstring that *documents* an anti-pattern is not a use of
    it. A test that cannot tell those apart trains people to ignore it.
    """
    import ast

    tree = _parse(_src_dir() / "governor.py")

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    for forbidden in ("project_x_py", "aiohttp", "httpx", "requests"):
        assert not any(forbidden in name for name in imported), (
            f"governor.py must not import {forbidden}"
        )

    clock_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("now", "utcnow", "today"):
            clock_calls.append(f"line {node.lineno}: .{func.attr}()")
    assert not clock_calls, (
        "governor.py must not read the clock; `now` arrives on the snapshot. "
        f"Found {clock_calls}"
    )


def test_no_source_file_localises_with_replace_tzinfo():
    """Ban ``.replace(tzinfo=...)`` across src/.

    ``datetime.now().replace(tzinfo=ET)`` produces a datetime that is aware,
    passes every ``_require_aware`` check, and is silently wrong by the host's
    UTC offset -- on a machine not set to ET, every session boundary shifts by
    hours with no error anywhere.

    Aware is necessary but not sufficient; correctly localised is the real
    requirement, and it cannot be checked from the value. So it is enforced at
    the source level instead: build instants with ``now_utc()`` and convert
    with ``astimezone``, which cannot express the bug.
    """
    import ast

    offenders = []
    for path in sorted(_src_dir().glob("*.py")):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "replace"):
                continue
            if any(kw.arg == "tzinfo" for kw in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "use now_utc() and astimezone() instead of .replace(tzinfo=...): "
        f"{offenders}"
    )


def test_adapter_clock_reads_utc_not_local_time():
    """now_utc must return a UTC instant, not a locally-stamped one."""

    from governor_adapter import now_utc

    value = now_utc()
    assert value.tzinfo is not None
    assert value.utcoffset() == UTC.utcoffset(None)


def test_connection_test_never_imports_an_order_capable_object():
    """Gate 1 must be structurally incapable of transmitting an order.

    It is safe only because it is built on ProjectX, whose public surface has
    no place/submit/modify/cancel method. Importing TradingSuite or any order
    manager would silently reintroduce that capability.
    """
    import ast
    import pathlib

    path = pathlib.Path(__file__).parent.parent / "src" / "connection_test.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported += [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]

    forbidden = ("TradingSuite", "OrderManager", "order_manager", "managed_trade",
                 "OrderChain", "order_chain")
    for name in imported:
        for bad in forbidden:
            assert bad not in name, f"connection_test.py imports {name!r}"

    assert any("ProjectX" in n for n in imported), "expected the read-only client"


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
