@app.route("/webhook", methods=["POST"])
2
def webhook():
3
print("Headers:", request.headers)
4
print("Raw Data:", request.data)
5
return {"status": "received"}, 200
