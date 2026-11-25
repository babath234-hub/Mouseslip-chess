import os
import time
import secrets
import string
import re
import logging
import threading

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

lock = threading.RLock()
rooms = {}
sid_room = {}
waiting_players = []

PIECES = {
    "w": "PNBRQK",
    "b": "pnbrqk"
}


# -------------------- ACCOUNTS --------------------

def get_player_by_username(username):
    result = (
        supabase.table("players")
        .select("*")
        .eq("username", username)
        .execute()
    )
    return result.data[0] if result.data else None


def get_player_by_id(player_id):
    if not player_id:
        return None

    result = (
        supabase.table("players")
        .select("id, username, rating")
        .eq("id", player_id)
        .execute()
    )
    return result.data[0] if result.data else None


def current_player():
    return get_player_by_id(session.get("player_id"))


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
        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", ""))

        if not valid_username(username):
            return jsonify({
                "error": "Username must be 3–16 characters and use only letters, numbers or underscores."
            }), 400

        if len(password) < 8 or len(password) > 128:
            return jsonify({
                "error": "Password must be between 8 and 128 characters."
            }), 400

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
        username = str(data.get("username", "")).strip().lower()

        if not valid_username(username):
            return jsonify({
                "error": "Username must be 3–16 characters and use only letters, numbers or underscores."
            }), 400

        existing = get_player_by_username(username)

        if existing and existing["id"] != player["id"]:
            return jsonify({"error": "That username is already taken."}), 409

        result = (
            supabase.table("players")
            .update({"username": username})
            .eq("id", player["id"])
            .execute()
        )

        if not result.data:
            raise Exception("Username was not updated.")

        return jsonify({
            "success": True,
            "player": account_response(result.data[0])
        })

    except Exception:
        return database_error("changing username")


# -------------------- CHESS SERVER --------------------

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
    if piece == "." or not piece:
        return None
    return "w" if piece.isupper() else "b"


def inside(r, c):
    return 0 <= r < 8 and 0 <= c < 8


def room_code():
    alphabet = string.ascii_uppercase + "23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


def new_room(white, black, seconds, increment, rated,
             white_id=None, black_id=None):
    code = room_code()

    rooms[code] = {
        "white": white,
        "black": black,
        "white_id": white_id,
        "black_id": black_id,
        "rated": bool(rated),
        "board": initial_board(),
        "rights": {"wK": True, "wQ": True, "bK": True, "bQ": True},
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


def send_match(code):
    room = rooms[code]

    for side, sid in (("w", room["white"]), ("b", room["black"])):
        emit("match_found", {
            "room": code,
            "color": side,
            "seconds": room["clocks"][side],
            "increment": room["increment"],
            "rated": room["rated"]
        }, to=sid)


def get_side(room, sid):
    if sid == room["white"]:
        return "w"
    if sid == room["black"]:
        return "b"
    return None


def player_id_for(room, side):
    return room["white_id"] if side == "w" else room["black_id"]


def finish_room(code, winner, reason):
    room = rooms.get(code)

    if not room or room["over"]:
        return

    room["over"] = True
    loser = "b" if winner == "w" else "w"
    rating_updated = False

    if room["rated"]:
        white_id = room["white_id"]
        black_id = room["black_id"]
        winner_id = player_id_for(room, winner)

        if white_id and black_id and winner_id:
            try:
                supabase.rpc("award_match_rating", {
                    "p_match_id": code,
                    "p_white": white_id,
                    "p_black": black_id,
                    "p_winner": winner_id
                }).execute()
                rating_updated = True
            except Exception:
                app.logger.exception("Could not award rating for %s", code)

    for side, sid in (("w", room["white"]), ("b", room["black"])):
        player = get_player_by_id(player_id_for(room, side))

        socketio.emit("match_result", {
            "winner": winner,
            "loser": loser,
            "reason": reason,
            "rated": room["rated"],
            "rating_updated": rating_updated,
            "rating": player["rating"] if player else None
        }, to=sid)

    socketio.emit("clock_stop", {}, to=code)


def pseudo_moves(board, r, c, rights):
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

        moves.append((rr, cc))
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
            if inside(rr, cc) and color_of(board[rr][cc]) not in (None, color):
                moves.append((rr, cc))

    elif kind == "n":
        for dr, dc in (
            (-2, -1), (-2, 1), (-1, -2), (-1, 2),
            (1, -2), (1, 2), (2, -1), (2, 1)
        ):
            add(r + dr, c + dc)

    else:
        if kind == "k":
            directions = [
                (-1, -1), (-1, 0), (-1, 1),
                (0, -1), (0, 1),
                (1, -1), (1, 0), (1, 1)
            ]
        elif kind == "b":
            directions = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        elif kind == "r":
            directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        else:
            directions = [
                (-1, -1), (-1, 0), (-1, 1), (0, -1),
                (0, 1), (1, -1), (1, 0), (1, 1)
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
                    rights[color + "K"] and
                    board[home][7] == rook and
                    board[home][5] == "." and
                    board[home][6] == "."
                ):
                    moves.append((home, 6))

                if (
                    rights[color + "Q"] and
                    board[home][0] == rook and
                    board[home][1] == "." and
                    board[home][2] == "." and
                    board[home][3] == "."
                ):
                    moves.append((home, 2))

    return moves


def update_rights(room, fr, fc, tr, tc, moving, captured):
    rights = room["rights"]

    if moving == "K":
        rights["wK"] = rights["wQ"] = False
    if moving == "k":
        rights["bK"] = rights["bQ"] = False

    if moving == "R" and (fr, fc) == (7, 7):
        rights["wK"] = False
    if moving == "R" and (fr, fc) == (7, 0):
        rights["wQ"] = False
    if moving == "r" and (fr, fc) == (0, 7):
        rights["bK"] = False
    if moving == "r" and (fr, fc) == (0, 0):
        rights["bQ"] = False

    if captured == "R" and (tr, tc) == (7, 7):
        rights["wK"] = False
    if captured == "R" and (tr, tc) == (7, 0):
        rights["wQ"] = False
    if captured == "r" and (tr, tc) == (0, 7):
        rights["bK"] = False
    if captured == "r" and (tr, tc) == (0, 0):
        rights["bQ"] = False


def apply_server_move(room, move):
    try:
        fr, fc = int(move["fr"]), int(move["fc"])
        tr, tc = int(move["tr"]), int(move["tc"])
    except (KeyError, TypeError, ValueError):
        return False, False

    if not all(inside(r, c) for r, c in ((fr, fc), (tr, tc))):
        return False, False

    board = room["board"]
    moving = board[fr][fc]
    captured = board[tr][tc]
    side = room["active"]

    if moving == "." or color_of(moving) != side:
        return False, False

    if (tr, tc) not in pseudo_moves(board, fr, fc, room["rights"]):
        return False, False

    promotion = move.get("promotion")
    if moving == "P" and tr == 0:
        if promotion not in ("Q", "R", "B", "N"):
            return False, False
    elif moving == "p" and tr == 7:
        if promotion not in ("q", "r", "b", "n"):
            return False, False
    else:
        promotion = None

    update_rights(room, fr, fc, tr, tc, moving, captured)

    board[fr][fc] = "."
    board[tr][tc] = promotion or moving

    if moving.lower() == "k" and fr == tr and abs(tc - fc) == 2:
        home = fr
        if tc == 6:
            board[home][5] = board[home][7]
            board[home][7] = "."
        else:
            board[home][3] = board[home][0]
            board[home][0] = "."

    king_captured = captured in ("K", "k")
    return True, king_captured


# -------------------- MATCHMAKING --------------------

@socketio.on("find_match")
def find_match(data=None):
    data = data or {}
    sid = request.sid
    player = current_player()

    if not player:
        emit("match_error", {
            "message": "Log in before searching. Random matches are always rated."
        })
        return

    seconds = max(0, min(7200, int(data.get("seconds", 300))))
    increment = max(0, min(60, int(data.get("increment", 0))))

    with lock:
        waiting_players[:] = [
            p for p in waiting_players if p["sid"] != sid
        ]

        now = time.monotonic()
        opponent = None
        best_difference = None

        for candidate in waiting_players:
            waited = now - candidate["joined"]
            allowed_difference = 200 + int(waited * 25)

            difference = abs(candidate["rating"] - player["rating"])

            if difference <= allowed_difference:
                if best_difference is None or difference < best_difference:
                    opponent = candidate
                    best_difference = difference

        if opponent:
            waiting_players.remove(opponent)

            code = new_room(
                opponent["sid"], sid,
                opponent["seconds"], opponent["increment"],
                True, opponent["player_id"], player["id"]
            )
            send_match(code)

        else:
            waiting_players.append({
                "sid": sid,
                "player_id": player["id"],
                "rating": player["rating"],
                "seconds": seconds,
                "increment": increment,
                "joined": now
            })

            emit("searching", {
                "message": "Looking for an opponent. The rating range expands while you wait..."
            })


@socketio.on("cancel_match")
def cancel_match():
    sid = request.sid

    with lock:
        waiting_players[:] = [
            p for p in waiting_players if p["sid"] != sid
        ]

    emit("match_cancelled")


# -------------------- PRIVATE LOBBIES --------------------

@socketio.on("create_private_lobby")
def create_private_lobby(data=None):
    data = data or {}
    sid = request.sid
    player = current_player()

    seconds = max(0, min(7200, int(data.get("seconds", 300))))
    increment = max(0, min(60, int(data.get("increment", 0))))
    rated = bool(data.get("rated", False))

    if rated and not player:
        emit("private_lobby_error", {
            "message": "Log in to create a rated private game."
        })
        return

    code = room_code()

    rooms[code] = {
        "white": sid,
        "black": None,
        "white_id": player["id"] if player else None,
        "black_id": None,
        "rated": rated,
        "board": initial_board(),
        "rights": {"wK": True, "wQ": True, "bK": True, "bQ": True},
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

    emit("private_lobby_created", {
        "code": code,
        "room": code,
        "rated": rated
    })


@socketio.on("join_private_lobby")
def join_private_lobby(data=None):
    data = data or {}
    code = str(data.get("code", "")).strip().upper()
    room = rooms.get(code)

    if not room or room["black"] is not None or room["over"]:
        emit("private_lobby_error", {
            "message": "That code is invalid or the lobby is full."
        })
        return

    if room["white"] == request.sid:
        emit("private_lobby_error", {
            "message": "You created this lobby. Share the code with your opponent."
        })
        return

    player = current_player()

    if room["rated"] and not player:
        emit("private_lobby_error", {
            "message": "This is a rated game. Log in before joining."
        })
        return

    room["black"] = request.sid
    room["black_id"] = player["id"] if player else None

    sid_room[request.sid] = code
    join_room(code)
    send_match(code)


# -------------------- ONLINE MOVES --------------------

@socketio.on("online_move")
def online_move(data=None):
    data = data or {}
    code = data.get("room")
    room = rooms.get(code)

    if not room or room["over"]:
        return

    side = get_side(room, request.sid)
    if not side or side != room["active"]:
        return

    now = time.monotonic()

    if not room["unlimited"] and room["started"]:
        elapsed = max(0, now - room["last"])
        room["clocks"][side] -= elapsed

        if room["clocks"][side] <= 0:
            room["clocks"][side] = 0
            finish_room(code, "b" if side == "w" else "w", "time")
            return

    move = data.get("move")
    if not isinstance(move, dict):
        return

    valid, king_captured = apply_server_move(room, move)

    if not valid:
        emit("invalid_move", {
            "message": "That move was not accepted. The board will be refreshed."
        })
        return

    room["started"] = True

    if not room["unlimited"]:
        room["clocks"][side] += room["increment"]

    room["active"] = "b" if side == "w" else "w"
    room["last"] = now

    socketio.emit("opponent_move", move, to=code, include_self=False)

    socketio.emit("clock_update", {
        "clocks": room["clocks"],
        "active": room["active"],
        "increment": room["increment"]
    }, to=code)

    if king_captured:
        finish_room(code, side, "king")


# -------------------- RESIGNATION --------------------

@socketio.on("online_resign")
def online_resign(data=None):
    code = (data or {}).get("room")
    room = rooms.get(code)

    if not room or room["over"]:
        return

    side = get_side(room, request.sid)
    if not side:
        return

    finish_room(code, "b" if side == "w" else "w", "resignation")


# -------------------- DISCONNECT --------------------

@socketio.on("disconnect")
def disconnect():
    sid = request.sid

    with lock:
        waiting_players[:] = [
            p for p in waiting_players if p["sid"] != sid
        ]

    code = sid_room.pop(sid, None)

    if not code:
        return

    room = rooms.get(code)
    if not room or room["over"]:
        return

    side = get_side(room, sid)

    if side and room["started"] and room["black"] is not None:
        finish_room(code, "b" if side == "w" else "w", "disconnect")
    else:
        socketio.emit("opponent_disconnected", {}, to=code, include_self=False)
        rooms.pop(code, None)


# -------------------- CLOCK --------------------

def clock_worker():
    while True:
        socketio.sleep(0.25)
        now = time.monotonic()

        for code, room in list(rooms.items()):
            if (
                room["over"] or
                not room["started"] or
                room["black"] is None or
                room["unlimited"]
            ):
                continue

            elapsed = max(0, now - room["last"])
            remaining = room["clocks"][room["active"]] - elapsed

            if remaining <= 0:
                room["clocks"][room["active"]] = 0
                loser = room["active"]
                finish_room(code, "b" if loser == "w" else "w", "time")
            else:
                socketio.emit("clock_tick", {
                    "active": room["active"],
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
