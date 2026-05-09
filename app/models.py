"""
models.py — Pydantic models for incoming TradingView webhook payloads.
"""

from typing import Optional
from enum import Enum

from pydantic import BaseModel, field_validator


class TradingSignal(str, Enum):
    """
    Raw signal types sent from Pine.
    Render decides what to do after checking Alpaca.
    """
    # Stock signals
    BASE_ENTRY      = "base_entry"
    ADD_LEVERAGE    = "add_leverage"
    REMOVE_LEVERAGE = "remove_leverage"
    STOP_LOSS       = "stop_loss"
    SUPPORT_NOTICE  = "support_notice"

    # Options signals
    BUY_CALL   = "buy_call"
    CLOSE_CALL = "close_call"


class AlertPayload(BaseModel):
    """
    Incoming TradingView webhook payload.

    Supports both stock and options signals.
    Pine sends raw intent only — Render checks Alpaca and decides what to do.
    """
    # Auth — must match WEBHOOK_SECRET env var
    secret: str

    # Symbol, e.g. "SPY"
    ticker: str

    # Raw signal from Pine (optional — falls back to `action` for options alerts)
    signal: Optional[TradingSignal] = None

    # Optional qty from Pine. Render may use it or override it.
    qty: Optional[float] = None

    # Current bar close price
    price: Optional[float] = None

    # Optional limit price (stock orders)
    limit: Optional[float] = None

    # TradingView idempotency key if you include one
    order_id: Optional[str] = None

    # TradingView timestamp
    timestamp: Optional[str] = None

    # Optional context fields from TradingView
    market_position: Optional[str] = None
    market_position_size: Optional[float] = None
    prev_market_position: Optional[str] = None
    prev_market_position_size: Optional[float] = None

    # Backward-compatible aliases if old Pine is still sending them
    action: Optional[str] = None
    contracts: Optional[float] = None

    # ── Options-specific fields ────────────────────────────────────────────────
    # "limit" or "market"
    order_type: Optional[str] = None

    # "mid" → Render fetches bid/ask from Alpaca and uses (bid+ask)/2
    price_method: Optional[str] = None

    # Minutes before a limit order is cancelled and replaced with market
    timeout_min: Optional[int] = None

    # Suggested strike price from Pine (nearest ATM)
    strike: Optional[float] = None

    # Target DTE from Pine
    dte: Optional[int] = None

    # Signal sub-type: "base_entry", "dd_entry", "dd_exit", "stop_loss"
    type: Optional[str] = None

    # Estimated option premium per share (from Pine's Black-Scholes estimate)
    est_premium: Optional[float] = None

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("ticker", mode="before")
    @classmethod
    def clean_ticker(cls, v: str) -> str:
        """Strip exchange prefix like 'NASDAQ:AAPL' -> 'AAPL'."""
        if ":" in v:
            v = v.split(":")[-1]
        return v.strip().upper()

    @field_validator("signal", mode="before")
    @classmethod
    def normalise_signal(cls, v) -> Optional[str]:
        """
        Accept mixed-case strings and some older plain-English forms.
        Returns None if v is None — signal is optional (options alerts use `action`).
        """
        if v is None or v == "":
            return None

        v = str(v).strip().lower()
        mapping = {
            "base entry":      "base_entry",
            "add leverage":    "add_leverage",
            "remove leverage": "remove_leverage",
            "stop loss":       "stop_loss",
            "support notice":  "support_notice",
            "buy call":        "buy_call",
            "close call":      "close_call",
        }
        return mapping.get(v, v)

    @field_validator("qty", mode="before")
    @classmethod
    def parse_qty(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    @field_validator("contracts", mode="before")
    @classmethod
    def parse_contracts(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    def resolved_signal(self) -> Optional[str]:
        """
        Return the effective signal, preferring `signal` then `action`.
        This is the single place order_logic should read from.
        """
        s = self.signal.value if self.signal else None
        return s or self.action

    model_config = {"extra": "ignore"}
