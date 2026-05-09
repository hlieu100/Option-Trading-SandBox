"""
alpaca_client.py — Thin wrapper around alpaca-py with retry logic.

Responsibilities:
  - Build and cache client instances (trading, stock data, option data).
  - Wrap every Alpaca call in tenacity retry with exponential back-off.
  - Expose stock and options order operations for order_logic / option_logic.
"""

import logging
import math
from datetime import date, timedelta
from typing import Optional

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    GetOptionContractsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus
from alpaca.trading.models import Position, Order
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest

from app.config import settings

log = logging.getLogger(__name__)

# ── Client singletons ─────────────────────────────────────────────────────────

_trading_client: Optional[TradingClient] = None
_stock_data_client: Optional[StockHistoricalDataClient] = None
_option_data_client: Optional[OptionHistoricalDataClient] = None


def get_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=_is_paper(),
        )
        log.info(
            "Alpaca trading client initialised",
            extra={"paper": _is_paper(), "base_url": settings.alpaca_base_url},
        )
    return _trading_client


def get_data_client() -> StockHistoricalDataClient:
    global _stock_data_client
    if _stock_data_client is None:
        _stock_data_client = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )
        log.info("Alpaca stock data client initialised")
    return _stock_data_client


def get_option_data_client() -> OptionHistoricalDataClient:
    global _option_data_client
    if _option_data_client is None:
        _option_data_client = OptionHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
        )
        log.info("Alpaca option data client initialised")
    return _option_data_client


def _is_paper() -> bool:
    return "paper" in settings.alpaca_base_url.lower()


# ── Retry decorator ───────────────────────────────────────────────────────────

_retry = retry(
    retry=retry_if_exception_type(APIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


# ── Stock helpers ─────────────────────────────────────────────────────────────

@_retry
def get_account():
    account = get_client().get_account()
    log.debug(
        "Account fetched",
        extra={
            "equity":       str(account.equity),
            "buying_power": str(account.buying_power),
            "cash":         str(account.cash),
        },
    )
    return account


@_retry
def get_latest_price(ticker: str) -> Optional[float]:
    try:
        req    = StockLatestTradeRequest(symbol_or_symbols=ticker)
        trades = get_data_client().get_stock_latest_trade(req)
        price  = float(trades[ticker].price)
        log.debug("Latest price fetched", extra={"ticker": ticker, "price": price})
        return price
    except Exception as exc:
        log.warning(
            "Could not fetch latest price",
            extra={"ticker": ticker, "error": str(exc)},
        )
        return None


@_retry
def get_position(ticker: str) -> Optional[Position]:
    try:
        return get_client().get_open_position(ticker)
    except APIError as exc:
        if "position does not exist" in str(exc).lower() or "40410000" in str(exc):
            return None
        raise


@_retry
def place_market_order(ticker: str, side: OrderSide, qty: float) -> Order:
    qty = _sanitise_qty(qty)
    req = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    log.info("Submitting market order", extra={"ticker": ticker, "side": side.value, "qty": qty})
    order = get_client().submit_order(req)
    log.info("Order accepted", extra={"order_id": str(order.id), "status": order.status})
    return order


@_retry
def place_limit_order(ticker: str, side: OrderSide, qty: float, limit_price: float) -> Order:
    qty = _sanitise_qty(qty)
    req = LimitOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    log.info("Submitting limit order", extra={"ticker": ticker, "side": side.value, "qty": qty, "limit": limit_price})
    order = get_client().submit_order(req)
    log.info("Limit order accepted", extra={"order_id": str(order.id), "status": order.status})
    return order


@_retry
def close_position(ticker: str) -> Optional[Order]:
    position = get_position(ticker)
    if position is None:
        log.info("No open position to close", extra={"ticker": ticker})
        return None
    log.info("Closing position", extra={"ticker": ticker, "qty": position.qty})
    order = get_client().close_position(ticker)
    log.info("Close-position order accepted", extra={"order_id": str(order.id)})
    return order


# ── Options helpers ───────────────────────────────────────────────────────────

@_retry
def find_option_contract(
    ticker: str,
    strike: Optional[float],
    dte_target: int = 30,
) -> str:
    """
    Find the best call contract for `ticker` using a 4-stage filter:

    1. Expiry window   — between DTE-10 and DTE+20 days out
    2. Open interest   — drop contracts below option_min_open_interest
    3. Monthly filter  — prefer standard monthly (3rd-Friday) expiries
    4. Spread quality  — fetch quotes for top candidates, pick tightest
                         bid/ask spread that is within option_max_spread_pct

    Returns the OCC option symbol string (e.g. "NVDA260117C00215000").
    """
    today    = date.today()
    exp_from = today + timedelta(days=max(7, dte_target - 10))
    exp_to   = today + timedelta(days=dte_target + 20)

    req = GetOptionContractsRequest(
        underlying_symbols=[ticker],
        expiration_date_gte=exp_from,
        expiration_date_lte=exp_to,
        type=ContractType.CALL,
        status=AssetStatus.ACTIVE,
    )

    result   = get_client().get_option_contracts(req)
    if not result or not result.option_contracts:
        raise ValueError(
            f"No active call contracts found for {ticker} "
            f"between {exp_from} and {exp_to}."
        )

    options = result.option_contracts
    reference_strike = strike or get_latest_price(ticker) or 0.0

    # ── Stage 1: open interest filter ─────────────────────────────────────────
    min_oi = settings.option_min_open_interest
    liquid = [c for c in options if int(c.open_interest or 0) >= min_oi]
    if not liquid:
        log.warning(
            "No contracts meet min OI — relaxing OI filter",
            extra={"ticker": ticker, "min_oi": min_oi},
        )
        liquid = options

    # ── Stage 2: monthly vs weekly preference ─────────────────────────────────
    if settings.option_prefer_monthly:
        monthly = [c for c in liquid if _is_monthly_expiry(c.expiration_date)]
        if monthly:
            liquid = monthly
        else:
            log.info(
                "No monthly expiries available — using all expiries",
                extra={"ticker": ticker},
            )

    # ── Stage 3: sort by nearest expiry, then closest strike ──────────────────
    liquid.sort(key=lambda c: (
        c.expiration_date,
        abs(float(c.strike_price) - reference_strike),
    ))

    # ── Stage 4: fetch quotes for top N, pick tightest spread ─────────────────
    candidates = liquid[: settings.option_max_candidates]
    symbols    = [c.symbol for c in candidates]

    try:
        quote_req = OptionLatestQuoteRequest(symbol_or_symbols=symbols)
        quotes    = get_option_data_client().get_option_latest_quote(quote_req)
    except Exception as exc:
        log.warning(
            "Could not fetch option quotes — falling back to nearest strike",
            extra={"ticker": ticker, "error": str(exc)},
        )
        chosen = candidates[0]
        log.info("Option contract selected (no-quote fallback)",
                 extra={"contract": chosen.symbol, "strike": str(chosen.strike_price)})
        return chosen.symbol

    def _spread_ratio(contract) -> float:
        """Return bid/ask spread as fraction of mid-price. inf if unusable."""
        q = quotes.get(contract.symbol)
        if not q:
            return float("inf")
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        if ask <= 0:
            return float("inf")
        mid = (bid + ask) / 2
        if mid <= 0:
            return float("inf")
        return (ask - bid) / mid

    max_spread = settings.option_max_spread_pct
    valid = [c for c in candidates if _spread_ratio(c) <= max_spread]
    if not valid:
        log.warning(
            "All candidates exceed max spread — using tightest available",
            extra={"ticker": ticker, "max_spread_pct": max_spread},
        )
        valid = candidates

    # Final sort: tightest spread first, then closest to target strike
    valid.sort(key=lambda c: (
        _spread_ratio(c),
        abs(float(c.strike_price) - reference_strike),
    ))

    chosen = valid[0]
    q = quotes.get(chosen.symbol)
    bid = float(q.bid_price or 0) if q else 0
    ask = float(q.ask_price or 0) if q else 0

    log.info(
        "Option contract selected",
        extra={
            "ticker":        ticker,
            "contract":      chosen.symbol,
            "strike":        str(chosen.strike_price),
            "expiry":        str(chosen.expiration_date),
            "open_interest": str(chosen.open_interest),
            "bid":           bid,
            "ask":           ask,
            "spread_pct":    f"{_spread_ratio(chosen):.1%}",
            "ref_strike":    reference_strike,
        },
    )
    return chosen.symbol


def _is_monthly_expiry(exp_date) -> bool:
    """Return True if exp_date falls on the 3rd Friday of its month."""
    from calendar import monthcalendar, FRIDAY
    d = exp_date if isinstance(exp_date, date) else date.fromisoformat(str(exp_date))
    fridays = [week[FRIDAY] for week in monthcalendar(d.year, d.month) if week[FRIDAY] != 0]
    return len(fridays) >= 3 and d.day == fridays[2]


@_retry
def get_option_mid_price(option_symbol: str) -> float:
    """
    Fetch the latest bid/ask for an option contract and return the mid-price.
    Falls back to ask if bid is zero, or raises if both are unavailable.
    """
    req    = OptionLatestQuoteRequest(symbol_or_symbols=option_symbol)
    quotes = get_option_data_client().get_option_latest_quote(req)
    quote  = quotes.get(option_symbol)

    if not quote:
        raise ValueError(f"No quote data available for {option_symbol}")

    bid = float(quote.bid_price or 0)
    ask = float(quote.ask_price or 0)

    if bid <= 0 and ask <= 0:
        raise ValueError(f"Both bid and ask are zero for {option_symbol} — market may be closed.")

    if bid <= 0:
        mid = ask
    else:
        mid = round((bid + ask) / 2, 2)

    log.info(
        "Option mid-price calculated",
        extra={"symbol": option_symbol, "bid": bid, "ask": ask, "mid": mid},
    )
    return mid


@_retry
def place_option_limit_order(
    option_symbol: str,
    side: OrderSide,
    qty: int,
    limit_price: float,
) -> Order:
    """Place a day limit order for an option contract at limit_price."""
    qty = int(max(1, qty))
    req = LimitOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    log.info(
        "Submitting option limit order",
        extra={"symbol": option_symbol, "side": side.value, "qty": qty, "limit": limit_price},
    )
    order = get_client().submit_order(req)
    log.info(
        "Option limit order accepted",
        extra={"order_id": str(order.id), "symbol": option_symbol, "status": order.status},
    )
    return order


@_retry
def place_option_market_order(
    option_symbol: str,
    side: OrderSide,
    qty: int,
) -> Order:
    """Place a market order for an option contract."""
    qty = int(max(1, qty))
    req = MarketOrderRequest(
        symbol=option_symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    log.info(
        "Submitting option market order",
        extra={"symbol": option_symbol, "side": side.value, "qty": qty},
    )
    order = get_client().submit_order(req)
    log.info(
        "Option market order accepted",
        extra={"order_id": str(order.id), "symbol": option_symbol, "status": order.status},
    )
    return order


def get_option_positions(underlying: str) -> list:
    """
    Return all open option positions for `underlying` (e.g. "NVDA").
    Alpaca option positions have symbols like "NVDA260117C00215000".
    """
    try:
        all_positions = get_client().get_all_positions()
        option_positions = [
            p for p in all_positions
            if p.symbol.startswith(underlying)
            and len(p.symbol) > len(underlying)  # not the stock itself
        ]
        log.info(
            "Option positions fetched",
            extra={"underlying": underlying, "count": len(option_positions)},
        )
        return option_positions
    except Exception as exc:
        log.warning("Could not fetch option positions", extra={"error": str(exc)})
        return []


def close_option_positions(underlying: str) -> list[Order]:
    """
    Market-close all open option positions for `underlying`.
    Returns list of submitted orders.
    """
    positions = get_option_positions(underlying)
    orders = []
    for pos in positions:
        qty = int(float(pos.qty or 0))
        if qty <= 0:
            continue
        try:
            order = place_option_market_order(pos.symbol, OrderSide.SELL, qty)
            orders.append(order)
        except Exception as exc:
            log.error(
                "Failed to close option position",
                extra={"symbol": pos.symbol, "error": str(exc)},
            )
    return orders


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sanitise_qty(qty: float) -> float:
    if not settings.allow_fractional_shares:
        qty = math.floor(qty)
    if qty <= 0:
        raise ValueError(
            f"Order quantity resolved to {qty} — must be > 0. "
            "Check 'contracts' in the TradingView alert and the "
            "ALLOW_FRACTIONAL_SHARES setting."
        )
    return qty
