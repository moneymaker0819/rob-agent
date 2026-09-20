from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "Rob Agent Running", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    print("Headers:", dict(request.headers))
    print("Raw Data:", request.get_data(as_text=True))

    return {
        "status": "received"
    }, 200
