import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus, ContractType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from dotenv import load_dotenv

# Load variables from .env file for local testing
load_dotenv()

app = FastAPI()

# --- CONFIGURATION ---
# These should be set as Environment Variables in Render
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE")

# Initialize Alpaca Clients
# Set paper=True for testing, paper=False for live trading
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

def get_best_alpaca_contract(underlying_symbol, side, timeframe):
    """
    Logic:
    - 1 Hour (60): ~45 days out
    - 15 Minutes (15): Next Friday (~7-12 days out)
    """
    try:
        # 1. Get current stock price for ATM strike matching
        quote_req = StockLatestQuoteRequest(symbol_or_symbols=[underlying_symbol])
        quote = stock_data_client.get_stock_latest_quote(quote_req)
        current_price = quote[underlying_symbol].ask_price

        # 2. Set Expiration Window based on timeframe
        if timeframe == "60": 
            min_days, max_days = 40, 50
        elif timeframe == "15":
            min_days, max_days = 5, 14
        else:
            min_days, max_days = 25, 35 # Default fallback

        min_date = datetime.now() + timedelta(days=min_days)
        max_date = datetime.now() + timedelta(days=max_days)
        
        # 3. Search for Active Option Contracts
        search_params = GetOptionContractsRequest(
            underlying_symbol=[underlying_symbol],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=min_date.date(),
            expiration_date_lte=max_date.date(),
            type=ContractType.CALL if side == "buy" else ContractType.PUT
        )
        
        response = trading_client.get_option_contracts(search_params)
        contracts = response.option_contracts
        
        if not contracts:
            print(f"No contracts found for {underlying_symbol} in range {min_days}-{max_days} days.")
            return None

        # 4. Find the strike price closest to current market price (At-The-Money)
        best_contract = min(contracts, key=lambda x: abs(float(x.strike_price) - current_price))
        return best_contract.symbol

    except Exception as e:
        print(f"Error in contract search: {e}")
        return None

def close_all_for_ticker(ticker):
    """Finds and exits all open option positions for a specific ticker."""
    try:
        positions = trading_client.get_all_positions()
        closed_contracts = []
        for p in positions:
            # Check if it's an option and belongs to our ticker
            if ticker in p.symbol and p.asset_class.value == "us_option":
                order_data = MarketOrderRequest(
                    symbol=p.symbol,
                    qty=p.qty,
                    side=OrderSide.SELL, 
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order_data)
                closed_contracts.append(p.symbol)
        return closed_contracts
    except Exception as e:
        print(f"Error closing positions: {e}")
        return []

@app.get("/")
def health_check():
    return {"status": "online", "message": "Trading bot is active"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    # Parse incoming JSON from TradingView
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Security Check
    if data.get("passphrase") != PASSPHRASE:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Clean Ticker (e.g., NASDAQ:TSLA -> TSLA)
    raw_ticker = data.get("ticker", "")
    ticker = raw_ticker.split(':')[-1]
    
    action = data.get("action", "").lower() # 'buy', 'sell', or 'close'
    timeframe = str(data.get("timeframe", "60"))

    # ACTION: CLOSE
    if action == "close":
        closed = close_all_for_ticker(ticker)
        return {"status": "success", "action": "close", "contracts": closed}

    # ACTION: OPEN (Buy Call or Buy Put)
    # Note: 'buy' triggers a Call search, 'sell' triggers a Put search
    contract_to_trade = get_best_alpaca_contract(ticker, action, timeframe)
    
    if not contract_to_trade:
        return {"status": "error", "message": f"Could not find ATM contract for {ticker}"}

    try:
        order_request = MarketOrderRequest(
            symbol=contract_to_trade,
            qty=1,
            side=OrderSide.BUY, # We buy to open the position
            time_in_force=TimeInForce.DAY
        )
        submitted_order = trading_client.submit_order(order_request)
        return {
            "status": "success", 
            "action": "open", 
            "contract": contract_to_trade, 
            "order_id": str(submitted_order.id)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    # Render provides a PORT environment variable automatically
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)