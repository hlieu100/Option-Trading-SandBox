"""
option_logic.py — Options order handling for TradingView → Alpaca.

Handles signals from Hoang-Kimi Options Pine script:

  buy_call   (type: base_entry | dd_entry)
             → Find ATM call contract, place limit order at mid-price.
             → If limit unfilled after timeout_min, cancel + replace with market.

  close_call (type: dd_exit)
             → Limit sell at mid-price with same timeout logic.

  close_call (type: stop_loss)
             → Immediate market sell — speed over price on stop loss.
"""

import asyncio
import logging
from typing import Any, Dict

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

from app.models import AlertPayload
from app.trading import alpaca_client as ac

log = logging.getLogger(__name__)

_OPEN_STATUSES = {"new", "partially_filled", "accepted", "pending_new", "held"}


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_option_signal(payload: AlertPayload) -> Dict[str, Any]:
    """
    Route options signals to the correct action.
    Called by order_logic.handle_signal when action is buy_call or close_call.
    """
    action       = payload.resolved_signal()
    signal_type  = payload.type or ""          # base_entry, dd_entry, dd_exit, stop_loss
    ticker       = payload.ticker
    contracts    = int(payload.contracts or 1)
    order_type   = (payload.order_type or "limit").lower()
    timeout_min  = int(payload.timeout_min or 5)
    strike       = payload.strike
    dte          = int(payload.dte or 30)

    result: Dict[str, Any] = {
        "action": action,
        "type":   signal_type,
        "ticker": ticker,
        "orders": [],
    }

    # ── BUY CALL ──────────────────────────────────────────────────────────────
    if action == "buy_call":
        contract_symbol = ac.find_option_contract(ticker, strike, dte)
        result["contract"] = contract_symbol

        if order_type == "limit":
            mid_price = ac.get_option_mid_price(contract_symbol)
            order     = ac.place_option_limit_order(
                contract_symbol, OrderSide.BUY, contracts, mid_price
            )
            result["orders"].append(_order_summary(order))
            result["limit_price"] = mid_price
            result["status"] = "submitted"

            # Background: cancel + market if unfilled after timeout
            asyncio.create_task(
                _limit_timeout(
                    order_id     = str(order.id),
                    symbol       = contract_symbol,
                    side         = OrderSide.BUY,
                    qty          = contracts,
                    timeout_min  = timeout_min,
                )
            )
        else:
            order = ac.place_option_market_order(contract_symbol, OrderSide.BUY, contracts)
            result["orders"].append(_order_summary(order))
            result["status"] = "submitted"

        log.info(
            "Option BUY submitted",
            extra={
                "ticker":   ticker,
                "type":     signal_type,
                "contract": contract_symbol,
                "qty":      contracts,
                "order_type": order_type,
            },
        )
        return result

    # ── CLOSE CALL ────────────────────────────────────────────────────────────
    if action == "close_call":
        is_stop_loss = signal_type == "stop_loss"

        if is_stop_loss:
            # Market close — speed matters on stop loss
            orders = ac.close_option_positions(ticker)
            result["orders"] = [_order_summary(o) for o in orders]
            result["status"]  = "submitted" if orders else "ignored"
            if not orders:
                result["note"] = "No open option positions to close."
            log.info(
                "Option STOP LOSS market close",
                extra={"ticker": ticker, "orders_count": len(orders)},
            )
        else:
            # Limit sell at mid-price with timeout fallback
            positions = ac.get_option_positions(ticker)
            if not positions:
                result["status"] = "ignored"
                result["note"]   = "No open option positions found."
                return result

            for pos in positions:
                qty = int(float(pos.qty or 0))
                if qty <= 0:
                    continue
                try:
                    mid_price = ac.get_option_mid_price(pos.symbol)
                    order     = ac.place_option_limit_order(
                        pos.symbol, OrderSide.SELL, qty, mid_price
                    )
                    result["orders"].append(_order_summary(order))

                    asyncio.create_task(
                        _limit_timeout(
                            order_id    = str(order.id),
                            symbol      = pos.symbol,
                            side        = OrderSide.SELL,
                            qty         = qty,
                            timeout_min = timeout_min,
                        )
                    )
                except APIError as exc:
                    log.error(
                        "Failed to place option sell",
                        extra={"symbol": pos.symbol, "error": str(exc)},
                    )

            result["status"] = "submitted" if result["orders"] else "ignored"

        return result

    raise ValueError(f"Unsupported option action: {action}")


# ── Limit-order timeout ───────────────────────────────────────────────────────

async def _limit_timeout(
    order_id: str,
    symbol: str,
    side: OrderSide,
    qty: int,
    timeout_min: int,
) -> None:
    """
    After `timeout_min` minutes, cancel the limit order if still open
    and replace any unfilled quantity with a market order.
    """
    await asyncio.sleep(timeout_min * 60)
    try:
        order = get_client().get_order_by_id(order_id)
        status = str(order.status).lower()

        if status not in _OPEN_STATUSES:
            log.debug(
                "Limit order already resolved — no timeout action needed",
                extra={"order_id": order_id, "status": status},
            )
            return

        filled_qty = int(float(order.filled_qty or 0))
        remaining  = qty - filled_qty

        log.info(
            "Limit order timed out — cancelling and replacing with market",
            extra={"order_id": order_id, "symbol": symbol, "remaining": remaining},
        )

        try:
            get_client().cancel_order_by_id(order_id)
        except Exception as cancel_exc:
            log.warning(
                "Could not cancel limit order",
                extra={"order_id": order_id, "error": str(cancel_exc)},
            )

        if remaining > 0:
            ac.place_option_market_order(symbol, side, remaining)

    except Exception as exc:
        log.warning(
            "Timeout handler error",
            extra={"order_id": order_id, "symbol": symbol, "error": str(exc)},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_client():
    return ac.get_client()


def _order_summary(order) -> dict:
    return {
        "alpaca_order_id": str(order.id),
        "symbol":          str(order.symbol),
        "side":            str(order.side),
        "qty":             str(order.qty),
        "type":            str(order.order_type),
        "status":          str(order.status),
    }
