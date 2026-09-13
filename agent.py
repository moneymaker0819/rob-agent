from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os

app = Flask(__name__)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_API_SECRET, BASE_URL)

@app.route("/api/trade-signal", methods=["POST"])
def trade_signal():
    data = request.get_json()

    ticker = data.get("ticker")
    side = data.get("direction")
    qty = 1

    if side == "BUY":
        api.submit_order(symbol=ticker, qty=qty, side="buy", type="market", time_in_force="gtc")
    elif side == "SELL":
        api.submit_order(symbol=ticker, qty=qty, side="sell", type="market", time_in_force="gtc")

    return jsonify({"status": "order sent", "ticker": ticker, "side": side})

@app.route("/", methods=["GET"])
def home():
    return "Agent is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

