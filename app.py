import hmac
import json
import logging
import os
import re

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError
from flask import Flask, jsonify, request

app = Flask(__name__)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"

ALPACA_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip()

# TRADING_MODE: "paper" (default) or "live".
TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()
if TRADING_MODE not in {"paper", "live"}:
    raise ValueError("TRADING_MODE must be 'paper' or 'live'")

# Explicit override wins; otherwise the base URL follows TRADING_MODE.
ALPACA_BASE_URL = os.getenv("APCA_API_BASE_URL", "").strip() or (
    LIVE_BASE_URL if TRADING_MODE == "live" else PAPER_BASE_URL
)

# Refuse to start on a mismatched combination.
if TRADING_MODE == "live" and ALPACA_BASE_URL.rstrip("/") == PAPER_BASE_URL:
    raise ValueError("TRADING_MODE=live but APCA_API_BASE_URL points at paper")
if TRADING_MODE == "paper" and ALPACA_BASE_URL.rstrip("/") == LIVE_BASE_URL:
    raise ValueError("TRADING_MODE=paper but APCA_API_BASE_URL points at live")

# Live trading requires an explicit second opt-in.
LIVE_CONFIRMED = os.getenv("LIVE_TRADING_CONFIRMED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
if TRADING_MODE == "live" and not LIVE_CONFIRMED:
    raise ValueError("TRADING_MODE=live requires LIVE_TRADING_CONFIRMED=true")

ALLOWED_SYMBOLS = {
    s.strip().upper()
    for s in os.getenv("ALLOWED_SYMBOLS", "SPY,QQQ").split(",")
    if s.strip()
}

try:
    ORDER_QTY = int(os.getenv("ORDER_QTY", "1"))
except ValueError:
    raise ValueError("ORDER_QTY must be an integer")

if ORDER_QTY < 1:
    raise ValueError("ORDER_QTY must be at least 1")

# Words TradingView alerts commonly use for a long entry.
BUY_WORDS = {"BUY", "LONG", "ENTRY", "MOMLE"}

api = (
    tradeapi.REST(
        key_id=ALPACA_KEY,
        secret_key=ALPACA_SECRET,
        base_url=ALPACA_BASE_URL,
        api_version="v2",
    )
    if ALPACA_KEY and ALPACA_SECRET
    else None
)

logger.info(
    "Rob Agent starting: mode=%s base_url=%s qty=%d symbols=%s",
    TRADING_MODE,
    ALPACA_BASE_URL,
    ORDER_QTY,
    ",".join(sorted(ALLOWED_SYMBOLS)),
)


def reject(message: str, status: int, **extra):
    """Log every rejection with its reason so Render's logs show WHY."""
    logger.warning("Webhook rejected (%d): %s %s", status, message, extra or "")
    return jsonify(error=message, **extra), status


def safe_preview(raw: str) -> str:
    """Body preview for logs with the secret value masked."""
    masked = re.sub(r'("secret"\s*:\s*")[^"]*(")', r"\1***\2", raw)
    return masked[:300]


def make_client_order_id(signal_id: str) -> str:
    """Create a stable Alpaca-compatible ID for duplicate protection."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", signal_id).strip("-_")
    return f"tv-{safe_id}"[:48]


def is_missing_order_error(exc: APIError) -> bool:
    """Return True only when Alpaca reports that the order does not exist."""
    if getattr(exc, "status_code", None) == 404:
        return True
    text = str(exc).lower()
    return "order not found" in text or "not found" in text


def parse_webhook_payload():
    """Parse a JSON object sent as text/plain, application/json, or a quoted string.

    TradingView always posts Content-Type: text/plain, so the raw body is parsed
    directly instead of using request.get_json() (which caused the old 415).
    Returns (payload_or_None, raw_body).
    """
    raw = (request.get_data(as_text=True) or "").strip()
    if not raw:
        return None, raw

    # Strip a UTF-8 BOM or stray wrapping that some clients add.
    raw = raw.lstrip("\ufeff")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, raw

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None, raw

    return (payload if isinstance(payload, dict) else None), raw


def normalize_symbol(value: str) -> str:
    """'AMEX:SPY', 'spy', ' SPY ' -> 'SPY'."""
    value = str(value or "").strip().upper()
    if ":" in value:
        value = value.split(":")[-1]
    return value


@app.get("/")
def home():
    return jsonify(
        service="Rob Agent",
        status="running",
        mode=TRADING_MODE,
        base_url=ALPACA_BASE_URL,
        qty=ORDER_QTY,
        symbols=sorted(ALLOWED_SYMBOLS),
    ), 200


@app.get("/health")
def health():
    if api is None or not WEBHOOK_SECRET:
        return jsonify(
            status="unhealthy",
            error="Required environment variables are not configured",
        ), 503

    try:
        account = api.get_account()
        return jsonify(
            status="healthy",
            mode=TRADING_MODE,
            base_url=ALPACA_BASE_URL,
            account_status=str(account.status),
        ), 200
    except Exception:
        logger.exception("Alpaca health check failed")
        return jsonify(status="unhealthy", error="Unable to reach Alpaca"), 503


@app.post("/webhook")
def webhook():
    if api is None or not WEBHOOK_SECRET:
        logger.error("Required environment variables are missing")
        return jsonify(error="Server configuration error"), 503

    payload, raw = parse_webhook_payload()
    if payload is None:
        return reject(
            "Invalid JSON payload",
            400,
            received=safe_preview(raw),
            hint="Alert message must be a JSON object; no unexpanded {{placeholders}} outside quotes",
        )

    supplied_secret = str(payload.get("secret", ""))
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        return reject("Unauthorized", 401)

    symbol = normalize_symbol(payload.get("symbol", ""))
    action = str(payload.get("action", "")).strip().upper()
    signal_id = str(payload.get("signal_id", "")).strip()

    if not signal_id:
        return reject("signal_id is required", 400)

    if len(signal_id) > 128:
        return reject("signal_id is too long", 400)

    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
        return reject("Invalid symbol", 400, symbol_received=symbol)

    if symbol not in ALLOWED_SYMBOLS:
        return reject(
            "Symbol is not allowed",
            403,
            symbol_received=symbol,
            allowed=sorted(ALLOWED_SYMBOLS),
        )

    if action not in BUY_WORDS:
        return reject(
            "Only BUY is supported (long-only)",
            400,
            action_received=action,
        )

    client_order_id = make_client_order_id(signal_id)

    try:
        existing_order = api.get_order_by_client_order_id(client_order_id)
        return jsonify(
            status="duplicate",
            mode=TRADING_MODE,
            symbol=symbol,
            order_id=str(existing_order.id),
            order_status=str(existing_order.status),
            client_order_id=client_order_id,
        ), 200
    except APIError as exc:
        if not is_missing_order_error(exc):
            logger.exception("Unable to check for an existing order")
            return jsonify(error="Order lookup failed"), 502
    except Exception:
        logger.exception("Unable to check for an existing order")
        return jsonify(error="Order lookup failed"), 502

    try:
        clock = api.get_clock()
        if not clock.is_open:
            return reject("Market is closed", 409)

        account = api.get_account()
        if account.trading_blocked:
            return reject("Account is blocked from trading", 403)

        if any(
            position.symbol.upper() == symbol
            for position in api.list_positions()
        ):
            return reject("A position already exists for this symbol", 409)

        order = api.submit_order(
            symbol=symbol,
            qty=ORDER_QTY,
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id=client_order_id,
        )

        logger.info(
            "%s order submitted: symbol=%s qty=%d order_id=%s client_order_id=%s",
            TRADING_MODE,
            symbol,
            ORDER_QTY,
            order.id,
            client_order_id,
        )

        return jsonify(
            status="submitted",
            mode=TRADING_MODE,
            symbol=symbol,
            qty=ORDER_QTY,
            order_id=str(order.id),
            client_order_id=client_order_id,
        ), 202
    except APIError:
        logger.exception("Alpaca rejected the %s order for %s", TRADING_MODE, symbol)
        return jsonify(error="Order was rejected by Alpaca"), 502
    except Exception:
        logger.exception("%s order submission failed for %s", TRADING_MODE, symbol)
        return jsonify(error="Order submission failed"), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
