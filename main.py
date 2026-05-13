import asyncio
import json
import os
import re
import urllib.request as _urllib_req
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOptionContractsRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus, ContractType, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest, OptionLatestQuoteRequest
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# --- CONFIGURATION ---
API_KEY              = os.getenv("ALPACA_API_KEY")
SECRET_KEY           = os.getenv("ALPACA_SECRET_KEY")
PASSPHRASE           = os.getenv("WEBHOOK_PASSPHRASE")
ALERT_SECRET         = os.getenv("ALERT_SECRET")
ALPACA_PAPER         = os.getenv("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL")

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
        body = await request.body()
        print(f"RAW BODY: {body.decode()}")
        data = json.loads(body)
    except Exception as e:
        print(f"JSON PARSE ERROR: {e} | body={body}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

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

# ── P&L Report ────────────────────────────────────────────────────────────────

def _underlying(symbol: str) -> str:
    """Extract underlying ticker from an OCC option symbol or return as-is."""
    m = re.match(r'^([A-Z]+)\d', str(symbol))
    return m.group(1) if m else str(symbol)


def _discord_post(payload: dict):
    """POST a JSON payload to the Discord webhook (no-op if URL not set)."""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set — skipping Discord post")
        return
    data = json.dumps(payload).encode()
    req  = _urllib_req.Request(
        DISCORD_WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json"},
    )
    _urllib_req.urlopen(req, timeout=10)


@app.get("/report")
def pnl_report():
    """
    Fetch today's intraday P&L from Alpaca, break it down per ticker,
    and send a Discord embed with realized + unrealized totals.
    """
    try:
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # ── Closed orders today ───────────────────────────────────────────────
        orders = trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=today,
            limit=200,
        ))

        pnl: dict = {}
        for o in orders:
            if "filled" not in str(o.status).lower():
                continue
            sym    = str(o.symbol)
            ticker = _underlying(sym)
            qty    = float(o.filled_qty or 0)
            price  = float(o.filled_avg_price or 0)
            mult   = 100 if (len(sym) > 6 and not sym.isalpha()) else 1
            side   = str(o.side).lower()

            entry = pnl.setdefault(ticker, {"realized": 0.0, "buys": 0, "sells": 0})
            if "sell" in side:
                entry["realized"] += qty * price * mult
                entry["sells"]    += 1
            else:
                entry["realized"] -= qty * price * mult
                entry["buys"]     += 1

        # ── Open positions (unrealized) ───────────────────────────────────────
        positions = trading_client.get_all_positions()
        open_pnl: dict = {}
        for p in positions:
            ticker = _underlying(str(p.symbol))
            open_pnl[ticker] = open_pnl.get(ticker, 0.0) + float(p.unrealized_pl or 0)

        # ── Account equity ────────────────────────────────────────────────────
        account  = trading_client.get_account()
        equity   = float(account.equity or 0)
        total_r  = sum(v["realized"] for v in pnl.values())
        total_u  = sum(open_pnl.values())

        # ── Build Discord embed fields ────────────────────────────────────────
        fields = []
        for ticker, data in sorted(pnl.items()):
            r     = data["realized"]
            emoji = "🟢" if r >= 0 else "🔴"
            upl   = open_pnl.get(ticker, None)
            upl_line = f"\nUnrealized: **${upl:+,.2f}**" if upl is not None else ""
            fields.append({
                "name":   f"{emoji} {ticker}",
                "value":  f"Realized: **${r:+,.2f}**\nBuys: {data['buys']} | Sells: {data['sells']}{upl_line}",
                "inline": True,
            })

        # Tickers with open positions but no closed trades today
        for ticker, upl in sorted(open_pnl.items()):
            if ticker not in pnl:
                fields.append({
                    "name":   f"🟡 {ticker} (open only)",
                    "value":  f"Unrealized: **${upl:+,.2f}**",
                    "inline": True,
                })

        if not fields:
            fields = [{"name": "No activity today", "value": "No filled orders yet.", "inline": False}]

        date_str = datetime.now().strftime("%Y-%m-%d")
        _discord_post({
            "embeds": [{
                "title":  f"📊 Intraday P&L — {date_str}",
                "color":  0x00b300 if total_r >= 0 else 0xcc0000,
                "fields": fields,
                "footer": {
                    "text": (
                        f"Realized: ${total_r:+,.2f}  •  "
                        f"Unrealized: ${total_u:+,.2f}  •  "
                        f"Equity: ${equity:,.2f}"
                    )
                },
            }]
        })

        return {
            "status":           "sent",
            "date":             date_str,
            "total_realized":   round(total_r, 2),
            "total_unrealized": round(total_u, 2),
            "by_ticker":        {k: round(v["realized"], 2) for k, v in pnl.items()},
        }

    except Exception as e:
        print(f"Report error: {e}")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
