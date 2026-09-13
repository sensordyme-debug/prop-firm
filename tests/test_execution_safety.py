"""Hard execution invariants (STEP 20). These must never be violated.

Most tests describe what the system does. These describe what it is
structurally incapable of doing, which is a stronger claim and the only kind
worth making about order transmission.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime

import pytest

from config import AppConfig, ConfigError, ExecutionMode
from execution.broker import (
    ExecutionBroker,
    ExecutionCapability,
    ExecutionNotEnabled,
    MarketDataBroker,
    grant_execution,
)
from execution.models import (
    OrderIntent,
    OrderState,
    OrderStateError,
    OrderType,
    Side,
    WorkingOrder,
    transition,
)

SRC = pathlib.Path(__file__).parent.parent / "src"


# ===========================================================================
# Transmission is impossible
# ===========================================================================


def test_execution_capability_cannot_be_granted():
    """The single most important test in the repository."""
    with pytest.raises(ExecutionNotEnabled, match="not implemented"):
        grant_execution()


def test_capability_refuses_unless_every_precondition_holds():
    now = datetime.now(UTC)
    all_true = dict(
        credentials_valid=True, account_identified=True,
        reconciliation_passed=True, market_data_fresh=True,
        governor_healthy=True, explicitly_enabled_by_human=True,
    )
    ExecutionCapability(**all_true, granted_at=now)  # must not raise

    for field_name in all_true:
        weakened = {**all_true, field_name: False}
        with pytest.raises(ExecutionNotEnabled, match=field_name):
            ExecutionCapability(**weakened, granted_at=now)


def test_the_readonly_adapter_is_not_an_execution_broker():
    """A type-level guarantee, not a promise in a docstring."""
    from brokers.projectx import ProjectXReadOnlyBroker

    broker = ProjectXReadOnlyBroker(config=AppConfig())
    assert isinstance(broker, MarketDataBroker)
    assert not isinstance(broker, ExecutionBroker)


def test_the_readonly_adapter_has_no_order_methods_at_all():
    from brokers.projectx import ProjectXReadOnlyBroker

    broker = ProjectXReadOnlyBroker(config=AppConfig())
    for forbidden in ("place_order", "place_bracket_order", "cancel_order",
                      "flatten", "submit", "modify_order"):
        assert not hasattr(broker, forbidden), f"adapter exposes {forbidden}"


def test_no_source_file_calls_an_sdk_order_method():
    """Belt and braces: nothing in src/ calls a transmitting SDK method."""
    forbidden = (
        "place_order", "place_bracket_order", "close_position_direct",
        "close_all_positions", "close_position_by_contract",
        "partially_close_position", "cancel_order", "modify_order",
    )
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            if name in forbidden:
                offenders.append(f"{path.name}:{node.lineno} -> {name}")
    assert not offenders, f"order-transmitting calls found: {offenders}"


# ===========================================================================
# Configuration cannot enable execution
# ===========================================================================


def test_dry_run_defaults_true_and_live_defaults_false():
    c = AppConfig()
    assert c.dry_run is True
    assert c.live_trading_enabled is False
    assert c.execution_mode.may_transmit is False


def test_live_trading_cannot_be_enabled_by_configuration():
    """Even a fully deliberate .env cannot reach EXECUTION."""
    with pytest.raises(ConfigError, match="NOT IMPLEMENTED"):
        AppConfig(live_trading_enabled=True, dry_run=False)


def test_half_set_execution_flags_are_rejected_as_incoherent():
    with pytest.raises(ConfigError, match="incoherent"):
        AppConfig(live_trading_enabled=True, dry_run=True)


def test_no_reachable_execution_mode_may_transmit():
    for creds in ((None, None), ("k", "u")):
        cfg = AppConfig(api_key=creds[0], username=creds[1])
        assert cfg.execution_mode.may_transmit is False


def test_ambiguous_boolean_env_values_are_rejected(monkeypatch):
    """'maybe' must not quietly become False on a flag that gates orders."""
    from config import load_config

    monkeypatch.setenv("DRY_RUN", "maybe")
    with pytest.raises(ConfigError, match="not a boolean"):
        load_config(load_dotenv_file=False)


# ===========================================================================
# Order intents cannot be unsafe
# ===========================================================================


def intent(**kw) -> OrderIntent:
    base = dict(
        symbol="MNQ", contract_id="CON.F.US.MNQ.Z26", side=Side.BUY, size=2,
        order_type=OrderType.LIMIT, limit_price=20_000.0,
        stop_loss=19_990.0, take_profit=20_020.0,
    )
    return OrderIntent(**{**base, **kw})


def test_an_intent_without_a_stop_is_rejected():
    """CLAUDE.md constraint 1: a naked position must never exist."""
    with pytest.raises(OrderStateError, match="naked position"):
        intent(stop_loss=None).validate()


def test_a_stop_on_the_wrong_side_is_rejected():
    with pytest.raises(OrderStateError, match="at or above entry"):
        intent(stop_loss=20_010.0).validate()
    with pytest.raises(OrderStateError, match="at or below entry"):
        intent(side=Side.SELL, stop_loss=19_990.0, take_profit=19_950.0).validate()


def test_a_target_on_the_wrong_side_of_the_stop_is_rejected():
    with pytest.raises(OrderStateError, match="not above stop"):
        intent(take_profit=19_980.0).validate()


def test_zero_or_negative_size_is_rejected():
    for bad in (0, -2):
        with pytest.raises(OrderStateError, match="size must be positive"):
            intent(size=bad).validate()


def test_a_missing_contract_id_is_rejected():
    with pytest.raises(OrderStateError, match="contract_id"):
        intent(contract_id="").validate()


def test_a_valid_intent_passes():
    intent().validate()
    intent(side=Side.SELL, stop_loss=20_010.0, take_profit=19_980.0).validate()


# ===========================================================================
# Order state machine
# ===========================================================================


def order(state: OrderState) -> WorkingOrder:
    return WorkingOrder(
        order_id="1", contract_id="C", side=Side.BUY,
        order_type=OrderType.MARKET, size=2, state=state,
    )


def test_unknown_is_treated_as_possibly_live():
    """A timeout does not mean the order failed."""
    assert OrderState.UNKNOWN.is_live is True
    assert OrderState.UNKNOWN.requires_reconciliation is True
    assert OrderState.UNKNOWN.is_terminal is False


def test_unknown_can_only_be_left_via_a_state_the_broker_reported():
    for reported in (OrderState.FILLED, OrderState.CANCELLED,
                     OrderState.REJECTED, OrderState.ACCEPTED):
        transition(order(OrderState.UNKNOWN), reported)

    # Not back to a state that would imply we re-submitted.
    for illegal in (OrderState.SUBMITTED, OrderState.CREATED,
                    OrderState.VALIDATED, OrderState.CANCEL_REQUESTED):
        with pytest.raises(OrderStateError, match="illegal transition"):
            transition(order(OrderState.UNKNOWN), illegal)


def test_terminal_states_are_escapable_by_nothing():
    for terminal in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED):
        assert terminal.is_terminal
        for target in OrderState:
            with pytest.raises(OrderStateError):
                transition(order(terminal), target)


def test_a_submitted_order_may_time_out_into_unknown():
    transition(order(OrderState.SUBMITTED), OrderState.UNKNOWN)


def test_filled_orders_are_not_live():
    assert OrderState.FILLED.is_live is False
    assert OrderState.PARTIALLY_FILLED.is_live is True


# ===========================================================================
# Secrets never leak
# ===========================================================================


def test_redacted_config_contains_no_secret_values():
    cfg = AppConfig(api_key="SUPER-SECRET-KEY", username="trader1")
    rendered = str(cfg.redacted())
    assert "SUPER-SECRET-KEY" not in rendered
    assert "trader1" not in rendered
    assert cfg.redacted()["api_key"] == "<set>"
    assert cfg.redacted()["credentials_present"] is True


def test_account_state_redaction_omits_the_name():
    from execution.models import AccountState

    state = AccountState(
        account_id=7, name="Monish Combine 50K", balance=50_000.0,
        can_trade=True, is_simulated=True,
    )
    assert "Monish" not in str(state.redacted())
    assert state.redacted()["account_id"] == 7


def test_no_source_file_prints_a_raw_credential_source():
    """Static guard: no print may read the RAW credential.

    Narrowed from a substring scan on "api_key", which flagged
    ``redacted()['api_key']`` -- a value that is ``<set>`` by construction. A
    check that fires on safe code gets suppressed, and a suppressed check
    protects nothing. So this targets the raw sources specifically:
    ``cfg.api_key``, ``config.api_key``, and direct environment reads.

    The behavioural test below is the stronger of the two.
    """
    raw_sources = (
        "cfg.api_key", "config.api_key", "self.api_key",
        'environ["PROJECT_X_API_KEY"]', "environ.get(\"PROJECT_X_API_KEY\")",
    )
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if "print(" in line and any(src in line for src in raw_sources):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"raw credential reaches print(): {offenders}"


def test_config_check_output_never_contains_the_key(capsys, monkeypatch):
    """Behavioural proof, which no refactor can quietly defeat.

    Feeds a distinctive credential through the real command and asserts the
    value does not appear anywhere in the output. Stronger than any source
    scan because it tests what the user would actually see.
    """
    import cli

    monkeypatch.setenv("PROJECT_X_API_KEY", "REALKEY-9f2a8c1b4d6e7788")
    monkeypatch.setenv("PROJECT_X_USERNAME", "realtrader")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")

    import argparse

    assert cli.cmd_config_check(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "REALKEY-9f2a8c1b4d6e7788" not in out
    assert "9f2a8c1b" not in out
    assert "PRESENT" in out or "<set>" in out


# ===========================================================================
# Placeholder credentials are not credentials
# ===========================================================================


def test_example_placeholders_are_not_treated_as_credentials():
    """Copying .env.example to .env is step one; it must not look configured."""
    cfg = AppConfig(api_key="paste_your_key_here", username="your_topstepx_username")
    assert cfg.has_credentials is False
    assert cfg.execution_mode is ExecutionMode.BACKTEST


def test_a_plausible_credential_is_accepted():
    cfg = AppConfig(api_key="k3y-9f2a8c1b4d6e", username="monish")
    assert cfg.has_credentials is True


def test_blank_and_whitespace_credentials_are_not_credentials():
    for value in (None, "", "   "):
        assert AppConfig(api_key=value, username="monish").has_credentials is False


def test_the_redacted_view_explains_a_placeholder():
    cfg = AppConfig(api_key="paste_your_key_here", username="your_name")
    assert "placeholder" in cfg.redacted()["api_key"]
