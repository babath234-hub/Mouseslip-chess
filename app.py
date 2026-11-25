
import os
import re
import time
import secrets
import string
import logging

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)

rooms = {}
sid_room = {}
waiting_players = []


# ---------------- ACCOUNT FUNCTIONS ----------------

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
        .select("id,username,rating")
        .eq("id", player_id)
        .execute()
    )
    return result.data[0] if result.data else None


def current_player():
    player_id = session.get("player_id")
    return get_player_by_id(player_id) if player_id else None


def valid_username(username):
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", username))


def account_response(player):
    return {
        "id": player["id"],
        "username": player["username"],
        "rating": player["rating"]
    }


def database_error(action):
    app.logger.exception("Database error: %s", action)
    return jsonify({"error": "Database error. Check the Render logs."}), 500


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.get_json(silent=True) or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))

        if not valid_username(username):
            return jsonify({
                "error": "Username must be 3–16 characters and use only letters, numbers or underscores."
            }), 400

        if not 8 <= len(password) <= 128:
            return jsonify({
                "error": "Password must be between 8 and 128 characters."
            }), 400

        username = username.lower()

        if get_player_by_username(username):
            return jsonify({"error": "That username is already taken."}), 409

        result = supabase.table("players").insert({
            "username": username,
            "password_hash": generate_password_hash(password),
            "rating": 1000
        }).execute()

        if not result.data:
            raise Exception("Player was not created.")

        player = result.data[0]
        session.clear()
        session["player_id"] = player["id"]

        return jsonify({
            "success": True,
            "player": account_response(player)
        })

    except Exception:
        return database_error("registration")


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
            return jsonify({"error": "Incorrect username or password."}), 401

        session.clear()
        session["player_id"] = player["id"]

        return jsonify({
            "success": True,
            "player": account_response(player)
        })

    except Exception:
        return database_error("login")


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


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
        return database_error("loading account")


@app.route("/api/change-username", methods=["POST"])
def change_username():
    try:
        player = current_player()

        if not player:
            return jsonify({"error": "You must be logged in."}), 401

        data = request.get_json(silent=True) or {}
        username = str(data.get("username", "")).strip()

        if not valid_username(username):
            return jsonify({
                "error": "Username must be 3–16 characters and use only letters, numbers or underscores."
            }), 400

        username = username.lower()
        existing = get_player_by_username(username)

        if existing and existing["id"] != player["id"]:
            return jsonify({"error": "That username is already taken."}), 409

        result = (
            supabase.table("players")
            .update({"username": username})
            .eq("id", player["id"])
            .execute()
        )

        return jsonify({
            "success": True,
            "player": account_response(result.data[0])
        })

    except Exception:
        return database_error("changing username")


# ---------------- CHESS BOARD ----------------

def initial_board():
    return [
        list("rnbqkbnr"),
        list("pppppppp"),
        list("........"),
        list("........"),
        list("........"),
        list("........"),
        list("PPPPPPPP"),
        list("RNBQKBNR")
    ]


def color_of(piece):
    if piece == ".":
        return None
    return "w" if piece.isupper() else "b"


def inside(r, c):
    return 0 <= r < 8 and 0 <= c < 8


def make_moves(board, r, c, rights):
    piece = board[r][c]

    if piece == ".":
        return []

    color = color_of(piece)
    kind = piece.lower()
    moves = []

    def add(rr, cc):
        if not inside(rr, cc):
            return False

        target = board[rr][cc]

        if color_of(target) == color:
            return False

        moves.append({
            "fr": r, "fc": c,
            "tr": rr, "tc": cc
        })

        return target == "."

    if kind == "p":
        direction = -1 if color == "w" else 1
        start = 6 if color == "w" else 1

        if inside(r + direction, c) and board[r + direction][c] == ".":
            add(r + direction, c)

            if r == start and board[r + 2 * direction][c] == ".":
                add(r + 2 * direction, c)

        for dc in (-1, 1):
            rr, cc = r + direction, c + dc
            if inside(rr, cc) and board[rr][cc] != ".":
                if color_of(board[rr][cc]) != color:
                    add(rr, cc)

    elif kind == "n":
        for dr, dc in [
            (-2, -1), (-2, 1), (-1, -2), (-1, 2),
            (1, -2), (1, 2), (2, -1), (2, 1)
        ]:
            add(r + dr, c + dc)

    else:
        directions = []

        if kind in ("b", "q"):
            directions += [
                (-1, -1), (-1, 1), (1, -1), (1, 1)
            ]

        if kind in ("r", "q"):
            directions += [
                (-1, 0), (1, 0), (0, -1), (0, 1)
            ]

        if kind == "k":
            directions = [
                (-1, -1), (-1, 0), (-1, 1),
                (0, -1), (0, 1),
                (1, -1), (1, 0), (1, 1)
            ]

        for dr, dc in directions:
            rr, cc = r + dr, c + dc

            while inside(rr, cc):
                empty = add(rr, cc)

                if kind == "k" or not empty:
                    break

                rr += dr
                cc += dc

        if kind == "k":
            home = 7 if color == "w" else 0
            rook = "R" if color == "w" else "r"

            if r == home and c == 4:
                if (
                    rights[color + "K"]
                    and board[home][7] == rook
                    and board[home][5] == "."
                    and board[home][6] == "."
                ):
                    moves.append({
                        "fr": r, "fc": c,
                        "tr": home, "tc": 6,
                        "castle": "K"
                    })

                if (
                    rights[color + "Q"]
                    and board[home][0] == rook
                    and board[home][1] == "."
                    and board[home][2] == "."
                    and board[home][3] == "."
                ):
                    moves.append({
                        "fr": r, "fc": c,
                        "tr": home, "tc": 2,
                        "castle": "Q"
                    })

    return moves


def update_rights(room, move, moving, captured):
    rights = room["rights"]

    if moving == "K":
        rights["wK"] = False
        rights["wQ"] = False

    if moving == "k":
        rights["bK"] = False
        rights["bQ"] = False

    if moving == "R" and move["fr"] == 7 and move["fc"] == 7:
        rights["wK"] = False
    if moving == "R" and move["fr"] == 7 and move["fc"] == 0:
        rights["wQ"] = False
    if moving == "r" and move["fr"] == 0 and move["fc"] == 7:
        rights["bK"] = False
    if moving == "r" and move["fr"] == 0 and move["fc"] == 0:
        rights["bQ"] = False

    if captured == "R" and move["tr"] == 7 and move["tc"] == 7:
        rights["wK"] = False
    if captured == "R" and move["tr"] == 7 and move["tc"] == 0:
        rights["wQ"] = False
    if captured == "r" and move["tr"] == 0 and move["tc"] == 7:
        rights["bK"] = False
    if captured == "r" and move["tr"] == 0 and move["tc"] == 0:
        rights["bQ"] = False


def apply_move(room, move):
    board = room["board"]
    fr, fc = move["fr"], move["fc"]
    tr, tc = move["tr"], move["tc"]

    moving = board[fr][fc]
    captured = board[tr][tc]

    update_rights(room, move, moving, captured)

    board[fr][fc] = "."
    board[tr][tc] = moving

    if moving.lower() == "k" and fr == tr and abs(tc - fc) == 2:
        if tc == 6:
            board[fr][5] = board[fr][7]
            board[fr][7] = "."
        else:
            board[fr][3] = board[fr][0]
            board[fr][0] = "."

    promotion = move.get("promotion")

    if moving == "P" and tr == 0:
        if promotion not in ("Q", "R", "B", "N"):
            promotion = "Q"
        board[tr][tc] = promotion

    elif moving == "p" and tr == 7:
        if promotion not in ("q", "r", "b", "n"):
            promotion = "q"
        board[tr][tc] = promotion

    return captured


# ---------------- ROOM HELPERS ----------------

def make_room_code():
    alphabet = string.ascii_uppercase + "23456789"

    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


def new_room(white, black, seconds, increment=0, rated=False):
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
        "over": False,
        "rated": rated,
        "board": initial_board(),
        "rights": {
            "wK": True, "wQ": True,
            "bK": True, "bQ": True
        },
        "turn": "w",
        "players": {
            "w": white["player_id"],
            "b": black["player_id"]
        }
    }

    sid_room[white["sid"]] = code
    sid_room[black["sid"]] = code

    join_room(code, sid=white["sid"])
    join_room(code, sid=black["sid"])

    return code


def emit_match(code):
    room = rooms[code]

    emit("match_found", {
        "room": code,
        "color": "w",
        "seconds": room["clocks"]["w"],
        "increment": room["increment"]
    }, to=room["white"]["sid"])

    emit("match_found", {
        "room": code,
        "color": "b",
        "seconds": room["clocks"]["b"],
        "increment": room["increment"]
    }, to=room["black"]["sid"])


def award_rating(code, winner_side):
    room = rooms.get(code)

    if not room or not room["rated"] or room.get("rating_awarded"):
        return

    winner_id = room["players"][winner_side]
    white_id = room["players"]["w"]
    black_id = room["players"]["b"]

    try:
        supabase.rpc("award_match_rating", {
            "p_match_id": code,
            "p_white": white_id,
            "p_black": black_id,
            "p_winner": winner_id
        }).execute()

        room["rating_awarded"] = True

        for side in ("w", "b"):
            player_id = room["players"][side]
            player = get_player_by_id(player_id)

            if player:
                socketio.emit(
                    "rating_updated",
                    account_response(player),
                    to=room[side_name(room, side)]["sid"]
                )

    except Exception:
        app.logger.exception("Could not award match rating")


def side_name(room, side):
    return "white" if side == "w" else "black"


def finish_by_time(code, loser):
    room = rooms.get(code)

    if not room or room["over"]:
        return

    room["over"] = True
    winner = "b" if loser == "w" else "w"

    if room["rated"]:
        award_rating(code, winner)

    socketio.emit("clock_flag", {"loser": loser}, to=code)


# ---------------- MATCHMAKING ----------------

@socketio.on("find_match")
def find_match(data=None):
    data = data or {}
    player = current_player()

    if not player:
        emit("match_error", {"message": "Log in before finding a rated opponent."})
        return

    sid = request.sid

    if sid in sid_room:
        emit("match_error", {"message": "You are already in a match."})
        return

    seconds = max(0, min(7200, int(data.get("seconds", 300))))
    increment = max(0, min(60, int(data.get("increment", 0))))

    waiting_players[:] = [
        item for item in waiting_players if item["sid"] != sid
    ]

    now = time.monotonic()
    chosen = None
    chosen_difference = None

    for item in waiting_players:
        waited = now - item["joined"]
        allowed_difference = 100 + int(waited // 5) * 50

        if waited >= 60:
            allowed_difference = 1000000

        difference = abs(item["rating"] - player["rating"])

        if difference <= allowed_difference:
            if chosen is None or difference < chosen_difference:
                chosen = item
                chosen_difference = difference

    if chosen:
        waiting_players.remove(chosen)

        white = {
            "sid": chosen["sid"],
            "player_id": chosen["player_id"]
        }
        black = {
            "sid": sid,
            "player_id": player["id"]
        }

        code = new_room(
            white, black,
            chosen["seconds"],
            chosen["increment"],
            rated=True
        )
        emit_match(code)

    else:
        waiting_players.append({
            "sid": sid,
            "player_id": player["id"],
            "rating": player["rating"],
            "seconds": seconds,
            "increment": increment,
            "joined": now
        })

        emit("searching", {"message": "Searching for an opponent..."})


@socketio.on("cancel_match")
def cancel_match():
    waiting_players[:] = [
        item for item in waiting_players if item["sid"] != request.sid
    ]
    emit("match_cancelled")


# ---------------- PRIVATE LOBBIES ----------------

@socketio.on("create_private_lobby")
def create_private_lobby(data=None):
    data = data or {}
    sid = request.sid

    if sid in sid_room:
        emit("private_lobby_error", {"message": "You are already in a match."})
        return

    seconds = max(0, min(7200, int(data.get("seconds", 300))))
    increment = max(0, min(60, int(data.get("increment", 0))))

    code = make_room_code()

    rooms[code] = {
        "white": {"sid": sid, "player_id": None},
        "black": None,
        "clocks": {"w": seconds, "b": seconds},
        "increment": increment,
        "active": "w",
        "unlimited": seconds == 0,
        "last": time.monotonic(),
        "started": False,
        "over": False,
        "rated": False,
        "board": initial_board(),
        "rights": {
            "wK": True, "wQ": True,
            "bK": True, "bQ": True
        },
        "turn": "w",
        "players": {"w": None, "b": None}
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

    if room["white"]["sid"] == request.sid:
        emit("private_lobby_error", {
            "message": "You created this lobby."
        })
        return

    room["black"] = {"sid": request.sid, "player_id": None}
    sid_room[request.sid] = code
    join_room(code)
    emit_match(code)


# ---------------- ONLINE MOVES ----------------

@socketio.on("online_move")
def online_move(data=None):
    data = data or {}
    code = data.get("room")
    room = rooms.get(code)

    if not room or room["over"]:
        return

    sid = request.sid

    if sid not in (room["white"]["sid"], room["black"]["sid"]):
        return

    side = "w" if sid == room["white"]["sid"] else "b"

    if side != room["turn"]:
        return

    now = time.monotonic()

    if not room["started"] and side != "w":
        return

    if not room["unlimited"] and room["started"]:
        elapsed = max(0, now - room["last"])
        room["clocks"][side] -= elapsed

        if room["clocks"][side] <= 0:
            room["clocks"][side] = 0
            finish_by_time(code, side)
            return

    move = data.get("move")

    if not isinstance(move, dict):
        return

    try:
        fr = int(move["fr"])
        fc = int(move["fc"])
        tr = int(move["tr"])
        tc = int(move["tc"])
    except (KeyError, TypeError, ValueError):
        return

    if not all(0 <= n < 8 for n in (fr, fc, tr, tc)):
        return

    board = room["board"]
    moving = board[fr][fc]

    if moving == "." or color_of(moving) != side:
        return

    legal = make_moves(board, fr, fc, room["rights"])

    chosen = next(
        (m for m in legal if m["tr"] == tr and m["tc"] == tc),
        None
    )

    if not chosen:
        emit("move_rejected", {"message": "That move is not legal."})
        return

    promotion = move.get("promotion")

    if moving == "P" and tr == 0:
        if promotion not in ("Q", "R", "B", "N"):
            promotion = "Q"
    elif moving == "p" and tr == 7:
        if promotion not in ("q", "r", "b", "n"):
            promotion = "q"
    else:
        promotion = None

    chosen["promotion"] = promotion

    captured = apply_move(room, chosen)

    room["started"] = True
    room["clocks"][side] += room["increment"]
    room["turn"] = "b" if side == "w" else "w"
    room["active"] = room["turn"]
    room["last"] = now

    socketio.emit(
        "opponent_move",
        chosen,
        to=code,
        include_self=False
    )

    socketio.emit("clock_update", {
        "clocks": room["clocks"],
        "active": room["active"],
        "increment": room["increment"]
    }, to=code)

    if captured in ("K", "k"):
        room["over"] = True

        if room["rated"]:
            award_rating(code, side)

        socketio.emit("king_captured", {"winner": side}, to=code)


# ---------------- RESIGNATION ----------------

@socketio.on("online_resign")
def online_resign(data=None):
    code = (data or {}).get("room")
    room = rooms.get(code)

    if not room or room["over"]:
        return

    sid = request.sid

    if sid not in (room["white"]["sid"], room["black"]["sid"]):
        return

    side = "w" if sid == room["white"]["sid"] else "b"
    winner = "b" if side == "w" else "w"

    room["over"] = True

    if room["rated"]:
        award_rating(code, winner)

    socketio.emit("opponent_resigned", to=code, include_self=False)


# ---------------- DISCONNECT ----------------

@socketio.on("disconnect")
def disconnect():
    sid = request.sid

    waiting_players[:] = [
        item for item in waiting_players if item["sid"] != sid
    ]

    code = sid_room.pop(sid, None)

    if code and code in rooms:
        room = rooms[code]

        socketio.emit(
            "opponent_disconnected",
            to=code,
            include_self=False
        )

        rooms.pop(code, None)

        for player in (room["white"], room["black"]):
            if player:
                sid_room.pop(player["sid"], None)


# ---------------- CLOCK WORKER ----------------

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

            side = room["active"]
            elapsed = max(0, now - room["last"])
            remaining = room["clocks"][side] - elapsed

            if remaining <= 0:
                finish_by_time(code, side)
            else:
                socketio.emit("clock_tick", {
                    "active": side,
                    "remaining": remaining
                }, to=code)


socketio.start_background_task(clock_worker)


if __name__ == "__main__":
    print("Mouseslip Chess server starting...")

    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False,
        allow_unsafe_werkzeug=True
    )
