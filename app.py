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
# Connect to Alpaca Paper Trading
8
api = tradeapi.REST(
9
os.environ["APCA_API_KEY_ID"],
10
os.environ["APCA_API_SECRET_KEY"],
11
"https://paper-api.alpaca.markets",
12
api_version="v2"
13
)
14
 
15
@app.route("/")
16
def home():
17
return "Rob Agent Running", 200
18
 
19
 
20
@app.route("/webhook", methods=["POST"])
21
def webhook():
22
 
23
payload = request.get_json(silent=True)
24
 
25
print("JSON Data:", payload)
26
 
27
if payload:
28
 
29
symbol = payload.get("symbol")
30
action = payload.get("action")
31
 
32
if action == "BUY":
33
 
34
api.submit_order(
35
symbol=symbol,
36
qty=1,
37
side="buy",
38
type="market",
39
time_in_force="gtc"
40
)
41
 
42
print(f"BUY order submitted for {symbol}")
43
 
44
return {"status": "received"}, 200
45
 
46
 
47
if __name__ == "__main__":
48
app.run(host="0.0.0.0", port=5000)
