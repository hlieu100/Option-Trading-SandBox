"""
order_logic.py — Render-side position manager for TradingView alerts.

Design
──────
Pine sends raw signals only.
Render checks Alpaca account/position and decides what to do.

Supported signals
─────────────────
Stock signals:
  base_entry      → if no position exists, open a long position
  add_leverage    → if long position exists, add to it
  remove_leverage → if long position exists, trim it
  stop_loss       → if position exists, close all
  support_notice  → notification only, no trade

Options signals (routed to option_logic.py):
  buy_call        → find ATM call contract, limit order at mid-price
  close_call      → close option positions (limit @ mid or market for stop loss)
"""

import logging
import math
from typing import Any, Dict, Optional

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Order

from app.config import settings
from app.models import AlertPayload
from app.trading import alpaca_client as ac
from app.trading.option_logic import handle_option_signal

log = logging.getLogger(__name__)

LEVERAGE_FACTOR = 0.5

_OPTION_ACTIONS = {"buy_call", "close_call"}


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_signal(payload: AlertPayload) -> dict:
    """
    Render is the source of truth.

    Pine sends a signal.
    We query Alpaca and decide whether to buy, add, trim, close, or ignore.
    """
    signal = payload.resolved_signal()
    ticker = payload.ticker
    requested_qty = _extract_requested_qty(payload)

    log.info(
        "Handling signal",
        extra={"signal": signal, "ticker": ticker, "requested_qty": requested_qty},
    )

    # ── Route options signals ─────────────────────────────────────────────────
    if signal in _OPTION_ACTIONS:
        if not settings.options_enabled:
            return {
                "signal": signal,
                "ticker": ticker,
                "status": "ignored",
                "note":   "Options trading is disabled. Set OPTIONS_ENABLED=true to enable.",
            }
        return await handle_option_signal(payload)

    # ── Stock signals below ───────────────────────────────────────────────────
    account  = ac.get_account()
    position = ac.get_position(ticker)

    result: Dict[str, Any] = {
        "signal": signal,
        "ticker": ticker,
        "orders": [],
    }

    # ── Notification only ─────────────────────────────────────────────────────
    if signal == "support_notice":
        result["status"] = "notified_only"
        result["note"]   = "Support notice received. No trade sent to Alpaca."
        result["position_exists"] = position is not None
        return result

    # ── Base entry ────────────────────────────────────────────────────────────
    if signal == "base_entry":
        if position is not None:
            result["status"] = "ignored"
            result["note"]   = "Base entry ignored — Alpaca already has a position."
            result["current_position_qty"] = _position_qty(position)
            return result

        qty = _resolve_base_qty(account, ticker, payload)
        if qty <= 0:
            result["status"] = "ignored"
            result["note"]   = "Base entry ignored — computed qty <= 0."
            return result

        order = _place_buy(ticker, qty, payload.limit)
        result["orders"].append(_order_summary(order))
        result["status"] = "submitted"
        return result

    # ── Add leverage ──────────────────────────────────────────────────────────
    if signal == "add_leverage":
        if position is None:
            result["status"] = "ignored"
            result["note"]   = "Add ignored — no Alpaca position exists."
            return result

        if not _is_long_position(position):
            result["status"] = "ignored"
            result["note"]   = "Add ignored — current Alpaca position is not long."
            return result

        qty = _resolve_add_qty(account, ticker, payload)
        if qty <= 0:
            result["status"] = "ignored"
            result["note"]   = "Add ignored — computed qty <= 0."
            return result

        order = _place_buy(ticker, qty, payload.limit)
        result["orders"].append(_order_summary(order))
        result["status"] = "submitted"
        return result

    # ── Remove leverage / trim ────────────────────────────────────────────────
    if signal == "remove_leverage":
        if position is None:
            result["status"] = "ignored"
            result["note"]   = "Trim ignored — no Alpaca position exists."
            return result

        if not _is_long_position(position):
            result["status"] = "ignored"
            result["note"]   = "Trim ignored — current Alpaca position is not long."
            return result

        current_qty = _position_qty(position)
        if current_qty <= 0:
            result["status"] = "ignored"
            result["note"]   = "Trim ignored — current quantity is zero."
            return result

        qty = _resolve_trim_qty(current_qty, requested_qty)
        if qty <= 0:
            result["status"] = "ignored"
            result["note"]   = "Trim ignored — trim qty <= 0."
            return result

        order = _place_sell(ticker, qty, payload.limit)
        result["orders"].append(_order_summary(order))
        result["status"] = "submitted"
        return result

    # ── Stop loss / hard close ────────────────────────────────────────────────
    if signal == "stop_loss":
        if position is None:
            result["status"] = "ignored"
            result["note"]   = "Stop loss ignored — no Alpaca position exists."
            return result

        order = ac.close_position(ticker)
        if order:
            result["orders"].append(_order_summary(order))
            result["status"] = "submitted"
        else:
            result["status"] = "ignored"
            result["note"]   = "Stop loss ignored — no close order was created."
        return result

    raise ValueError(f"Unsupported signal/action: {signal}")


# ── Qty resolution ────────────────────────────────────────────────────────────

def _extract_requested_qty(payload: AlertPayload) -> float:
    raw_qty = getattr(payload, "qty", None)
    if raw_qty is None:
        raw_qty = getattr(payload, "contracts", None)
    try:
        return float(raw_qty) if raw_qty is not None else 0.0
    except Exception:
        return 0.0


def _effective_price(ticker: str, price: Optional[float], limit: Optional[float]) -> float:
    p = limit or price
    if p and p > 0:
        return float(p)
    fetched = ac.get_latest_price(ticker)
    if not fetched:
        raise ValueError(f"Could not determine price for {ticker}")
    return float(fetched)


def _resolve_base_qty(account, ticker: str, payload: AlertPayload) -> int:
    requested_qty = _extract_requested_qty(payload)
    if requested_qty > 0:
        return math.floor(requested_qty)
    buying_power = float(account.buying_power)
    exec_price   = _effective_price(ticker, payload.price, payload.limit)
    return math.floor(buying_power / exec_price)


def _resolve_add_qty(account, ticker: str, payload: AlertPayload) -> int:
    requested_qty = _extract_requested_qty(payload)
    if requested_qty > 0:
        return math.floor(requested_qty)
    buying_power = float(account.buying_power)
    exec_price   = _effective_price(ticker, payload.price, payload.limit)
    return math.floor((buying_power * LEVERAGE_FACTOR) / exec_price)


def _resolve_trim_qty(current_qty: float, requested_qty: float) -> int:
    if requested_qty > 0:
        return math.floor(min(current_qty, requested_qty))
    return math.floor(max(1, current_qty / 2))


# ── Position helpers ──────────────────────────────────────────────────────────

def _is_long_position(position) -> bool:
    return str(position.side).lower() == "long"


def _position_qty(position) -> float:
    try:
        return float(position.qty)
    except Exception:
        return 0.0


# ── Order helpers ─────────────────────────────────────────────────────────────

def _place_buy(ticker: str, qty: int, limit: Optional[float]) -> Order:
    log.info("Submitting BUY", extra={"ticker": ticker, "qty": qty, "limit": limit})
    if limit and limit > 0:
        return ac.place_limit_order(ticker, OrderSide.BUY, qty, limit)
    return ac.place_market_order(ticker, OrderSide.BUY, qty)


def _place_sell(ticker: str, qty: int, limit: Optional[float]) -> Order:
    log.info("Submitting SELL", extra={"ticker": ticker, "qty": qty, "limit": limit})
    if limit and limit > 0:
        return ac.place_limit_order(ticker, OrderSide.SELL, qty, limit)
    return ac.place_market_order(ticker, OrderSide.SELL, qty)


def _order_summary(order: Order) -> dict:
    return {
        "alpaca_order_id": str(order.id),
        "symbol":          order.symbol,
        "side":            str(order.side),
        "qty":             str(order.qty),
        "type":            str(order.order_type),
        "status":          str(order.status),
    }
