@app.post("/webhook")
def webhook():
 
if api is None or not WEBHOOK_SECRET:
logger.error("Missing configuration")
return jsonify(error="Server configuration error"), 503
 
# Accept JSON or text/plain from TradingView
payload = request.get_json(silent=True)
 
if payload is None:
try:
raw_body = request.data.decode("utf-8")
logger.info("Raw body: %s", raw_body)
payload = json.loads(raw_body)
except Exception:
logger.exception("Failed to parse webhook payload")
return jsonify(
error="Unable to parse webhook payload"
), 400
 
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
signal_id = str(payload.get("signal_id", "")).strip()
 
qty = float(payload.get("qty", 1))
 
logger.info(
"Signal received: symbol=%s action=%s qty=%s signal_id=%s",
symbol,
action,
qty,
signal_id,
)
 
if not signal_id:
return jsonify(error="signal_id is required"), 400
 
if action not in ["BUY", "SELL"\]:
return jsonify(error="Invalid action"), 400
 
if symbol not in ALLOWED_SYMBOLS:
return jsonify(
error=f"Symbol not allowed: {symbol}"
), 403
 
client_order_id = make_client_order_id(signal_id)
 
try:
existing_order = api.get_order_by_client_order_id(
client_order_id
)
 
return jsonify(
status="duplicate",
symbol=symbol,
order_id=str(existing_order.id),
client_order_id=client_order_id,
), 200
 
except APIError as exc:
if not is_missing_order_error(exc):
logger.exception("Order lookup failed")
return sonify(error="Order lookup failed"), 502
 
side = "buy" if action == "BUY" else "sell"
 
try:
order = api.submit_order(
symbol=symbol,
qty=qty,
side=side,
type="market",
time_in_force="gtc",
client_order_id=client_order_id,
)
 
logger.info(
"Order submitted: %s %s %s",
side,
qty,
symbol,
)
 
return jsonify(
status="submitted",
symbol=symbol,
side=side,
qty=qty,
order_id=str(order.id),
), 202
 
except Exception:
logger.exception("Order submission failed")
return jsonify(
error="Order submission failed"
), 502
