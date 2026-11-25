
import os
import time
import secrets
import string
import re
import logging

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client


# CONFIGURATION
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)


# GAME STORAGE
waiting_player = None
rooms = {}
sid_room = {}


# ERROR HANDLING
def database_error(action):
    app.logger.exception("Database error during: %s", action)
    return jsonify({
        "error": (
            "The database request failed. "
            "Open Render Logs and check the newest error."
        )
    }), 500


# ACCOUNT HELPERS
def get_player_by_username(username):
    result = (
        supabase.table("players")
        .select("*")
        .eq("username", username)
        .execute()
    )
    return result.data[0] if result.data else None


def get_player_by_id(player_id):
    result = (
        supabase.table("players")
        .select("id, username, rating")
        .eq("id", player_id)
        .execute()
    )
    return result.data[0] if result.data else None


def current_player():
    player_id = session.get("player_id")
    if not player_id:
        return None
    return get_player_by_id(player_id)


def valid_username(username):
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", username))


def account_response(player):
    return {
        "id": player["id"],
        "username": player["username"],
        "rating": player["rating"]
    }


# WEBSITE
@app.route("/")
def home():
    return render_template("index.html")


# REGISTER
@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.get_json(silent=True) or {}

        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))

        if not valid_username(username):
            return jsonify({
                "error": (
                    "Username must be 3–16 characters "
                    "and use only letters, numbers or underscores."
                )
            }), 400

        if len(password) < 8 or len(password) > 128:
            return jsonify({
                "error": "Password must be between 8 and 128 characters."
            }), 400

        username = username.lower()

        if get_player_by_username(username):
            return jsonify({
                "error": "That username is already taken."
            }), 409

        password_hash = generate_password_hash(password)

        result = (
            supabase.table("players")
            .insert({
                "username": username,
                "password_hash": password_hash,
                "rating": 1000
            })
            .execute()
        )

        if not result.data:
            raise Exception("Supabase did not return the new player.")

        player = result.data[0]

        session.clear()
        session["player_id"] = player["id"]

        return jsonify({
            "success": True,
            "player": account_response(player)
        })

    except Exception:
        return database_error("account registration")


# LOGIN
@app.route("/api/login", methods=["POST"])
def login():
    try:
        data = request.get_json(silent=True) or {}

        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", ""))

        player = get_player_by_username(username)

        if not player or not check_password_hash(
            player["password_hash"], password
        ):
            return jsonify({
                "error": "Incorrect username or password."
            }), 401

        session.clear()
        session["player_id"] = player["id"]

        return jsonify({
            "success": True,
            "player": account_response(player)
        })

    except Exception:
        return database_error("account login")


# LOGOUT
@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


# CURRENT ACCOUNT
@app.route("/api/me")
def me():
    try:
        player = current_player()

        if not player:
            return jsonify({"logged_in": False})

        return jsonify({
            "logged_in": True,
            "player": account_response(player)
        })

    except Exception:
        return database_error("loading current account")


# CHANGE USERNAME
@app.route("/api/change-username", methods=["POST"])
def change_username():
    try:
        player = current_player()

        if not player:
            return jsonify({
                "error": "You must be logged in."
            }), 401

        data = request.get_json(silent=True) or {}
        username = str(data.get("username", "")).strip()

        if not valid_username(username):
            return jsonify({
                "error": (
                    "Username must be 3–16 characters "
                    "and use only letters, numbers or underscores."
                )
            }), 400

        username = username.lower()
        existing = get_player_by_username(username)

        if existing and existing["id"] != player["id"]:
            return jsonify({
                "error": "That username is already taken."
            }), 409

        result = (
            supabase.table("players")
            .update({"username": username})
            .eq("id", player["id"])
            .execute()
        )

        if not result.data:
            raise Exception("Supabase did not return the updated player.")

        updated = result.data[0]

        return jsonify({
            "success": True,
            "player": account_response(updated)
        })

    except Exception:
        return database_error("changing username")


# ROOM HELPERS
def make_room_code():
    alphabet = string.ascii_uppercase + "23456789"

    while True:
        code = "".join(
            secrets.choice(alphabet) for _ in range(6)
        )

        if code not in rooms:
            return code


def new_room(white, black, seconds, increment=0):
    code = make_room_code()

    rooms[code] = {
        "white": white,
        "black": black,
        "clocks": {
            "w": int(seconds),
            "b": int(seconds)
        },
        "increment": int(increment),
        "active": "w",
        "unlimited": int(seconds) == 0,
        "last": time.monotonic(),
        "started": False,
        "over": False,
        "rated": False
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


# RANDOM MATCHMAKING
@socketio.on("find_match")
def find_match(data=None):
    global waiting_player

    data = data or {}
    sid = request.sid

    seconds = max(
        0, min(7200, int(data.get("seconds", 300)))
    )

    increment = max(
        0, min(60, int(data.get("increment", 0)))
    )

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


# PRIVATE LOBBIES
@socketio.on("create_private_lobby")
def create_private_lobby(data=None):
    data = data or {}
    sid = request.sid

    seconds = max(
        0, min(7200, int(data.get("seconds", 300)))
    )

    increment = max(
        0, min(60, int(data.get("increment", 0)))
    )

    code = make_room_code()

    rooms[code] = {
        "white": sid,
        "black": None,
        "clocks": {
            "w": seconds,
            "b": seconds
        },
        "increment": increment,
        "active": "w",
        "unlimited": seconds == 0,
        "last": time.monotonic(),
        "started": False,
        "over": False,
        "rated": False
    }

    sid_room[sid] = code
    join_room(code)

    emit("private_lobby_created", {
        "code": code,
        "room": code,
        "seconds": seconds,
        "increment": increment
    })


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
            "message": (
                "You created this lobby. "
                "Share the code with your opponent."
            )
        })
        return

    room["black"] = request.sid
    sid_room[request.sid] = code

    join_room(code)
    emit_match(code)


# ONLINE MOVES
@socketio.on("online_move")
def online_move(data=None):
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
        elapsed = max(
            0, now - room["last"]
        )

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

    move = data.get("move")

    if not isinstance(move, dict):
        return

    try:
        fr = int(move["fr"])
        fc = int(move["fc"])
        tr = int(move["tr"])
        tc = int(move["tc"])

        if not all(
            0 <= x <= 7 for x in (fr, fc, tr, tc)
        ):
            return

    except (KeyError, TypeError, ValueError):
        return

    room["started"] = True
    room["clocks"][side] += room["increment"]
    room["active"] = "b" if side == "w" else "w"
    room["last"] = now

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


# RESIGNATION
@socketio.on("online_resign")
def online_resign(data=None):
    code = (data or {}).get("room")
    room = rooms.get(code)

    if room and request.sid in (
        room["white"], room["black"]
    ):
        room["over"] = True

        emit(
            "opponent_resigned",
            to=code,
            include_self=False
        )


# DISCONNECT
@socketio.on("disconnect")
def disconnect():
    global waiting_player

    if waiting_player and waiting_player["sid"] == request.sid:
        waiting_player = None

    code = sid_room.pop(request.sid, None)

    if code and code in rooms:
        room = rooms[code]

        if request.sid in (
            room["white"], room["black"]
        ):
            socketio.emit(
                "opponent_disconnected",
                to=code,
                include_self=False
            )

            rooms.pop(code, None)


# CLOCK WORKER
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

            elapsed = max(
                0, now - room["last"]
            )

            remaining = (
                room["clocks"][room["active"]] - elapsed
            )

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


# START SERVER
if __name__ == "__main__":
    print("Mouseslip Chess server starting...")

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False,
        allow_unsafe_werkzeug=True
    )
