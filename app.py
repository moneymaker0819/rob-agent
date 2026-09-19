from flask import Flask,
request 

app = Flask(__name__)
 
@app.route("/")
def home():
    return "UFE Running"

@app.route("/webhook",
methods=["POST"])
def webhook():
    print("Headers:",
request.headers)
    print("Raw Data:",
request.data)

    return {"status":
"received"}, 200
