from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "Rob Agent Running", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    print("Headers:", dict(request.headers))
    print("Raw Data:", request.get_data(as_text=True))

    payload = request.get_json(silent=True)
    if payload is not None:
        print("JSON Data:", payload)
    else:
        print("No JSON payload received")

    return {
        "status": "received",
        "data": payload
    }, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
