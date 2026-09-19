from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "UFE Running"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print(data)
    return {"status": "received"}
