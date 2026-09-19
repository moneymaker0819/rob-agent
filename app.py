from flask import Flask, request

app=Flask(_name_)
@app.route("/")
def home():
    return "UFE Running"

@app.route("/webhook", methods=["POST"])
6
def webhook():
7
print("Headers:", request.headers)
8
print("Raw Data:", request.data)
9
 
10
return {"status": "received"}, 200
