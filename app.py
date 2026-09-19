from flask import Flask, request
2
 
3
app = Flask(__name__)
4
 
5
@app.route("/")
6
def home():
7
return "UFE Running"
8
 
9
@app.route("/webhook", methods=["POST"])
10
def webhook():
11
print("Headers:", request.headers)
12
print("Raw Data:", request.data)
13
 
14
return {"status": "received"}, 200
