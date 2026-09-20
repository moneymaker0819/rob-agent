Python
1
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
return "Rob Agent Running", 200
8
 
9
@app.route("/webhook", methods=["POST"])
10
def webhook():
11
print("Headers:", dict(request.headers))
12
print("Raw Data:", request.get_data(as_text=True))
13
 
14
return {
15
"status": "received"
16
}, 200
