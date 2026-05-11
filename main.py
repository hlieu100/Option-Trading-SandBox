import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus, ContractType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# --- CONFIGURATION ---
API_KEY      = os.getenv("ALPACA_API_KEY")
SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")
PASSPHRASE   = os.getenv("WEBHOOK_PASSPHRASE")
ALERT_SECRET = os.getenv("ALERT_SECRET")       # Pine Script "secret" field
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

trading_client    = TradingClient(API_KEY, SECRET_KEY, paper=ALPACA_PAPER)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Map Pine Script action names → (intent, side)
# intent: "open" | "close"
# side:   "buy" = CALL, "sell" = PUT, None = close
ACTION_MAP = {
    "buy_call":   ("open",  "buy"),
    "buy_put":    ("open",  "sell"),
    "close_call": ("close", None),
    "close_put":  ("close", None),
    "buy":        ("open",  "buy"),
    "sell":       ("open",  "sell"),
    "close":      ("close", None),
}

def get_best_alpaca_contract(underlying_symbol, side, timeframe):
    """side: 'buy'=CALL, 'sell'=PUT. Finds ATM contract for the given window."""
    try:
        quote_req = StockLatestQuoteRequest(symbol_or_symbols=[underlying_symbol])
        quote = stock_data_client.get_stock_latest_quote(quote_req)
        current_price = quote[underlying_symbol].ask_price

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

        best_contract = min(contracts, key=lambda x: abs(float(x.strike_price) - current_price))
        return best_contract.symbol

    except Exception as e:
        print(f"Error in contract search: {e}")
        return None

def close_all_for_ticker(ticker):
    """Close all open option positions for a specific underlying ticker."""
    try:
        positions = trading_client.get_all_positions()
        closed = []
        # Match by ticker prefix first; fall back to all option positions
        matched = [p for p in positions if p.symbol.startswith(ticker)]
        if not matched:
            matched = [p for p in positions if len(p.symbol) > 6 and not p.symbol.isalpha()]
        for p in matched:
            try:
                order_data = MarketOrderRequest(
                    symbol=p.symbol,
                    qty=p.qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order_data)
                closed.append(p.symbol)
            except Exception:
                pass
        return closed
    except Exception as e:
        print(f"Error closing positions: {e}")
        return []

@app.get("/")
def health_check():
    return {"status": "online", "message": "Trading bot is active"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Accept both "passphrase" (legacy/TradingView) and "secret" (Pine Script)
    incoming = data.get("passphrase") or data.get("secret")
    expected = PASSPHRASE or ALERT_SECRET
    if expected and incoming != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Clean ticker (e.g. NASDAQ:TSLA → TSLA)
    ticker     = data.get("ticker", "").split(":")[-1]
    raw_action = data.get("action", "").lower()
    timeframe  = str(data.get("timeframe", "5"))

    # Direct contract close — bypasses ticker search
    if raw_action == "close_contract":
        contract_symbol = data.get("contract")
        if not contract_symbol:
            raise HTTPException(status_code=400, detail="Missing 'contract' field for close_contract")
        try:
            positions = trading_client.get_all_positions()
            pos = next((p for p in positions if p.symbol == contract_symbol), None)
            qty = int(float(pos.qty)) if pos else 1

            # Try market order first; fall back to limit if Alpaca rejects (no quote)
            try:
                order_request = MarketOrderRequest(
                    symbol=contract_symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                submitted = trading_client.submit_order(order_request)
            except Exception:
                # Fetch mid price for limit order; use $0.01 if unavailable
                try:
                    from alpaca.data.historical import OptionHistoricalDataClient
                    from alpaca.data.requests import OptionLatestQuoteRequest
                    opt_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
                    quote = opt_client.get_option_latest_quote(
                        OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
                    )
                    q = quote.get(contract_symbol)
                    bid = float(q.bid_price or 0) if q else 0
                    ask = float(q.ask_price or 0) if q else 0
                    limit_price = round((bid + ask) / 2, 2) if ask > 0 else 0.01
                except Exception:
                    limit_price = 0.01

                order_request = LimitOrderRequest(
                    symbol=contract_symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price
                )
                submitted = trading_client.submit_order(order_request)

            return {"status": "success", "action": "close_contract", "contract": contract_symbol, "order_id": str(submitted.id)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    resolved = ACTION_MAP.get(raw_action)
    if resolved is None:
        raise HTTPException(status_code=400, detail=f"Unknown action: {raw_action}")

    intent, side = resolved

    # CLOSE
    if intent == "close":
        closed = close_all_for_ticker(ticker)
        return {"status": "success", "action": "close", "ticker": ticker, "contracts": closed}

    # OPEN
    contract = get_best_alpaca_contract(ticker, side, timeframe)
    if not contract:
        return {"status": "error", "message": f"No ATM contract found for {ticker} ({side})"}

    try:
        order_request = MarketOrderRequest(
            symbol=contract,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        submitted    = trading_client.submit_order(order_request)
        option_type  = "CALL" if side == "buy" else "PUT"
        return {
            "status":   "success",
            "action":   "open",
            "type":     option_type,
            "ticker":   ticker,
            "contract": contract,
            "order_id": str(submitted.id)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
