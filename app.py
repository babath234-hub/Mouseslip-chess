import os
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import secrets

app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

waiting_player = None
rooms = {}

@app.route("/")
def home():
    return render_template("index.html")

@socketio.on("find_match")
def find_match(data=None):
    global waiting_player
    sid = request.sid
    if waiting_player and waiting_player != sid:
        other = waiting_player
        waiting_player = None
        room = secrets.token_urlsafe(8)
        rooms[room] = {"white": other, "black": sid}
        join_room(room, sid=other)
        join_room(room, sid=sid)
        emit("match_found", {"room": room, "color": "w"}, to=other)
        emit("match_found", {"room": room, "color": "b"}, to=sid)
    else:
        waiting_player = sid
        emit("searching", {"message": "Looking for an opponent..."})

@socketio.on("cancel_match")
def cancel_match():
    global waiting_player
    if waiting_player == request.sid:
        waiting_player = None
    emit("match_cancelled")

@socketio.on("online_move")
def online_move(data):
    room = data.get("room")
    if room in rooms and request.sid in rooms[room].values():
        emit("opponent_move", data.get("move"), to=room, include_self=False)

@socketio.on("online_resign")
def online_resign(data):
    room = data.get("room")
    if room in rooms and request.sid in rooms[room].values():
        emit("opponent_resigned", to=room, include_self=False)

@socketio.on("disconnect")
def disconnect():
    global waiting_player
    if waiting_player == request.sid:
        waiting_player = None
    for room, players in list(rooms.items()):
        if request.sid in players.values():
            emit("opponent_disconnected", room=room, include_self=False)
            rooms.pop(room, None)

if __name__ == "__main__":
    print("Mouseslip Chess server starting at http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False, allow_unsafe_werkzeug=True)
