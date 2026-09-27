"""
TradingView -> Alpaca webhook application.

Receives TradingView alert payloads, validates them, and submits market
orders to Alpaca. The account (paper vs live) is chosen by ALPACA_PAPER.

Deployment notes
----------------
* Nothing here calls get_json(); the body is parsed from raw bytes, so
  TradingView's wrong/missing Content-Type header is irrelevant. This is
  the fix for a 415 returned by the earlier handler.
* The shared secret is never written to the logs. Do not re-add a raw-body
  or header dump.
* Crypto is 24/7 and uses GTC + fractional qty; equities are session-bound,
  DAY, whole shares. Both live in this one code path.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque

from flask import Flask, jsonify, request

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


# --- Environment -------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def _optional_env(name: str) -> str:
    return os.environ.get(name, "").strip()


# Paper unless explicitly disabled. Only the literal string "false" (case
# insensitive) turns it off, so a typo like ALPACA_PAPER=0 stays in paper.
ALPACA_PAPER = _optional_env("ALPACA_PAPER").lower() != "false"
ENV_LABEL = "PAPER" if ALPACA_PAPER else "LIVE"

# Prefer per-environment credential pairs so a live key can never sit in the
# paper deployment's environment waiting for one variable to flip. Fall back
# to the generic names for a single-environment setup.
if ALPACA_PAPER:
    ALPACA_API_KEY = _optional_env("ALPACA_PAPER_API_KEY") or _require_env("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = _optional_env("ALPACA_PAPER_SECRET_KEY") or _require_env("ALPACA_SECRET_KEY")
else:
    ALPACA_API_KEY = _optional_env("ALPACA_LIVE_API_KEY") or _require_env("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = _optional_env("ALPACA_LIVE_SECRET_KEY") or _require_env("ALPACA_SECRET_KEY")

WEBHOOK_SECRET = _require_env("WEBHOOK_SECRET")

api = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
app = Flask(__name__)

logger.warning("=" * 66)
logger.warning(
    "Alpaca client initialised against the %s account. TradingView alerts "
    "on this URL will submit %s orders.",
    ENV_LABEL,
    ENV_LABEL,
)
logger.warning("=" * 66)


# --- Configuration -----------------------------------------------------------

MAX_BODY_BYTES = 64 * 1024

# Allowlist: 16 equities + 11 crypto pairs. Everything else is rejected.
#
# VOO is deliberately absent: it holds substantially the same constituents as
# SPY, so a Pine script firing on both is one position reported twice rather
# than two. VTI (total market, adds mid/small cap) and IWM carry the breadth.
ALLOWED_SYMBOLS = {
    # --- Equities (16) ---
    "SPY",    # SPDR S&P 500 ETF
    "QQQ",    # Invesco QQQ Trust
    "SQQQ",   # ProShares UltraPro Short QQQ (3x inverse)
    "IWM",    # iShares Russell 2000 ETF
    "DIA",    # SPDR Dow Jones Industrial Average ETF
    "AAPL",   # Apple
    "MSFT",   # Microsoft
    "NVDA",   # NVIDIA
    "AMZN",   # Amazon
    "GOOGL",  # Alphabet Class A
    "META",   # Meta Platforms
    "MU",     # Micron Technology
    "VTI",    # Vanguard Total Stock Market ETF
    "XLK",    # Technology Select Sector SPDR
    "XLF",    # Financial Select Sector SPDR
    "TLT",    # iShares 20+ Year Treasury Bond ETF
    # --- Crypto (11) ---
    "BTC/USD",   # Bitcoin
    "ETH/USD",   # Ethereum
    "SOL/USD",   # Solana
    "XRP/USD",   # XRP
    "LTC/USD",   # Litecoin
    "BCH/USD",   # Bitcoin Cash
    "LINK/USD",  # Chainlink
    "AVAX/USD",  # Avalanche
    "DOGE/USD",  # Dogecoin
    "UNI/USD",   # Uniswap
    "AAVE/USD",  # Aave
}

# Symbols that may never be traded even if listed.
DENIED_SYMBOLS = set()

# Per-symbol direction restriction. A symbol named here accepts only the sides
# listed. SQQQ is BUY-only: a SELL reads like "close my position" but is a
# short of a short on a fund that rebalances daily, and the drift is ugly.
DIRECTION_RESTRICTIONS = {
    "SQQQ": {"BUY"},
}

DEFAULT_QTY = 1
MAX_QTY = 100
MAX_NOTIONAL = 50_000        # per-order USD cap for notional-sized orders
MAX_REQUESTS_PER_MINUTE = 30  # in-process backstop, see _rate_limited()

MISSING_ORDER_STATUS = 404

# Equities (BRK.B, RDS-A, SPY) and crypto pairs (BTC/USD).
_SYMBOL_RE = re.compile(r"(?:[A-Z][A-Z0-9.\-]{0,14}|[A-Z]{2,10}/[A-Z]{2,10})")

# Quote currencies TradingView may glue onto a crypto ticker (BTCUSD).
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "EUR")

# Optional comma-separated override, e.g. ALLOWED_SYMBOLS=SPY,QQQ,BTC/USD
_override = _optional_env("ALLOWED_SYMBOLS").upper()
if _override:
    ALLOWED_SYMBOLS = {s.strip() for s in _override.split(",") if s.strip()}


# --- Symbol normalization (TradingView -> Alpaca) ----------------------------

def normalize_symbol(raw: str) -> str:
    """
    TradingView sends its own ticker format; Alpaca wants its own.

        NASDAQ:AAPL   -> AAPL
        AMEX:SPY      -> SPY
        CRYPTO:BTCUSD -> BTC/USD
        BTC/USD       -> BTC/USD  (unchanged)

    Falls back to the stripped, upper-cased input. The allowlist check is what
    finally rejects anything unrecognised, so an unmapped ticker fails there
    rather than being silently rewritten.
    """
    sym = raw.strip().upper()

    # Strip the exchange prefix TradingView prepends.
    if ":" in sym:
        sym = sym.split(":", 1)[1]

    # Already a pair (BTC/USD) or a plain equity (AAPL).
    if "/" in sym:
        return sym

    # Crypto arrives glued: BTCUSD -> BTC/USD. Only rewrite when the candidate
    # is already allowlisted, so 'SPY' can never be mangled into 'SP/Y'.
    for quote in _QUOTE_SUFFIXES:
        if sym.endswith(quote) and len(sym) > len(quote):
            candidate = f"{sym[: -len(quote)]}/{quote}"
            if candidate in ALLOWED_SYMBOLS:
                return candidate

    return sym


# --- Idempotency -------------------------------------------------------------

def make_client_order_id(signal_id: str) -> str:
    """
    Deterministic idempotency key. Hashed rather than truncated, so two
    different signal_ids cannot collide down to the same key. Alpaca caps
    client_order_id at 48 characters.
    """
    digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()
    return ("tv-" + digest)[:48]


def is_missing_order_error(exc: APIError) -> bool:
    """
    True only when Alpaca affirmatively says 'no order with that
    client_order_id'. Match the status code, never message text: a 500 whose
    body happens to contain 'not found' must not read as safe-to-submit.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status == MISSING_ORDER_STATUS


# --- Rate limiting -----------------------------------------------------------

_rate_lock = threading.Lock()
_recent_hits: deque = deque()


def _rate_limited(now: float) -> bool:
    """
    Simple sliding-window limiter.

    Scope: this process only. It does NOT coordinate across gunicorn workers
    or across restarts, so treat it as a backstop against an alert loop, not
    as a guarantee. A real per-account ceiling belongs in shared storage.
    """
    cutoff = now - 60.0
    with _rate_lock:
        while _recent_hits and _recent_hits[0] < cutoff:
            _recent_hits.popleft()
        if len(_recent_hits) >= MAX_REQUESTS_PER_MINUTE:
            return True
        _recent_hits.append(now)
        return False


# --- Helpers -----------------------------------------------------------------

def _bad_request(message: str, code: int = 400):
    return jsonify(error=message), code


def _parse_size(payload: dict, is_crypto: bool):
    """
    Returns (kwargs, error_response). Accepts qty or notional, never both.

    Parsed as float first so a fractional equity qty is *rejected* rather than
    silently truncated by int(1.5) == 1, which would quietly fill the wrong
    size instead of telling you the payload was wrong.

    math.isfinite() guards NaN and infinity: NaN compares False against both
    <=0 and >MAX_QTY, so without this it would sail past the range check and
    reach the order request.
    """
    has_qty = payload.get("qty") is not None
    has_notional = payload.get("notional") is not None

    if has_qty and has_notional:
        return None, _bad_request("Provide qty or notional, not both")

    if has_notional:
        try:
            notional = float(payload["notional"])
        except (TypeError, ValueError):
            return None, _bad_request("notional must be a number")
        if not math.isfinite(notional):
            return None, _bad_request("notional must be a finite number")
        if not (1 <= notional <= MAX_NOTIONAL):
            return None, _bad_request(
                f"notional must be between 1 and {MAX_NOTIONAL}"
            )
        return {"notional": round(notional, 2)}, None

    raw = payload.get("qty", DEFAULT_QTY)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, _bad_request("qty must be a number")

    if not math.isfinite(value):
        return None, _bad_request("qty must be a finite number")

    if value <= 0 or value > MAX_QTY:
        return None, _bad_request(
            f"qty must be greater than 0 and at most {MAX_QTY}"
        )

    if not is_crypto and not value.is_integer():
        return None, _bad_request("Fractional qty is only supported for crypto")

    return {"qty": value if is_crypto else int(value)}, None


def resolve_asset(symbol: str):
    """
    Live confirmation the symbol exists, is active and is tradable on this
    account. Returns (asset, error_response).
    """
    try:
        asset = api.get_asset(symbol)
    except APIError as exc:
        if getattr(exc, "status_code", None) == 404:
            return None, (jsonify(error=f"Unknown symbol: {symbol}"), 404)
        logger.exception("Asset lookup failed for %s", symbol)
        return None, (jsonify(error="Asset lookup failed"), 502)
    except Exception:
        logger.exception("Asset lookup failed for %s", symbol)
        return None, (jsonify(error="Asset lookup failed"), 502)

    if asset.status != AssetStatus.ACTIVE:
        return None, (jsonify(error=f"Symbol not active: {symbol}"), 403)
    if not asset.tradable:
        return None, (jsonify(error=f"Symbol not tradable: {symbol}"), 403)

    return asset, None


# --- Routes ------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    """Liveness probe. Reports which environment is armed, no side effects."""
    return jsonify(
        status="ok",
        environment=ENV_LABEL,
        symbols=len(ALLOWED_SYMBOLS),
    ), 200


@app.post("/webhook")
def webhook():
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return _bad_request("Payload too large", 413)

    # Parse from raw bytes. TradingView frequently sends the wrong (or no)
    # Content-Type, so the header is never consulted and cannot cause a 415.
    try:
        raw = request.data.decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        logger.exception("Failed to parse JSON payload")
        return _bad_request("Invalid JSON payload")

    if not isinstance(payload, dict):
        return _bad_request("Invalid JSON payload")

    # Field names and values only. Never the raw body: it holds the secret.
    logger.info(
        "Webhook received [%s]: signal_id=%s symbol=%s action=%s",
        ENV_LABEL,
        payload.get("signal_id"),
        payload.get("symbol"),
        payload.get("action"),
    )

    # --- Auth (constant time) -----------------------------------------------
    supplied_secret = str(payload.get("secret", ""))
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        logger.warning("Webhook rejected: bad secret")
        return jsonify(error="Unauthorized"), 401

    # Rate limit AFTER auth. Counting unauthenticated requests would let
    # anyone with the URL starve your own TradingView alerts for the rest of
    # the window by flooding bad secrets.
    if _rate_limited(time.monotonic()):
        logger.warning("Webhook rejected: rate limited")
        return jsonify(error="Too many requests"), 429

    # --- Validate -----------------------------------------------------------
    raw_symbol = str(payload.get("symbol", ""))
    symbol = normalize_symbol(raw_symbol)
    action = str(payload.get("action", "")).strip().upper()
    signal_id = str(payload.get("signal_id", "")).strip()

    if raw_symbol and raw_symbol.strip().upper() != symbol:
        logger.info("Symbol normalized: %s -> %s", raw_symbol, symbol)

    if not signal_id:
        return _bad_request("signal_id is required")

    if not _SYMBOL_RE.fullmatch(symbol):
        return _bad_request("Invalid symbol")

    if symbol not in ALLOWED_SYMBOLS:
        return jsonify(error="Symbol is not allowed"), 403

    if symbol in DENIED_SYMBOLS:
        return jsonify(error="Symbol is not allowed"), 403

    side_map = {"BUY": OrderSide.BUY, "SELL": OrderSide.SELL}
    side = side_map.get(action)
    if side is None:
        return _bad_request("action must be BUY or SELL")

    permitted = DIRECTION_RESTRICTIONS.get(symbol)
    if permitted is not None and action not in permitted:
        return jsonify(
            error=f"{symbol} only accepts {sorted(permitted)}"
        ), 403

    is_crypto = "/" in symbol

    size_kwargs, size_error = _parse_size(payload, is_crypto)
    if size_error is not None:
        return size_error

    asset, asset_error = resolve_asset(symbol)
    if asset_error is not None:
        return asset_error

    client_order_id = make_client_order_id(signal_id)

    # --- Idempotency: exactly two outcomes, no fall-through -----------------
    # A lookup that fails ambiguously must NOT continue into submission, or an
    # unknown error becomes a duplicate live order.
    try:
        existing = api.get_order_by_client_order_id(client_order_id)
    except APIError as exc:
        if not is_missing_order_error(exc):
            logger.exception("Order lookup failed (ambiguous)")
            return jsonify(error="Order lookup failed"), 502
        # 404 -> definitively absent -> safe to submit below.
    except Exception:
        logger.exception("Order lookup failed (ambiguous)")
        return jsonify(error="Order lookup failed"), 502
    else:
        return jsonify(
            status="duplicate",
            environment=ENV_LABEL,
            symbol=symbol,
            order_id=str(existing.id),
            client_order_id=client_order_id,
        ), 200

    # --- Submit -------------------------------------------------------------
    try:
        clock = api.get_clock()

        # Crypto trades 24/7; equities only inside the session.
        if not is_crypto and not clock.is_open:
            return jsonify(error="Market is closed"), 409

        account = api.get_account()
        if account.trading_blocked:
            return jsonify(error="Account is blocked from trading"), 403

        order = api.submit_order(
            order_data=MarketOrderRequest(
                symbol=symbol,
                side=side,
                # Crypto takes GTC; equities use DAY.
                time_in_force=TimeInForce.GTC if is_crypto else TimeInForce.DAY,
                client_order_id=client_order_id,
                **size_kwargs,
            )
        )

        logger.info(
            "Order submitted [%s]: symbol=%s side=%s size=%s "
            "order_id=%s client_order_id=%s",
            ENV_LABEL,
            symbol,
            side.value,
            size_kwargs,
            order.id,
            client_order_id,
        )

        return jsonify(
            status="submitted",
            environment=ENV_LABEL,
            symbol=symbol,
            side=side.value,
            **size_kwargs,
            order_id=str(order.id),
            client_order_id=client_order_id,
        ), 202

    except APIError:
        logger.exception("Alpaca rejected order")
        return jsonify(error="Order rejected by Alpaca"), 502

    except Exception:
        logger.exception("Order submission failed")
        return jsonify(error="Order submission failed"), 502


if __name__ == "__main__":
    logger.info(
        "Starting webhook: environment=%s symbols=%d",
        ENV_LABEL,
        len(ALLOWED_SYMBOLS),
    )
    app.run(host="0.0.0.0", port=int(_optional_env("PORT") or "5000"))
That's the complete file — imports through entrypoint, nothing elided. It's also saved at outputs/app.py if you'd rather download it.

Two things to install and set before running:

bash


pip install flask alpaca-py
export ALPACA_API_KEY=... ALPACA_SECRET_KEY=... WEBHOOK_SECRET=...
python app.py
Leave ALPACA_PAPER unset for paper mode. It's Sunday, so an equity alert returns 409 Market is closed while crypto fills — the clock gate behaving correctly.
