"""
main.py — FastAPI application entry point.

Endpoints
─────────
POST /webhook   Receives TradingView alerts and routes them to Alpaca.
GET  /health    Liveness probe — returns 200 + uptime info.

Security model
──────────────
Every request to /webhook must carry the correct "secret" field in the
JSON body (matched via constant-time comparison in security.py). There is
no separate API-key header — the secret is embedded in the alert payload
as TradingView requires.
"""

import logging
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import settings
from app.idempotency import is_duplicate, mark_processed
from app.logging_config import setup_logging
from app.models import AlertPayload
from app.notifications import notify
from app.security import verify_webhook_secret
from app.trading.order_logic import handle_signal
from alpaca.common.exceptions import APIError

# ── Logging must be set up before the first log call ─────────────────────────
setup_logging()
log = logging.getLogger(__name__)

_start_time = time.time()


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TradingView → Alpaca webhook server starting",
        extra={"paper_trading": "paper" in settings.alpaca_base_url},
    )
    yield
    log.info("Server shutting down.")


app = FastAPI(
    title="TradingView → Alpaca Webhook",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    log.warning("Invalid payload", extra={"errors": exc.errors()})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "Invalid payload", "detail": exc.errors()},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health():
    """Liveness / readiness probe. Returns 200 while the server is up."""
    return {
        "status": "ok",
        "uptime_s": round(time.time() - _start_time, 1),
        "paper": "paper" in settings.alpaca_base_url,
    }


@app.post("/webhook", tags=["trading"])
async def webhook(request: Request):
    """
    Main TradingView alert receiver.

    Flow:
      1. Parse raw JSON.
      2. Validate secret.
      3. Validate AlertPayload.
      4. Reject duplicates when order_id is present.
      5. Let Render decide what to do via handle_signal().
      6. Return structured response.
    """
    # ── 1. Raw JSON parse ─────────────────────────────────────────────────────
    try:
        raw = await request.json()
    except Exception:
        log.warning("Received non-JSON request body")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must be valid JSON."},
        )

    log.debug("Raw alert received", extra={"body": raw})

    # ── 2. Secret check ───────────────────────────────────────────────────────
    received_secret = raw.get("secret", "")
    try:
        verify_webhook_secret(received_secret)
    except Exception:
        log.warning("Alert rejected — bad secret", extra={"ip": _client_ip(request)})
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Unauthorized."},
        )

    # ── 3. Payload validation ─────────────────────────────────────────────────
    try:
        payload = AlertPayload(**raw)
    except ValidationError as exc:
        log.warning("Alert rejected — validation error", extra={"errors": exc.errors()})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Payload validation failed.", "detail": exc.errors()},
        )

    log.info(
        "Signal received",
        extra={
            "ticker": payload.ticker,
            "signal": getattr(payload, "signal", None),
            "action": getattr(payload, "action", None),
            "qty": getattr(payload, "qty", None),
            "contracts": getattr(payload, "contracts", None),
            "order_id": payload.order_id,
            "timestamp": payload.timestamp,
        },
    )

    # ── 4. Idempotency check ──────────────────────────────────────────────────
    if payload.order_id and is_duplicate(payload):
        log.info(
            "Duplicate alert ignored",
            extra={"ticker": payload.ticker, "order_id": payload.order_id},
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "duplicate", "message": "Alert already processed."},
        )

    # ── 5. Handle signal (Render is source of truth) ─────────────────────────
    try:
        result = await handle_signal(payload)

        if payload.order_id:
            mark_processed(payload)

        log.info(
            "Signal handled",
            extra={
                "ticker": payload.ticker,
                "signal": getattr(payload, "signal", None),
                "action": getattr(payload, "action", None),
                "result": result,
            },
        )

        await notify(
            f"✅ <b>{getattr(payload, 'signal', getattr(payload, 'action', 'unknown')).upper()}</b> "
            f"{payload.ticker} | result={result.get('status')}"
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "result": result},
        )

    except ValueError as exc:
        log.warning(
            "Signal rejected — bad value: %s",
            exc,
            extra={"ticker": payload.ticker},
        )
        await notify(f"⚠️ Signal rejected for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": str(exc)},
        )

    except APIError as exc:
        log.error(
            "Alpaca API error",
            exc_info=True,
            extra={"ticker": payload.ticker},
        )
        await notify(f"❌ Alpaca error for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "Alpaca API error.", "detail": str(exc)},
        )

    except Exception as exc:
        log.exception("Unexpected error processing alert")
        await notify(f"❌ Unexpected error for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error."},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Best-effort client IP (respects X-Forwarded-For from proxies)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
        log_config=None,
    )
