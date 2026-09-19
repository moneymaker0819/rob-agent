@app.route("/webhook", methods=["POST"])

def webhook():

print("Headers:", request.headers)

print("Raw Data:", request.data)

return {"status": "received"}, 200
