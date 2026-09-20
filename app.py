import hmac
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

ALPACA_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
ALPACA_BASE_URL = os.getenv(
    "APCA_API_BASE_URL",
    "https://paper-api.alpaca.markets",
).strip()

ALLOWED_SYMBOLS = {
    symbol.strip().upper()
    for symbol in os.getenv("ALLOWED_SYMBOLS", "SPY,QQQ").split(",")
    if symbol.strip()
}

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


def make_client_order_id(signal_id: str) -> str:
    """Create a stable Alpaca-compatible ID for duplicate protection."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", signal_id).strip("-_")
    return f"tv-{safe_id}"[:48]


def is_missing_order_error(exc: APIError) -> bool:
    """Return True only when Alpaca reports that the order does not exist."""
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True

    error_text = str(exc).lower()
    return "order not found" in error_text or "not found" in error_text


@app.get("/")
def home():
    return jsonify(
        service="Rob Agent",
        status="running",
        mode="paper",
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
            mode="paper",
            account_status=str(account.status),
        ), 200
    except Exception:
        logger.exception("Alpaca health check failed")
        return jsonify(
            status="unhealthy",
            error="Unable to reach Alpaca",
        ), 503


@app.post("/webhook")
def webhook():
    if api is None or not WEBHOOK_SECRET:
        logger.error("Required environment variables are missing")
        return jsonify(error="Server configuration error"), 503

    if not request.is_json:
        return jsonify(error="Content-Type must be application/json"), 415

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Invalid JSON payload"), 400

    # TradingView can send this value in the JSON body. Avoid logging it.
    supplied_secret = str(payload.get("secret", ""))
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        return jsonify(error="Unauthorized"), 401

    symbol = str(payload.get("symbol", "")).strip().upper()
    action = str(payload.get("action", "")).strip().upper()
    signal_id = str(payload.get("signal_id", "")).strip()

    if not signal_id:
        return jsonify(error="signal_id is required"), 400

    if len(signal_id) > 128:
        return jsonify(error="signal_id is too long"), 400

    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
        return jsonify(error="Invalid symbol"), 400

    if symbol not in ALLOWED_SYMBOLS:
        return jsonify(error="Symbol is not allowed"), 403

    if action != "BUY":
        return jsonify(error="Only BUY is currently supported"), 400

    client_order_id = make_client_order_id(signal_id)

    try:
        existing_order = api.get_order_by_client_order_id(client_order_id)
        return jsonify(
            status="duplicate",
            mode="paper",
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
            return jsonify(error="Market is closed"), 409

        account = api.get_account()
        if account.trading_blocked:
            return jsonify(error="Account is blocked from trading"), 403

        if any(
            position.symbol.upper() == symbol
            for position in api.list_positions()
        ):
            return jsonify(
                error="A position already exists for this symbol"
            ), 409

        order = api.submit_order(
            symbol=symbol,
            qty=1,
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id=client_order_id,
        )

        logger.info(
            "Paper order submitted: symbol=%s order_id=%s client_order_id=%s",
            symbol,
            order.id,
            client_order_id,
        )

        return jsonify(
            status="submitted",
            mode="paper",
            symbol=symbol,
            order_id=str(order.id),
            client_order_id=client_order_id,
        ), 202
    except APIError:
        logger.exception("Alpaca rejected the paper order for %s", symbol)
        return jsonify(error="Order was rejected by Alpaca"), 502
    except Exception:
        logger.exception("Paper order submission failed for %s", symbol)
        return jsonify(error="Order submission failed"), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
