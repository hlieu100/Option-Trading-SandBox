import asyncio
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus, ContractType
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest, OptionLatestQuoteRequest
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# --- CONFIGURATION ---
API_KEY      = os.getenv("ALPACA_API_KEY")
SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")
PASSPHRASE   = os.getenv("WEBHOOK_PASSPHRASE")
ALERT_SECRET = os.getenv("ALERT_SECRET")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

trading_client     = TradingClient(API_KEY, SECRET_KEY, paper=ALPACA_PAPER)
stock_data_client  = StockHistoricalDataClient(API_KEY, SECRET_KEY)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

ACTION_MAP = {
    "buy_call":   ("open",  "buy"),
    "buy_put":    ("open",  "sell"),
    "close_call": ("close", None),
    "close_put":  ("close", None),
    "buy":        ("open",  "buy"),
    "sell":       ("open",  "sell"),
    "close":      ("close", None),
}

_OPEN_STATUSES = {"new", "partially_filled", "accepted", "pending_new", "held"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_option_mid_price(contract_symbol: str) -> float:
    """Fetch bid/ask and return mid-price. Returns 0.0 if unavailable."""
    try:
        quotes = option_data_client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
        )
        q   = quotes.get(contract_symbol)
        bid = float(q.bid_price or 0) if q else 0
        ask = float(q.ask_price or 0) if q else 0
        if ask <= 0:
            return 0.0
        return round((bid + ask) / 2, 2) if bid > 0 else ask
    except Exception as e:
        print(f"Could not fetch mid price for {contract_symbol}: {e}")
        return 0.0

def get_best_alpaca_contract(underlying_symbol: str, side: str, timeframe: str) -> str | None:
    """Find ATM option contract. side: 'buy'=CALL, 'sell'=PUT."""
    try:
        # Use last trade price — more reliable than ask (works outside market hours)
        try:
            trade_req     = StockLatestTradeRequest(symbol_or_symbols=[underlying_symbol])
            trade         = stock_data_client.get_stock_latest_trade(trade_req)
            current_price = float(trade[underlying_symbol].price)
        except Exception:
            quote_req     = StockLatestQuoteRequest(symbol_or_symbols=[underlying_symbol])
            quote         = stock_data_client.get_stock_latest_quote(quote_req)
            current_price = float(quote[underlying_symbol].ask_price or quote[underlying_symbol].bid_price)
        print(f"Current price for {underlying_symbol}: {current_price}")

        if timeframe == "60":
            min_days, max_days = 40, 50
        elif timeframe == "15":
            min_days, max_days = 5, 14
        else:
            min_days, max_days = 25, 35

        min_date = datetime.now() + timedelta(days=min_days)
        max_date = datetime.now() + timedelta(days=max_days)

        search_params = GetOptionContractsRequest(
            underlying_symbols=[underlying_symbol],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=min_date.date(),
            expiration_date_lte=max_date.date(),
            type=ContractType.CALL if side == "buy" else ContractType.PUT
        )

        response  = trading_client.get_option_contracts(search_params)
        contracts = response.option_contracts

        if not contracts:
            print(f"No contracts found for {underlying_symbol} ({side}) in {min_days}-{max_days} days.")
            return None

        best = min(contracts, key=lambda x: abs(float(x.strike_price) - current_price))
        print(f"ATM selected: {best.symbol} strike={best.strike_price} vs current={current_price} from {len(contracts)} contracts")
        return best.symbol

    except Exception as e:
        print(f"Error in contract search: {e}")
        return None

def close_all_for_ticker(ticker: str) -> list:
    """Cancel open buy orders then market-close all option positions for ticker."""
    try:
        # Cancel any open buy orders to avoid wash trade rejection
        try:
            open_orders = trading_client.get_orders()
            for o in open_orders:
                if str(o.symbol).startswith(ticker) and str(o.side).lower() in ("buy", "orderside.buy"):
                    trading_client.cancel_order_by_id(str(o.id))
                    print(f"Cancelled open buy order {o.id} for {o.symbol}")
        except Exception as ce:
            print(f"Could not cancel open orders: {ce}")

        positions = trading_client.get_all_positions()
        matched   = [p for p in positions if p.symbol.startswith(ticker)]
        if not matched:
            matched = [p for p in positions if len(p.symbol) > 6 and not p.symbol.isalpha()]
        closed = []
        for p in matched:
            try:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=p.symbol,
                    qty=p.qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                ))
                closed.append(p.symbol)
            except Exception:
                pass
        return closed
    except Exception as e:
        print(f"Error closing positions: {e}")
        return []

async def _limit_timeout(order_id: str, symbol: str, qty: int, timeout_min: int):
    """After timeout_min minutes, cancel unfilled limit order and replace with market."""
    await asyncio.sleep(timeout_min * 60)
    try:
        order  = trading_client.get_order_by_id(order_id)
        status = str(order.status).lower()
        if status not in _OPEN_STATUSES:
            return
        filled    = int(float(order.filled_qty or 0))
        remaining = qty - filled
        try:
            trading_client.cancel_order_by_id(order_id)
        except Exception:
            pass
        if remaining > 0:
            trading_client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=remaining,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            ))
            print(f"Timeout: replaced limit with market for {symbol} x{remaining}")
    except Exception as e:
        print(f"Timeout handler error for {order_id}: {e}")

# ── Flask app ─────────────────────────────────────────────────────────────────

@app.get("/")
def health_check():
    return {"status": "online", "message": "Trading bot is active"}

@app.get("/debug/{ticker}")
def debug_price(ticker: str):
    """Show current price and available ATM contracts for a ticker."""
    try:
        trade_req = StockLatestTradeRequest(symbol_or_symbols=[ticker])
        trade     = stock_data_client.get_stock_latest_trade(trade_req)
        price     = float(trade[ticker].price)
    except Exception as e:
        return {"error": f"Could not fetch price: {e}"}

    from datetime import date, timedelta
    min_date = date.today() + timedelta(days=25)
    max_date = date.today() + timedelta(days=35)
    try:
        result    = trading_client.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[ticker], status=AssetStatus.ACTIVE,
            expiration_date_gte=min_date, expiration_date_lte=max_date,
            type=ContractType.CALL
        ))
        contracts = result.option_contracts or []
        strikes   = sorted([float(c.strike_price) for c in contracts])
        closest   = min(strikes, key=lambda s: abs(s - price)) if strikes else None
    except Exception as e:
        return {"price": price, "error": f"Contract search failed: {e}"}

    return {
        "ticker":          ticker,
        "current_price":   price,
        "window":          f"{min_date} to {max_date}",
        "contracts_found": len(strikes),
        "closest_strike":  closest,
        "all_strikes":     strikes[:20],
    }

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    incoming = data.get("passphrase") or data.get("secret")
    expected = PASSPHRASE or ALERT_SECRET
    if expected and incoming != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    ticker      = data.get("ticker", "").split(":")[-1]
    raw_action  = data.get("action", "").lower()
    timeframe   = str(data.get("timeframe", "5"))
    order_type  = (data.get("order_type") or "limit").lower()
    timeout_min = int(data.get("timeout_min") or 5)
    contracts   = int(data.get("contracts") or 1)

    # ── Direct contract close ─────────────────────────────────────────────────
    if raw_action == "close_contract":
        contract_symbol = data.get("contract")
        if not contract_symbol:
            raise HTTPException(status_code=400, detail="Missing 'contract' field")
        try:
            # Cancel any open buy orders first to avoid wash trade rejection
            try:
                open_orders = trading_client.get_orders()
                for o in open_orders:
                    if str(o.symbol) == contract_symbol and str(o.side).lower() in ("buy", "orderside.buy"):
                        trading_client.cancel_order_by_id(str(o.id))
                        print(f"Cancelled open buy order {o.id} for {contract_symbol}")
            except Exception as ce:
                print(f"Could not cancel open orders: {ce}")

            positions = trading_client.get_all_positions()
            pos = next((p for p in positions if p.symbol == contract_symbol), None)
            qty = int(float(pos.qty)) if pos else 1
            try:
                submitted = trading_client.submit_order(MarketOrderRequest(
                    symbol=contract_symbol, qty=qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                ))
            except Exception:
                mid = get_option_mid_price(contract_symbol) or 0.01
                submitted = trading_client.submit_order(LimitOrderRequest(
                    symbol=contract_symbol, qty=qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, limit_price=mid
                ))
            return {"status": "success", "action": "close_contract",
                    "contract": contract_symbol, "order_id": str(submitted.id)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    resolved = ACTION_MAP.get(raw_action)
    if resolved is None:
        raise HTTPException(status_code=400, detail=f"Unknown action: {raw_action}")

    intent, side = resolved

    # ── CLOSE ─────────────────────────────────────────────────────────────────
    if intent == "close":
        closed = close_all_for_ticker(ticker)
        return {"status": "success", "action": "close", "ticker": ticker, "contracts": closed}

    # ── OPEN ──────────────────────────────────────────────────────────────────
    contract = get_best_alpaca_contract(ticker, side, timeframe)
    if not contract:
        return {"status": "error", "message": f"No ATM contract found for {ticker} ({side})"}

    option_type = "CALL" if side == "buy" else "PUT"

    try:
        if order_type == "limit":
            mid = get_option_mid_price(contract)
            if mid > 0:
                submitted = trading_client.submit_order(LimitOrderRequest(
                    symbol=contract,
                    qty=contracts,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=mid
                ))
                # Background timeout → cancel + market if unfilled
                asyncio.create_task(_limit_timeout(
                    str(submitted.id), contract, contracts, timeout_min
                ))
                return {
                    "status":      "success",
                    "action":      "open",
                    "type":        option_type,
                    "ticker":      ticker,
                    "contract":    contract,
                    "order_type":  "limit",
                    "limit_price": mid,
                    "timeout_min": timeout_min,
                    "order_id":    str(submitted.id)
                }
            # No quote — fall through to market

        # Market order (explicit or fallback)
        submitted = trading_client.submit_order(MarketOrderRequest(
            symbol=contract,
            qty=contracts,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        ))
        return {
            "status":     "success",
            "action":     "open",
            "type":       option_type,
            "ticker":     ticker,
            "contract":   contract,
            "order_type": "market",
            "order_id":   str(submitted.id)
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
