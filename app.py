
import os
import time
import secrets
import string
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

waiting_player = None
rooms = {}
sid_room = {}


def make_room_code():
    alphabet = string.ascii_uppercase + "23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


def new_room(white, black, seconds, increment=0):
    code = make_room_code()
    rooms[code] = {
        "white": white,
        "black": black,
        "clocks": {"w": int(seconds), "b": int(seconds)},
        "increment": int(increment),
        "active": "w",
        "unlimited": int(seconds) == 0,
        "last": time.monotonic(),
        "started": False,
        "over": False
    }

    sid_room[white] = code
    sid_room[black] = code

    join_room(code, sid=white)
    join_room(code, sid=black)

    return code


def emit_match(code):
    room = rooms[code]

    emit("match_found", {
        "room": code,
        "color": "w",
        "seconds": room["clocks"]["w"],
        "increment": room["increment"]
    }, to=room["white"])

    emit("match_found", {
        "room": code,
        "color": "b",
        "seconds": room["clocks"]["b"],
        "increment": room["increment"]
    }, to=room["black"])


@app.route("/")
def home():
    return render_template("index.html")


@socketio.on("find_match")
def find_match(data=None):
    global waiting_player

    data = data or {}
    sid = request.sid

    seconds = max(0, int(data.get("seconds", 300)))
    increment = max(0, int(data.get("increment", 0)))

    if waiting_player and waiting_player["sid"] != sid:
        other = waiting_player
        waiting_player = None

        code = new_room(
            other["sid"],
            sid,
            other["seconds"],
            other["increment"]
        )

        emit_match(code)

    else:
        waiting_player = {
            "sid": sid,
            "seconds": seconds,
            "increment": increment
        }

        emit("searching", {
            "message": "Looking for an opponent..."
        })


@socketio.on("cancel_match")
def cancel_match():
    global waiting_player

    if waiting_player and waiting_player["sid"] == request.sid:
        waiting_player = None

    emit("match_cancelled")


@socketio.on("create_private_lobby")
def create_private_lobby(data=None):
    data = data or {}
    sid = request.sid

    old = sid_room.get(sid)
    if old:
        rooms.pop(old, None)

    seconds = max(0, int(data.get("seconds", 300)))
    increment = max(0, int(data.get("increment", 0)))

    code = make_room_code()

    rooms[code] = {
        "white": sid,
        "black": None,
        "clocks": {"w": seconds, "b": seconds},
        "increment": increment,
        "active": "w",
        "unlimited": seconds == 0,
        "last": time.monotonic(),
        "started": False,
        "over": False
    }

    sid_room[sid] = code
    join_room(code)

    socketio.emit("private_lobby_created", {
        "code": code,
        "room": code,
        "seconds": seconds,
        "increment": increment
    }, to=sid)


@socketio.on("join_private_lobby")
def join_private_lobby(data=None):
    data = data or {}

    code = str(data.get("code", "")).strip().upper()
    room = rooms.get(code)

    if not room or room["black"] is not None:
        emit("private_lobby_error", {
            "message": "That code is invalid or the lobby is full."
        })
        return

    if room["white"] == request.sid:
        emit("private_lobby_error", {
            "message": "You created this lobby. Share the code with your opponent."
        })
        return

    room["black"] = request.sid
    sid_room[request.sid] = code

    join_room(code)
    emit_match(code)


@socketio.on("online_move")
def online_move(data):
    data = data or {}

    code = data.get("room")
    room = rooms.get(code)

    if not room or room["over"]:
        return

    if request.sid not in (room["white"], room["black"]):
        return

    side = "w" if request.sid == room["white"] else "b"
    now = time.monotonic()

    if not room["started"]:
        if side != "w":
            return

    elif not room["unlimited"]:
        elapsed = max(0, now - room["last"])
        room["clocks"][room["active"]] -= elapsed

        if room["clocks"][room["active"]] <= 0:
            room["clocks"][room["active"]] = 0
            room["over"] = True

            socketio.emit(
                "clock_flag",
                {"loser": room["active"]},
                to=code
            )
            return

    if side != room["active"]:
        return

    room["started"] = True
    room["clocks"][side] += room["increment"]

    room["active"] = "b" if side == "w" else "w"
    room["last"] = now

    move = data.get("move")

    socketio.emit(
        "opponent_move",
        move,
        to=code,
        include_self=False
    )

    socketio.emit("clock_update", {
        "clocks": room["clocks"],
        "active": room["active"],
        "increment": room["increment"]
    }, to=code)


@socketio.on("online_resign")
def online_resign(data):
    code = (data or {}).get("room")
    room = rooms.get(code)

    if room and request.sid in (room["white"], room["black"]):
        room["over"] = True
        emit("opponent_resigned", to=code, include_self=False)


@socketio.on("disconnect")
def disconnect():
    global waiting_player

    if waiting_player and waiting_player["sid"] == request.sid:
        waiting_player = None

    code = sid_room.pop(request.sid, None)

    if code and code in rooms:
        room = rooms[code]

        if request.sid in (room["white"], room["black"]):
            socketio.emit(
                "opponent_disconnected",
                to=code,
                include_self=False
            )

            rooms.pop(code, None)


def clock_worker():
    while True:
        socketio.sleep(0.25)
        now = time.monotonic()

        for code, room in list(rooms.items()):
            if (
                not room["started"]
                or room["over"]
                or room["black"] is None
                or room["unlimited"]
            ):
                continue

            elapsed = max(0, now - room["last"])
            remaining = room["clocks"][room["active"]] - elapsed

            if remaining <= 0:
                room["clocks"][room["active"]] = 0
                room["over"] = True

                socketio.emit(
                    "clock_flag",
                    {"loser": room["active"]},
                    to=code
                )

            else:
                socketio.emit("clock_tick", {
                    "active": room["active"],
                    "remaining": remaining
                }, to=code)


socketio.start_background_task(clock_worker)


if __name__ == "__main__":
    print("Mouseslip Chess server starting at http://127.0.0.1:5000")

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False,
        allow_unsafe_werkzeug=True
    )
