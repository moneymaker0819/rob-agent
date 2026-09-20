Python
1
import os
2
from flask import Flask, request
3
import alpaca_trade_api as tradeapi
4
 
5
app = Flask(__name__)
6
 
7
api = tradeapi.REST(
8
os.environ["APCA_API_KEY_ID"],
9
os.environ["APCA_API_SECRET_KEY"],
10
"https://paper-api.alpaca.markets",
11
api_version="v2"
12
)
13
 
14
@app.route("/")
15
def home():
16
return "Rob Agent Running", 200
17
 
18
@app.route("/webhook", methods=["POST"])
19
def webhook():
20
payload = request.get_json(silent=True)
21
 
22
print("JSON Data:", payload)
23
 
24
if payload:
25
symbol = payload.get("symbol")
26
action = payload.get("action")
27
 
28
if action == "BUY":
29
api.submit_order(
30
symbol=symbol,
31
qty=1,
32
side="buy",
33
type="market",
34
time_in_force="gtc"
35
)
36
 
37
print(f"BUY order submitted for {symbol}")
38
 
39
return {"status": "received"}, 200
40
 
41
if __name__ == "__main__":
42
app.run(host="0.0.0.0", port=5000)
