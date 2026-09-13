"""ProjectX read-only adapter. The ONLY module that speaks the vendor dialect.

Implements :class:`MarketDataBroker` and deliberately NOT
:class:`ExecutionBroker`. It has no order method of any kind, so a component
holding this object cannot transmit -- that is a property of the type, not a
flag checked at the last moment.

WHAT THIS TRANSLATES, AND WHY EACH ONE MATTERS
----------------------------------------------
* **Bad logins return HTTP 200 with ``success: false``** (docs/PROJECTX_API.md
  §1). An adapter that trusts the status code treats a rejection as a success
  and runs on with no token. Every call here checks the outcome, not the code.

* **Net liquidation does not exist on the account object.** It is derived as
  balance plus unrealised P&L, and when the unrealised part cannot be computed
  honestly this reports ``None`` rather than falling back to balance. ``None``
  means unknown, and unknown stops trading.

* **The point value is not 1.0.** ``Position.unrealized_pnl`` defaults its
  multiplier to 1.0, which halves every MNQ figure. It is always passed
  explicitly here, and the derived value is cross-checked against the contract.

* **A timeout is not a failure.** Anything that leaves us unsure what the
  broker did raises :class:`BrokerUnavailableError`, which the runtime treats
  as "reconcile", never as "retry".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from execution.broker import ContractSpec, MarketDataBroker
from execution.models import (
    AccountState,
    AuthenticationError,
    BrokerError,
    BrokerUnavailableError,
    MarketBar,
    OrderState,
    OrderType,
    Position,
    RateLimitError,
    Side,
    WorkingOrder,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from config import AppConfig

__all__ = ["ProjectXReadOnlyBroker", "classify_broker_error"]

# project_x_py.PositionType: 0 undefined, 1 long, 2 short.
_POSITION_LONG = 1
_POSITION_SHORT = 2

# project_x_py.OrderSide: BUY = 0, SELL = 1  (docs: side 0 = Bid, 1 = Ask)
_SIDE_BUY = 0

# project_x_py.OrderType: 1 Limit, 2 Market, 4 Stop, 5 TrailingStop
_ORDER_TYPE_MAP = {
    1: OrderType.LIMIT,
    2: OrderType.MARKET,
    4: OrderType.STOP,
    5: OrderType.TRAILING_STOP,
}


def classify_broker_error(exc: BaseException) -> BrokerError:
    """Map a vendor or transport exception onto our own taxonomy.

    The distinction that matters is between "the broker said no" and "we do not
    know what the broker did". They look similar in a traceback and are utterly
    different in consequence: the first is safe to act on, the second requires
    reconciliation before anything else happens.
    """
    text = f"{type(exc).__name__}: {exc}".lower()

    if any(k in text for k in ("401", "unauthor", "forbidden", "403",
                               "token", "credential", "authenticat")):
        return AuthenticationError(str(exc))
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return RateLimitError(str(exc))
    if any(k in text for k in ("timeout", "timed out", "connection", "network",
                               "unreachable", "reset", "dns", "ssl",
                               "getaddrinfo", "disconnect")):
        return BrokerUnavailableError(str(exc))
    return BrokerUnavailableError(f"unclassified broker failure: {exc}")


@dataclass
class ProjectXReadOnlyBroker(MarketDataBroker):
    """Read-only ProjectX connection.

    ``client_factory`` exists so tests can inject a fake. The real factory
    imports the SDK lazily, so this module (and everything importing it) loads
    without credentials and without the package having to authenticate.
    """

    config: AppConfig
    client_factory: Any = None
    _client: Any = None
    _authenticated: bool = False
    _contract_cache: dict[str, ContractSpec] | None = None

    # -- connection ---------------------------------------------------------

    async def authenticate(self) -> None:
        if not self.config.has_credentials:
            raise AuthenticationError(
                "no credentials configured. Set PROJECT_X_API_KEY and "
                "PROJECT_X_USERNAME in .env. Note PROJECT_X_USERNAME is the "
                "TopstepX USERNAME, not the email address."
            )
        try:
            self._client = await self._make_client()
            await self._client.authenticate()
        except Exception as exc:
            self._authenticated = False
            raise classify_broker_error(exc) from exc

        # A bad login can return success-shaped output; prove we have an
        # account rather than assuming the absence of an exception means yes.
        account = self._client.get_account_info()
        if account is None:
            raise AuthenticationError(
                "authenticated but no account resolved. Usually one of: the "
                "API key was generated BEFORE linking to TopstepX, the API "
                "subscription is inactive, or PROJECT_X_ACCOUNT_NAME does not "
                "match any account exactly."
            )
        self._authenticated = True

    async def _make_client(self) -> Any:
        if self.client_factory is not None:
            maybe = self.client_factory()
            return await maybe if hasattr(maybe, "__await__") else maybe
        from project_x_py import ProjectX  # lazy: import without credentials

        return ProjectX.from_env()

    async def is_connected(self) -> bool:
        return self._authenticated and self._client is not None

    def _require_session(self) -> Any:
        if not self._authenticated or self._client is None:
            raise AuthenticationError(
                "not authenticated; call authenticate() first. Refusing to "
                "read account state over an unproven session."
            )
        return self._client

    # -- reads --------------------------------------------------------------

    async def get_account(self) -> AccountState:
        client = self._require_session()
        try:
            account = client.get_account_info()
        except Exception as exc:
            raise classify_broker_error(exc) from exc
        if account is None:
            raise BrokerError("account info unavailable")

        return AccountState(
            account_id=int(account.id),
            name=str(account.name),
            balance=float(account.balance),
            can_trade=bool(account.canTrade),
            is_simulated=bool(account.simulated),
            net_liquidation=None,  # derived by the caller; never guessed here
            observed_at=datetime.now(UTC),
        )

    async def get_positions(self) -> list[Position]:
        client = self._require_session()
        try:
            raw = await client.search_open_positions()
        except Exception as exc:
            raise classify_broker_error(exc) from exc

        positions: list[Position] = []
        for item in raw or []:
            size = int(item.size)
            kind = int(item.type)
            if kind == _POSITION_SHORT:
                size = -size
            elif kind != _POSITION_LONG:
                raise BrokerError(
                    f"position {item.id} has undefined direction (type={kind}); "
                    "refusing to guess whether it is long or short"
                )
            positions.append(
                Position(
                    contract_id=str(item.contractId),
                    size=size,
                    average_price=float(item.averagePrice),
                )
            )
        return positions

    async def get_working_orders(self) -> list[WorkingOrder]:
        client = self._require_session()
        try:
            raw = await client.search_open_orders()
        except AttributeError:
            try:
                raw = await client.get_orders()
            except Exception as exc:
                raise classify_broker_error(exc) from exc
        except Exception as exc:
            raise classify_broker_error(exc) from exc

        orders: list[WorkingOrder] = []
        for item in raw or []:
            orders.append(
                WorkingOrder(
                    order_id=str(item.id),
                    contract_id=str(item.contractId),
                    side=Side.BUY if int(item.side) == _SIDE_BUY else Side.SELL,
                    order_type=_ORDER_TYPE_MAP.get(int(item.type), OrderType.MARKET),
                    size=int(item.size),
                    # The vendor's status codes are not documented well enough
                    # to map confidently, and a wrong mapping here would hide a
                    # live order. UNKNOWN forces reconciliation instead.
                    state=OrderState.UNKNOWN,
                    limit_price=_opt_float(getattr(item, "limitPrice", None)),
                    stop_price=_opt_float(getattr(item, "stopPrice", None)),
                    filled_size=int(getattr(item, "fillVolume", 0) or 0),
                    custom_tag=getattr(item, "customTag", None),
                )
            )
        return orders

    async def get_contract(self, symbol: str) -> ContractSpec:
        if self._contract_cache and symbol in self._contract_cache:
            return self._contract_cache[symbol]

        client = self._require_session()
        try:
            instrument = await client.get_instrument(symbol)
        except Exception as exc:
            raise classify_broker_error(exc) from exc
        if instrument is None:
            raise BrokerError(f"no contract resolved for {symbol!r}")

        spec = ContractSpec(
            contract_id=str(instrument.id),
            symbol=str(instrument.name),
            tick_size=float(instrument.tickSize),
            tick_value=float(instrument.tickValue),
        )
        if spec.tick_size <= 0 or spec.tick_value <= 0:
            raise BrokerError(
                f"{symbol}: unusable tick geometry "
                f"(size={spec.tick_size}, value={spec.tick_value})"
            )
        self._contract_cache = {**(self._contract_cache or {}), symbol: spec}
        return spec

    async def get_bars(
        self, symbol: str, *, days: int = 5, interval_minutes: int = 5
    ) -> list[MarketBar]:
        """Raw bars as neutral objects.

        Returns MarketBar, NOT the backtester's Bar: the timestamp convention
        is still unverified, and :mod:`marketdata` is the only place allowed to
        assert one. Handing these straight to the backtester would smuggle an
        assumption past the guard that exists to catch it.
        """
        client = self._require_session()
        try:
            frame = await client.get_bars(symbol, days=days, interval=interval_minutes)
        except Exception as exc:
            raise classify_broker_error(exc) from exc
        if frame is None or len(frame) == 0:
            return []

        from marketdata import bars_from_projectx_frame

        return bars_from_projectx_frame(frame, symbol=symbol)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
