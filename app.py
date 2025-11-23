from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "newbiz_secret"

# 使用 gevent，兼容 Render（Python 3.12）
socketio = SocketIO(app, async_mode="gevent")

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("connect")
def connected():
    emit("message", {"msg": "✔ 成功连接到服务器（gevent + Render）！"})

@socketio.on("send_action")
def receive_action(data):
    # 示例：把某人动作实时广播给所有玩家
    emit("broadcast_action", data, broadcast=True)

if __name__ == "__main__":
    socketio.run(app)







