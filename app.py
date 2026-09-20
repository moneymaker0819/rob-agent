@app.route("/webhook", methods=["POST"])
2
def webhook():
3
 
4
payload = request.get_json(silent=True)
5
 
6
print("JSON Data:", payload)
7
 
8
if payload:
9
 
10
symbol = payload.get("symbol")
11
action = payload.get("action")
12
 
13
if action == "BUY":
14
 
15
api.submit_order(
16
symbol=symbol,
17
qty=1,
18
side="buy",
19
type="market",
20
time_in_force="gtc"
21
)
22
 
23
print(f"BUY order submitted for {symbol}")
24
 
25
return {"status": "received"}, 200
