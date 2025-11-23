from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'newbiz_secret'

# 必须指定 async_mode='eventlet'，否则 Render 会报错
socketio = SocketIO(app, async_mode='eventlet')

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def test_connect():
    emit("message", {"msg": "✔ 成功连接到 NewBiz Render 服务器！"})

@socketio.on("send_action")
def receive_action(data):
    """
    玩家提交的操作事件（例如：买原料、生产、零售等）
    你可以在这里写你的商赛逻辑
    """
    emit("broadcast_action", data, broadcast=True)

if __name__ == "__main__":
    # Render 不允许指定 host/port → 必须由 gunicorn 接管
    socketio.run(app)






