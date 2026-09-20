import hmac
import logging
import os
import re
import uuid

import alpaca_trade_api as tradeapi
from flask import Flask, jsonify, request

app = Flask(__name__)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "")

ALLOWED_SYMBOLS = {
    symbol.strip().upper()
    for symbol in os.getenv("ALLOWED_SYMBOLS", "SPY,QQQ").split(",")
    if symbol.strip()
}

api = None

if ALPACA_KEY and ALPACA_SECRET:
    api = tradeapi.REST(
        key_id=ALPACA_KEY,
        secret_key=ALPACA_SECRET,
        base_url="[paper-api.alpaca.markets](https://paper-api.alpaca.markets)",
        api_version="v2",
    )


@app.get("/")
def home():
    return jsonify(
        service="Rob Agent",
        status="running",
        mode="paper",
    ), 200


@app.get("/health")
def health():
    if api is None:
        return jsonify(
            status="unhealthy",
            error="Alpaca credentials are not configured",
        ), 503

    return jsonify(status="healthy"), 200


@app.post("/webhook")
def webhook():
    if api is None or not WEBHOOK_SECRET:
        logger.error("Required environment variables are missing")
        return jsonify(error="Server configuration error"), 503

    if not request.is_json:
        return jsonify(
            error="Content-Type must be application/json"
        ), 415

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify(error="Invalid JSON payload"), 400

    supplied_secret = str(payload.get("secret", ""))

    if not hmac.compare_digest(
        supplied_secret,
        WEBHOOK_SECRET,
    ):
        return jsonify(error="Unauthorized"), 401

    symbol = str(payload.get("symbol", "")).strip().upper()
    action = str(payload.get("action", "")).strip().upper()
    signal_id = str(
        payload.get("signal_id", uuid.uuid4().hex)
    ).strip()

    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
        return jsonify(error="Invalid symbol"), 400

    if symbol not in ALLOWED_SYMBOLS:
        return jsonify(error="Symbol is not allowed"), 403

    if action != "BUY":
        return jsonify(error="Only BUY is currently supported"), 400

    client_order_id = make_client_order_id(signal_id)

    # Prevent the same TradingView signal from creating another order.
    try:
        existing_order = api.get_order_by_client_order_id(
            client_order_id
        )

        return jsonify(
            status="duplicate",
            symbol=symbol,
            order_id=str(existing_order.id),
            order_status=str(existing_order.status),
        ), 200

    except Exception as exc:
        # This older SDK does not expose consistent exception classes
        # across all versions. Only continue when no matching order exists.
        if "not found" not in str(exc).lower():
            logger.exception("Unable to check for an existing order")
            return jsonify(error="Order lookup failed"), 502

    try:
        clock = api.get_clock()

        if not clock.is_open:
            return jsonify(error="Market is closed"), 409

        account = api.get_account()

        if account.trading_blocked:
            return jsonify(error="Account is blocked from trading"), 403

        position_symbols = {
            position.symbol.upper()
            for position in api.list_positions()
        }

        if symbol in position_symbols:
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
            "Paper order submitted: symbol=%s order_id=%s",
            symbol,
            order.id,
        )

        return jsonify(
            status="submitted",
            mode="paper",
            symbol=symbol,
            order_id=str(order.id),
            client_order_id=client_order_id,
        ), 202

    except Exception:
        logger.exception(
            "Paper order submission failed for %s",
            symbol,
        )

        return jsonify(error="Order submission failed"), 502


def make_client_order_id(signal_id: str) -> str:
    safe_id = re.sub(
        r"[^A-Za-z0-9_-]",
        "-",
        signal_id,
    )

    return f"tv-{safe_id}"[:48]


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
    )
