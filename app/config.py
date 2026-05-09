"""
config.py — Application settings loaded from environment variables.

All sensitive values (API keys, secrets) must be set in a .env file
or as real environment variables. Never hard-code them here.
"""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Alpaca ────────────────────────────────────────────────────────────────
    alpaca_api_key: str
    alpaca_secret_key: str
    # Paper trading endpoint by default. Switch to https://api.alpaca.markets
    # for live trading only after thorough testing.
    alpaca_base_url: str = "https://paper-api.alpaca.markets/v2"

    # ── Webhook security ──────────────────────────────────────────────────────
    # Must match the "secret" field TradingView sends in every alert payload.
    webhook_secret: str

    # ── Server ────────────────────────────────────────────────────────────────
    port: int = 8000

    # ── Idempotency ───────────────────────────────────────────────────────────
    # How long (seconds) to remember a processed alert_id to block duplicates.
    idempotency_ttl: int = 300  # 5 minutes

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Optional notifications (leave blank to disable) ───────────────────────
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # ── Order defaults ────────────────────────────────────────────────────────
    # Set to True only if your Alpaca account has fractional-share trading
    # enabled AND the symbol supports it.
    allow_fractional_shares: bool = False

    # ── Options trading ───────────────────────────────────────────────────────
    # Must be True to process buy_call / close_call signals.
    # Requires Options Level 2 approval on your Alpaca account.
    options_enabled: bool = False

    # Minutes before an unfilled limit order is cancelled and replaced with market.
    option_limit_timeout_min: int = 5

    # Contract selection — liquidity filters
    # Minimum open interest to consider a contract tradeable.
    option_min_open_interest: int = 100

    # Maximum bid/ask spread as a fraction of mid-price (0.15 = 15%).
    # Contracts wider than this are skipped.
    option_max_spread_pct: float = 0.15

    # Number of closest-strike candidates to fetch quotes for before
    # picking the tightest spread.
    option_max_candidates: int = 5

    # Prefer standard monthly expiries (3rd Friday) over weeklies.
    # Monthlies typically have higher OI and tighter spreads.
    option_prefer_monthly: bool = True

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


# Single shared instance — import this everywhere else.
settings = Settings()
