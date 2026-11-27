import os
import re
import time
import uuid
import json
import random
import secrets
import string
import logging
import threading

from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client

# ============================================================
# MOUSESLIP CHESS — COMPLETE BACKEND
# PART 1 OF 3: SETUP, ACCOUNTS, PROFILES, COMMUNITY
# ============================================================

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

lock = threading.RLock()

rooms = {}
sid_room = {}
waiting_players = []
connected_sids = set()
tournament_waiting = {}
player_sids = {}
spectator_rooms = {}
rematch_requests = {}
game_invites = {}

tournament_lock = threading.RLock()
_firebase_app = None
RIYADH = ZoneInfo("Asia/Riyadh")
SITE_URL = "https://mouseslip-chess-1.onrender.com/"


# ============================================================
# FIREBASE WEB PUSH
# ============================================================

def get_firebase_messaging():
    global _firebase_app

    import firebase_admin
    from firebase_admin import credentials, messaging

    if _firebase_app is None:
        raw_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
        if not raw_key:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not configured.")

        certificate = credentials.Certificate(json.loads(raw_key))

        try:
            _firebase_app = firebase_admin.get_app()
        except ValueError:
            _firebase_app = firebase_admin.initialize_app(certificate)

    return messaging


def send_tournament_push(tournament_id, title, body):
    entries = tournament_entries(tournament_id)
    player_ids = list({
        entry["player_id"] for entry in entries
    })

    if not player_ids:
        return 0

    subscriptions = (
        supabase.table("push_subscriptions")
        .select("fcm_token")
        .in_("player_id", player_ids)
        .execute()
    )
    tokens = list(dict.fromkeys(
        item["fcm_token"] for item in (subscriptions.data or [])
        if item.get("fcm_token")
    ))

    if not tokens:
        return 0

    messaging = get_firebase_messaging()
    message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={"url": SITE_URL},
        webpush=messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                title=title,
                body=body,
                icon="/favicon.ico"
            ),
            fcm_options=messaging.WebpushFCMOptions(link=SITE_URL)
        )
    )

    response = messaging.send_each_for_multicast(message)
    for index, result in enumerate(response.responses):
        if result.success:
            continue
        error_code = getattr(result.exception, "code", "")
        if error_code in (
            "messaging/registration-token-not-registered",
            "messaging/invalid-registration-token"
        ):
            supabase.table("push_subscriptions").delete().eq(
                "fcm_token", tokens[index]
            ).execute()

    return response.success_count


def parse_tournament_datetime(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def process_scheduled_tournaments():
    now = datetime.now(timezone.utc)
    result = (
        supabase.table("monthly_tournaments")
        .select("*")
        .eq("status", "registration")
        .not_.is_("scheduled_at", "null")
        .execute()
    )

    for tournament in result.data or []:
        scheduled = parse_tournament_datetime(tournament.get("scheduled_at"))
        if not scheduled:
            continue

        scheduled_local = scheduled.astimezone(RIYADH)
        date_label = scheduled_local.strftime("%A, %d %B %Y at 5:00 PM")

        if not tournament.get("announcement_sent", False):
            send_tournament_push(
                tournament["id"],
                "Mouseslip Chess tournament scheduled!",
                f"The 32-player tournament is scheduled for {date_label} Saudi time."
            )
            supabase.table("monthly_tournaments").update({
                "announcement_sent": True
            }).eq("id", tournament["id"]).execute()
            tournament["announcement_sent"] = True

        if now < scheduled:
            hours_left = (scheduled - now).total_seconds() / 3600

            if hours_left <= 24 and not tournament.get("reminder_24h_sent", False):
                send_tournament_push(
                    tournament["id"],
                    "Tournament reminder: 24 hours to go!",
                    f"The Mouseslip Chess tournament begins {date_label} Saudi time."
                )
                supabase.table("monthly_tournaments").update({
                    "reminder_24h_sent": True
                }).eq("id", tournament["id"]).execute()
                tournament["reminder_24h_sent"] = True

            if hours_left <= 1 and not tournament.get("reminder_1h_sent", False):
                send_tournament_push(
                    tournament["id"],
                    "Tournament starts in 1 hour!",
                    f"Get ready. The tournament begins at 5:00 PM Saudi time."
                )
                supabase.table("monthly_tournaments").update({
                    "reminder_1h_sent": True
                }).eq("id", tournament["id"]).execute()

        if now >= scheduled:
            start_tournament(tournament)


def tournament_scheduler():
    while True:
        try:
            process_scheduled_tournaments()
        except Exception:
            app.logger.exception("Tournament scheduler check failed.")
        socketio.sleep(30)


# ============================================================
# GENERAL HELPERS
# ============================================================

def database_error(action):
    app.logger.exception("Database error: %s", action)
    return jsonify({
        "error": "Database error. Check the Render logs."
    }), 500


def current_player():
    player_id = session.get("player_id")

    if not player_id:
        return None

    result = (
        supabase.table("players")
        .select("id, username, rating, is_admin")
        .eq("id", player_id)
        .limit(1)
        .execute()
    )

    return result.data[0] if result.data else None


def require_player():
    player = current_player()

    if not player:
        return None, (
            jsonify({"error": "You must log in first."}),
            401
        )

    return player, None


def require_admin():
    player = current_player()

    if not player or not player.get("is_admin"):
        return None, (
            jsonify({"error": "Admin access required."}),
            403
        )

    return player, None


def valid_username(username):
    return bool(
        re.fullmatch(r"[A-Za-z0-9_]{3,16}", username)
    )


def account_response(player):
    return {
        "id": player["id"],
        "username": player["username"],
        "rating": player["rating"],
        "is_admin": bool(player.get("is_admin", False))
    }


def get_player_by_username(username):
    result = (
        supabase.table("players")
        .select("*")
        .eq("username", username)
        .limit(1)
        .execute()
    )

    return result.data[0] if result.data else None


def get_player_by_id(player_id):
    if not player_id:
        return None

    result = (
        supabase.table("players")
        .select("id, username, rating, is_admin")
        .eq("id", player_id)
        .limit(1)
        .execute()
    )

    return result.data[0] if result.data else None


def get_player_stats(player_id):
    result = (
        supabase.table("player_stats")
        .select("*")
        .eq("player_id", player_id)
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]

    return {
        "player_id": player_id,
        "games_played": 0,
        "wins": 0,
        "losses": 0,
        "current_win_streak": 0,
        "longest_win_streak": 0
    }


def get_player_move_stats(player_id):
    result = (
        supabase.table("player_move_stats")
        .select("*")
        .eq("player_id", player_id)
        .limit(1)
        .execute()
    )

    stats = result.data[0] if result.data else {}

    normal_moves = int(stats.get("normal_moves", 0) or 0)
    mouseslips = int(stats.get("mouseslips", 0) or 0)

    return {
        "normal_moves": normal_moves,
        "normal_captures": int(
            stats.get("normal_captures", 0) or 0
        ),
        "mouseslips": mouseslips,
        "slipped_captures": int(
            stats.get("slipped_captures", 0) or 0
        ),
        "wins_with_mouseslip": int(
            stats.get("wins_with_mouseslip", 0) or 0
        ),
        "losses_in_games_with_mouseslip": int(
            stats.get("losses_in_games_with_mouseslip", 0) or 0
        ),
        "max_mouseslips_in_game": int(
            stats.get("max_mouseslips_in_game", 0) or 0
        ),
        "games_with_mouseslip": int(
            stats.get("games_with_mouseslip", 0) or 0
        ),
        "total_moves": normal_moves + mouseslips
    }


def get_combined_player_stats(player_id):
    stats = get_player_stats(player_id)
    stats.update(get_player_move_stats(player_id))
    return stats


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# MAIN WEBSITE
# ============================================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/firebase-messaging-sw.js")
def firebase_messaging_service_worker():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "firebase-messaging-sw.js",
        mimetype="application/javascript",
        max_age=0
    )


@app.route("/sitemap.xml")
def sitemap():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url>
<loc>https://mouseslip-chess-1.onrender.com/</loc>
<changefreq>weekly</changefreq>
<priority>1.0</priority>
</url>
</urlset>"""

    return app.response_class(
        xml,
        mimetype="application/xml"
    )


# ============================================================
# ACCOUNTS
# ============================================================

@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.get_json(silent=True) or {}

        username = str(
            data.get("username", "")
        ).strip().lower()

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

        if get_player_by_username(username):
            return jsonify({
                "error": "That username is already taken."
            }), 409

        result = supabase.table("players").insert({
            "username": username,
            "password_hash": generate_password_hash(password),
            "rating": 1000,
            "is_admin": False
        }).execute()

        if not result.data:
            raise Exception("Account was not created.")

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

        username = str(
            data.get("username", "")
        ).strip().lower()

        password = str(data.get("password", ""))

        player = get_player_by_username(username)

        if not player or not check_password_hash(
            player["password_hash"],
            password
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
        player, error = require_player()

        if error:
            return error

        data = request.get_json(silent=True) or {}

        username = str(
            data.get("username", "")
        ).strip().lower()

        if not valid_username(username):
            return jsonify({
                "error": (
                    "Username must be 3–16 characters "
                    "and use only letters, numbers or underscores."
                )
            }), 400

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
            raise Exception("Username was not updated.")

        return jsonify({
            "success": True,
            "player": account_response(result.data[0])
        })

    except Exception:
        return database_error("changing username")


# ============================================================
# PLAYER PROFILES
# ============================================================

@app.route("/api/profile/<username>")
def player_profile(username):
    try:
        username = str(username).strip().lower()

        result = (
            supabase.table("players")
            .select("id, username, rating, created_at")
            .eq("username", username)
            .limit(1)
            .execute()
        )

        if not result.data:
            return jsonify({
                "error": "Player not found."
            }), 404

        player = result.data[0]
        stats = get_combined_player_stats(player["id"])

        all_players = (
            supabase.table("players")
            .select("id, rating")
            .order("rating", desc=True)
            .execute()
        )

        rank = 1

        for item in all_players.data or []:
            if item["id"] == player["id"]:
                break
            rank += 1

        achievements_result = (
            supabase.table("player_achievements")
            .select("*")
            .eq("player_id", player["id"])
            .order("unlocked_at", desc=True)
            .execute()
        )
        achievement_rows = achievements_result.data or []
        achievement_ids = [
            item.get("achievement_id") for item in achievement_rows
            if item.get("achievement_id")
        ]
        if achievement_ids:
            catalog_result = (
                supabase.table("achievements")
                .select("*")
                .in_("id", achievement_ids)
                .execute()
            )
            catalog = {
                str(item["id"]): item
                for item in (catalog_result.data or [])
            }
            for item in achievement_rows:
                detail = catalog.get(str(item.get("achievement_id")), {})
                item["name"] = detail.get("name") or item.get("name") or "Achievement"
                item["description"] = detail.get("description") or item.get("description") or ""
                item["icon"] = detail.get("icon") or item.get("icon") or "🏅"
        history_result = (
            supabase.table("match_history")
            .select("*")
            .or_(
                f"white_player.eq.{player['id']},"
                f"black_player.eq.{player['id']}"
            )
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        profile = {
            "id": player["id"],
            "username": player["username"],
            "rating": player["rating"],
            "rank": rank,
            "created_at": player.get("created_at"),
            "stats": stats
        }
        return jsonify({
            "profile": profile,
            "player": profile,
            "stats": stats,
            "achievements": achievement_rows,
            "history": history_result.data or []
        })

    except Exception:
        return database_error("loading player profile")


@app.route("/api/player/stats")
def my_stats():
    try:
        player, error = require_player()

        if error:
            return error

        return jsonify({
            "username": player["username"],
            "rating": player["rating"],
            "stats": get_combined_player_stats(player["id"])
        })

    except Exception:
        return database_error("loading player statistics")


# ============================================================
# GLOBAL LEADERBOARD
# ============================================================

@app.route("/api/leaderboard")
def leaderboard():
    try:
        search = str(
            request.args.get("q", "")
        ).strip()[:16]

        query = (
            supabase.table("players")
            .select("id, username, rating")
        )

        if search:
            query = query.ilike(
                "username",
                f"%{search}%"
            )

        result = (
            query
            .order("rating", desc=True)
            .order("username")
            .limit(100)
            .execute()
        )

        players = result.data or []

        for index, player in enumerate(players, start=1):
            player["rank"] = index

        return jsonify({"players": players})

    except Exception:
        return database_error("loading leaderboard")


# ============================================================
# MATCH HISTORY
# ============================================================

@app.route("/api/player/history")
def match_history():
    try:
        player, error = require_player()

        if error:
            return error

        result = (
            supabase.table("match_history")
            .select("*")
            .or_(
                f"white_player.eq.{player['id']},"
                f"black_player.eq.{player['id']}"
            )
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return jsonify({
            "games": result.data or []
        })

    except Exception:
        return database_error("loading match history")


@app.route("/api/match/<match_id>")
def get_match_details(match_id):
    try:
        result = (
            supabase.table("match_history")
            .select("*")
            .eq("match_id", match_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return jsonify({
                "error": "Game not found."
            }), 404

        return jsonify({
            "game": result.data[0]
        })

    except Exception:
        return database_error("loading game details")


# ============================================================
# PUBLIC COMMUNITY COMMENTS
# ============================================================

@app.route("/api/comments", methods=["GET", "POST"])
def comments():
    try:
        if request.method == "GET":
            result = (
                supabase.table("comments")
                .select("id, username, message, created_at")
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )

            return jsonify({
                "comments": result.data or []
            })

        player, error = require_player()

        if error:
            return error

        data = request.get_json(silent=True) or {}

        message = str(
            data.get("message", "")
        ).strip()

        if not message or len(message) > 500:
            return jsonify({
                "error": "Comments must be between 1 and 500 characters."
            }), 400

        result = supabase.table("comments").insert({
            "player_id": player["id"],
            "username": player["username"],
            "message": message
        }).execute()

        return jsonify({
            "success": True,
            "comment": result.data[0] if result.data else None
        })

    except Exception:
        return database_error("comments")


# ============================================================
# PRIVATE FEEDBACK AND ADMIN INBOX
# ============================================================

@app.route("/api/feedback", methods=["POST"])
def send_feedback():
    try:
        player, error = require_player()

        if error:
            return error

        data = request.get_json(silent=True) or {}

        message = str(
            data.get("message", "")
        ).strip()

        if not message or len(message) > 2000:
            return jsonify({
                "error": "Feedback must be between 1 and 2000 characters."
            }), 400

        supabase.table("feedback").insert({
            "player_id": player["id"],
            "username": player["username"],
            "message": message
        }).execute()

        return jsonify({"success": True})

    except Exception:
        return database_error("sending feedback")


@app.route("/api/admin/feedback")
def admin_feedback():
    try:
        player, error = require_admin()

        if error:
            return error

        result = (
            supabase.table("feedback")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return jsonify({
            "feedback": result.data or []
        })

    except Exception:
        return database_error("loading feedback inbox")


@app.route(
    "/api/admin/feedback/<int:feedback_id>/reply",
    methods=["POST"]
)
def reply_feedback(feedback_id):
    try:
        player, error = require_admin()

        if error:
            return error

        data = request.get_json(silent=True) or {}

        reply = str(
            data.get("reply", "")
        ).strip()

        if not reply or len(reply) > 2000:
            return jsonify({
                "error": "Replies must be between 1 and 2000 characters."
            }), 400

        result = (
            supabase.table("feedback")
            .update({
                "admin_reply": reply,
                "replied_at": utc_now()
            })
            .eq("id", feedback_id)
            .execute()
        )

        return jsonify({
            "success": True,
            "feedback": result.data or []
        })

    except Exception:
        return database_error("replying to feedback")


# ============================================================
# FRIEND REQUESTS
# ============================================================

@app.route("/api/friends", methods=["GET", "POST"])
def friends():
    try:
        player, error = require_player()

        if error:
            return error

        if request.method == "GET":
            result = (
                supabase.table("friend_requests")
                .select("*")
                .or_(
                    f"sender_id.eq.{player['id']},"
                    f"receiver_id.eq.{player['id']}"
                )
                .order("created_at", desc=True)
                .limit(200)
                .execute()
            )

            records = result.data or []
            friends_list = []
            incoming = []
            outgoing = []
            for item in records:
                status = item.get("status")
                if status == "accepted":
                    other_id = (
                        item["receiver_id"]
                        if item.get("sender_id") == player["id"]
                        else item["sender_id"]
                    )
                    other = get_player_by_id(other_id)
                    if other:
                        friends_list.append({
                            "id": other["id"],
                            "username": other["username"]
                        })
                elif status == "pending":
                    if item.get("receiver_id") == player["id"]:
                        sender = get_player_by_id(item.get("sender_id"))
                        if sender:
                            incoming.append({
                                "id": item["id"],
                                "username": sender["username"]
                            })
                    elif item.get("sender_id") == player["id"]:
                        receiver = get_player_by_id(item.get("receiver_id"))
                        if receiver:
                            outgoing.append({
                                "id": item["id"],
                                "username": receiver["username"]
                            })
            return jsonify({
                "friends": friends_list,
                "requests": incoming,
                "outgoing_requests": outgoing
            })

        data = request.get_json(silent=True) or {}

        username = str(
            data.get("username", "")
        ).strip().lower()

        target = get_player_by_username(username)

        if not target:
            return jsonify({
                "error": "Player not found."
            }), 404

        if target["id"] == player["id"]:
            return jsonify({
                "error": "You cannot add yourself."
            }), 400

        existing = (
            supabase.table("friend_requests")
            .select("id")
            .or_(
                f"and(sender_id.eq.{player['id']},"
                f"receiver_id.eq.{target['id']}),"
                f"and(sender_id.eq.{target['id']},"
                f"receiver_id.eq.{player['id']})"
            )
            .limit(1)
            .execute()
        )

        if existing.data:
            return jsonify({
                "error": "A request already exists."
            }), 409

        result = supabase.table("friend_requests").insert({
            "sender_id": player["id"],
            "receiver_id": target["id"],
            "status": "pending"
        }).execute()

        return jsonify({
            "success": True,
            "request": result.data[0] if result.data else None
        })

    except Exception:
        return database_error("friend requests")


@app.route(
    "/api/friends/<request_id>/respond",
    methods=["POST"]
)
def respond_friend_request(request_id):
    try:
        player, error = require_player()

        if error:
            return error

        data = request.get_json(silent=True) or {}

        action = str(
            data.get("action", "")
        ).lower()

        if action not in ("accept", "decline"):
            return jsonify({
                "error": "Choose accept or decline."
            }), 400

        status = (
            "accepted" if action == "accept"
            else "declined"
        )

        result = (
            supabase.table("friend_requests")
            .update({"status": status})
            .eq("id", request_id)
            .eq("receiver_id", player["id"])
            .eq("status", "pending")
            .execute()
        )

        if not result.data:
            return jsonify({
                "error": "Request not found or already handled."
            }), 404

        return jsonify({"success": True})

    except Exception:
        return database_error("responding to friend request")


# ============================================================
# PLAYER CUSTOMISATION
# ============================================================

@app.route(
    "/api/player/customization",
    methods=["GET", "POST"]
)
def customization():
    try:
        player, error = require_player()

        if error:
            return error

        if request.method == "GET":
            result = (
                supabase.table("player_customizations")
                .select("*")
                .eq("player_id", player["id"])
                .limit(1)
                .execute()
            )

            settings = result.data[0] if result.data else {
                "board_theme": "classic",
                "piece_theme": "classic"
            }

            return jsonify({
                "customization": settings
            })

        data = request.get_json(silent=True) or {}

        board = str(
            data.get("board_theme", "classic")
        )

        pieces = str(
            data.get("piece_theme", "classic")
        )

        allowed_boards = {
            "classic", "blue", "green", "purple", "dark"
        }

        allowed_pieces = {
            "classic", "modern", "simple"
        }

        if board not in allowed_boards:
            return jsonify({
                "error": "Invalid board theme."
            }), 400

        if pieces not in allowed_pieces:
            return jsonify({
                "error": "Invalid piece theme."
            }), 400

        values = {
            "player_id": player["id"],
            "board_theme": board,
            "piece_theme": pieces
        }

        existing = (
            supabase.table("player_customizations")
            .select("player_id")
            .eq("player_id", player["id"])
            .limit(1)
            .execute()
        )

        if existing.data:
            result = (
                supabase.table("player_customizations")
                .update(values)
                .eq("player_id", player["id"])
                .execute()
            )
        else:
            result = (
                supabase.table("player_customizations")
                .insert(values)
                .execute()
            )

        return jsonify({
            "success": True,
            "customization": (
                result.data[0] if result.data else values
            )
        })

    except Exception:
        return database_error("saving customisation")


# ============================================================
# ACHIEVEMENTS
# ============================================================

@app.route("/api/player/achievements")
def player_achievements():
    try:
        player, error = require_player()

        if error:
            return error

        result = (
            supabase.table("player_achievements")
            .select("*")
            .eq("player_id", player["id"])
            .order("unlocked_at", desc=True)
            .execute()
        )

        return jsonify({
            "achievements": result.data or []
        })

    except Exception:
        return database_error("loading achievements")


# ============================================================
# SEASONAL LEADERBOARD
# ============================================================

@app.route("/api/seasonal-leaderboard")
def seasonal_leaderboard():
    try:
        now = datetime.now(timezone.utc)

        month_start = now.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        ).isoformat()

        games = (
            supabase.table("match_history")
            .select("winner_id")
            .gte("created_at", month_start)
            .execute()
        )

        wins = {}

        for game in games.data or []:
            winner = game.get("winner_id")

            if winner:
                wins[winner] = wins.get(winner, 0) + 1

        players_result = (
            supabase.table("players")
            .select("id, username, rating")
            .execute()
        )

        players = []

        for player in players_result.data or []:
            count = wins.get(player["id"], 0)

            if count:
                players.append({
                    "id": player["id"],
                    "username": player["username"],
                    "rating": player["rating"],
                    "monthly_wins": count
                })

        players.sort(
            key=lambda p: (
                -p["monthly_wins"],
                -p["rating"],
                p["username"]
            )
        )

        for index, player in enumerate(players, start=1):
            player["rank"] = index

        return jsonify({
            "players": players[:100]
        })

    except Exception:
        return database_error("loading seasonal leaderboard")


# ============================================================
# HALL OF FAME
# ============================================================

@app.route("/api/hall-of-fame")
def hall_of_fame():
    try:
        highest = (
            supabase.table("players")
            .select("id, username, rating")
            .order("rating", desc=True)
            .limit(10)
            .execute()
        )

        stats_result = (
            supabase.table("player_stats")
            .select("*")
            .execute()
        )

        stats = stats_result.data or []

        player_ids = [
            item["player_id"]
            for item in stats
        ]

        names = {}

        if player_ids:
            player_result = (
                supabase.table("players")
                .select("id, username")
                .in_("id", player_ids)
                .execute()
            )

            names = {
                p["id"]: p["username"]
                for p in player_result.data or []
            }

        def leaders(field):
            ordered = sorted(
                stats,
                key=lambda item: int(item.get(field) or 0),
                reverse=True
            )

            result = []

            for item in ordered[:10]:
                player_id = item["player_id"]

                if player_id in names:
                    result.append({
                        "username": names[player_id],
                        "value": item.get(field, 0)
                    })

            return result

        most_games = leaders("games_played")
        return jsonify({
            "highest_rated": highest.data or [],
            "longest_win_streaks": leaders("longest_win_streak"),
            "most_games_played": most_games,
            "most_games": most_games
        })

    except Exception:
        return database_error("loading Hall of Fame")


# ============================================================
# PART 2 STARTS BELOW
# ============================================================
# ============================================================
# CHESS ENGINE
# ============================================================

FILES = "abcdefgh"
PROMOTIONS = ["q", "r", "b", "n"]


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


def inside(r, c):
    return 0 <= r < 8 and 0 <= c < 8


def piece_color(piece):
    if piece == ".":
        return None
    return "white" if piece.isupper() else "black"


def opponent(color):
    return "black" if color == "white" else "white"


def square_name(r, c):
    return FILES[c] + str(8 - r)


def parse_square(square):
    if not isinstance(square, str) or len(square) != 2:
        return None

    if square[0] not in FILES or square[1] not in "12345678":
        return None

    return 8 - int(square[1]), FILES.index(square[0])


def copy_board(board):
    return [row[:] for row in board]


def get_piece(board, r, c):
    if not inside(r, c):
        return "."
    return board[r][c]


def is_enemy(board, r, c, color):
    piece = get_piece(board, r, c)
    return piece != "." and piece_color(piece) != color


KNIGHT_STEPS = [
    (-2, -1), (-2, 1),
    (-1, -2), (-1, 2),
    (1, -2), (1, 2),
    (2, -1), (2, 1)
]

KING_STEPS = [
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1)
]

ROOK_DIRS = [
    (-1, 0), (1, 0), (0, -1), (0, 1)
]

BISHOP_DIRS = [
    (-1, -1), (-1, 1), (1, -1), (1, 1)
]


def pseudo_moves(board, r, c, rights=None, en_passant=None):
    piece = get_piece(board, r, c)

    if piece == ".":
        return []

    color = piece_color(piece)
    kind = piece.lower()
    moves = []

    def add(nr, nc):
        if not inside(nr, nc):
            return

        target = board[nr][nc]

        if target == "." or piece_color(target) != color:
            moves.append((nr, nc))

    if kind == "p":
        direction = -1 if color == "white" else 1
        start_row = 6 if color == "white" else 1

        nr = r + direction

        if inside(nr, c) and board[nr][c] == ".":
            moves.append((nr, c))

            nr2 = r + 2 * direction

            if (
                r == start_row
                and inside(nr2, c)
                and board[nr2][c] == "."
            ):
                moves.append((nr2, c))

        for dc in (-1, 1):
            nr = r + direction
            nc = c + dc

            if inside(nr, nc):
                target = board[nr][nc]

                if target != "." and piece_color(target) != color:
                    moves.append((nr, nc))

                if en_passant == (nr, nc):
                    moves.append((nr, nc))

    elif kind == "n":
        for dr, dc in KNIGHT_STEPS:
            add(r + dr, c + dc)

    elif kind == "k":
        for dr, dc in KING_STEPS:
            add(r + dr, c + dc)

        if rights:
            row = 7 if color == "white" else 0

            if r == row and c == 4:
                rook = "R" if color == "white" else "r"

                if rights.get(color + "_kingside"):
                    if (
                        board[row][5] == "."
                        and board[row][6] == "."
                        and board[row][7] == rook
                    ):
                        moves.append((row, 6))

                if rights.get(color + "_queenside"):
                    if (
                        board[row][1] == "."
                        and board[row][2] == "."
                        and board[row][3] == "."
                        and board[row][0] == rook
                    ):
                        moves.append((row, 2))

    elif kind in ("r", "b", "q"):
        directions = []

        if kind in ("r", "q"):
            directions.extend(ROOK_DIRS)

        if kind in ("b", "q"):
            directions.extend(BISHOP_DIRS)

        for dr, dc in directions:
            nr, nc = r + dr, c + dc

            while inside(nr, nc):
                target = board[nr][nc]

                if target == ".":
                    moves.append((nr, nc))
                else:
                    if piece_color(target) != color:
                        moves.append((nr, nc))
                    break

                nr += dr
                nc += dc

    return moves


def update_castling_rights(rights, piece, start, end):
    rights = dict(rights or {})

    r, c = start
    nr, nc = end
    color = piece_color(piece)

    if piece.lower() == "k":
        rights[color + "_kingside"] = False
        rights[color + "_queenside"] = False

    if piece.lower() == "r":
        if color == "white":
            if (r, c) == (7, 0):
                rights["white_queenside"] = False
            if (r, c) == (7, 7):
                rights["white_kingside"] = False
        else:
            if (r, c) == (0, 0):
                rights["black_queenside"] = False
            if (r, c) == (0, 7):
                rights["black_kingside"] = False

    if (nr, nc) == (7, 0):
        rights["white_queenside"] = False
    if (nr, nc) == (7, 7):
        rights["white_kingside"] = False
    if (nr, nc) == (0, 0):
        rights["black_queenside"] = False
    if (nr, nc) == (0, 7):
        rights["black_kingside"] = False

    return rights


def apply_move(board, start, end, promotion="q", en_passant=None):
    board = copy_board(board)

    r, c = start
    nr, nc = end

    piece = board[r][c]
    target = board[nr][nc]

    if piece == ".":
        raise ValueError("No piece on starting square.")

    color = piece_color(piece)
    kind = piece.lower()

    board[r][c] = "."

    if (
        kind == "p"
        and en_passant == (nr, nc)
        and c != nc
        and target == "."
    ):
        board[r][nc] = "."

    if kind == "k" and abs(nc - c) == 2:
        if nc == 6:
            board[r][5] = board[r][7]
            board[r][7] = "."
        else:
            board[r][3] = board[r][0]
            board[r][0] = "."

    if kind == "p" and nr in (0, 7):
        promotion = str(promotion).lower()

        if promotion not in PROMOTIONS:
            promotion = "q"

        piece = promotion.upper() if color == "white" else promotion

    board[nr][nc] = piece

    new_ep = None

    if kind == "p" and abs(nr - r) == 2:
        new_ep = ((r + nr) // 2, c)

    return board, new_ep, target


def legal_destinations(board, r, c, rights=None, en_passant=None):
    return pseudo_moves(
        board,
        r,
        c,
        rights,
        en_passant
    )


# ============================================================
# MAKE MOVE — RECORDS INTENDED AND ACTUAL DESTINATIONS
# ============================================================

def make_move(
    room,
    start,
    end,
    promotion="q",
    player_id=None,
    intended_end=None
):
    board = room["board"]

    r, c = start
    nr, nc = end

    piece = board[r][c]

    if piece == ".":
        raise ValueError("Empty starting square.")

    color = piece_color(piece)

    destinations = pseudo_moves(
        board,
        r,
        c,
        room["rights"],
        room.get("en_passant")
    )

    if end not in destinations:
        raise ValueError("Illegal destination.")

    if intended_end is None:
        intended_end = end

    if intended_end not in destinations:
        raise ValueError("Your intended destination is not legal.")

    # The browser selects a random eligible slip and sends both squares.
    # The server validates the actual destination so clients cannot bypass
    # the guaranteed-mouseslip rule or report false statistics.
    if piece.lower() == "n":
        intended_direction = (intended_end[0] > r) - (intended_end[0] < r)
        alternatives = [
            destination for destination in destinations
            if destination != intended_end
            and ((destination[0] > r) - (destination[0] < r)) == intended_direction
        ]
    else:
        # Non-knight slips must land next to the intended destination.
        alternatives = [
            destination for destination in destinations
            if destination != intended_end
            and abs(destination[0] - intended_end[0]) <= 1
            and abs(destination[1] - intended_end[1]) <= 1
        ]

    if alternatives:
        if end not in alternatives:
            raise ValueError("Your move must mouseslip to an eligible nearby square.")
    elif end != intended_end:
        raise ValueError("There is no eligible mouseslip for that move.")

    nr, nc = end

    # Promotion itself also mouseslips: selecting one piece results in
    # a randomly selected different promotion piece.
    if piece.lower() == "p" and nr in (0, 7):
        promotion = random.choice([
            option for option in PROMOTIONS if option != promotion
        ])

    old_en_passant = room.get("en_passant")

    # Correctly detect normal captures and en passant captures.
    captured_piece = board[nr][nc]

    if (
        piece.lower() == "p"
        and old_en_passant == (nr, nc)
        and c != nc
        and captured_piece == "."
    ):
        captured_piece = board[r][nc]

    # The server determines whether the move slipped.
    slipped = intended_end != end

    new_board, new_ep, _ = apply_move(
        board,
        start,
        end,
        promotion,
        old_en_passant
    )

    room["rights"] = update_castling_rights(
        room["rights"],
        piece,
        start,
        end
    )

    room["board"] = new_board
    room["en_passant"] = new_ep
    room["turn"] = opponent(color)

    move = {
        "from": square_name(r, c),
        "to": square_name(nr, nc),
        "intended_to": square_name(
            intended_end[0],
            intended_end[1]
        ),
        "piece": piece,
        "player_id": player_id,
        "slipped": slipped,
        "captured": captured_piece != ".",
        "captured_piece": captured_piece,
        "promotion": (
            promotion
            if piece.lower() == "p" and nr in (0, 7)
            else None
        ),
        "fen_board": new_board
    }

    room["moves"].append(move)

    return move


# ============================================================
# GAME ROOMS
# ============================================================

def new_room(
    white,
    black,
    seconds,
    increment,
    rated,
    white_id=None,
    black_id=None,
    tournament_id=None,
    tournament_match_id=None,
    private=False
):
    code = "".join(
        secrets.choice(string.ascii_uppercase + string.digits)
        for _ in range(6)
    )

    while code in rooms:
        code = "".join(
            secrets.choice(string.ascii_uppercase + string.digits)
            for _ in range(6)
        )

    now = time.time()

    room = {
        "code": code,
        "match_id": str(uuid.uuid4()),
        "white": white,
        "black": black,
        "white_id": white_id,
        "black_id": black_id,
        "board": initial_board(),
        "rights": {
            "white_kingside": True,
            "white_queenside": True,
            "black_kingside": True,
            "black_queenside": True
        },
        "en_passant": None,
        "turn": "white",
        "moves": [],
        "seconds": max(1, int(seconds)),
        "increment": max(0, int(increment)),
        "white_time": max(1, int(seconds)),
        "black_time": max(1, int(seconds)),
        "last": now,
        "started": now,
        "active": True,
        "over": False,
        "rated": bool(rated),
        "unlimited": int(seconds) == 0,
        "last_clock_emit": now,
        "tournament_id": tournament_id,
        "tournament_match_id": tournament_match_id,
        "private": bool(private)
    }

    rooms[code] = room

    if white:
        sid_room[white] = code

    if black:
        sid_room[black] = code

    return room


def room_snapshot(room):
    return {
        "room": room["code"],
        "board": room["board"],
        "turn": room["turn"],
        "white_time": room["white_time"],
        "black_time": room["black_time"],
        "increment": room["increment"],
        "rated": room["rated"],
        "unlimited": room["unlimited"],
        "tournament": bool(room.get("tournament_match_id")),
        "over": room["over"],
        "moves": room["moves"]
    }


def finish_room(code, winner, reason):
    with lock:
        room = rooms.get(code)

        if not room or room["over"]:
            return

        room["over"] = True
        room["active"] = False
        room["winner"] = winner
        room["reason"] = reason

        match_id = room["match_id"]

        winner_id = (
            room["white_id"] if winner == "white"
            else room["black_id"]
        )

        try:
            supabase.rpc(
                "finalize_match",
                {
                    "p_match_id": match_id,
                    "p_white": room["white_id"],
                    "p_black": room["black_id"],
                    "p_winner_side": winner,
                    "p_reason": reason,
                    "p_rated": room["rated"],
                    "p_moves": room["moves"]
                }
            ).execute()

        except Exception:
            app.logger.exception(
                "Could not save completed match."
            )

        ratings = {}

        for side in ("white", "black"):
            player_id = room.get(side + "_id")

            if player_id:
                try:
                    player = get_player_by_id(player_id)
                    if player:
                        ratings[side] = player["rating"]
                except Exception:
                    pass

        result = {
            "winner": winner,
            "reason": reason,
            "match_id": match_id,
            "ratings": ratings
        }

        socketio.emit(
            "match_result",
            result,
            room=code
        )

        if room.get("tournament_match_id"):
            complete_tournament_match(
                room["tournament_match_id"],
                winner_id
            )


# ============================================================
# CLOCKS
# ============================================================

def clock_worker():
    while True:
        socketio.sleep(0.25)

        with lock:
            now = time.time()

            for code, room in list(rooms.items()):
                if room["over"] or not room["active"]:
                    continue

                if room["unlimited"]:
                    room["last"] = now
                    continue

                elapsed = now - room["last"]
                room["last"] = now

                if room["turn"] == "white":
                    room["white_time"] -= elapsed

                    if room["white_time"] <= 0:
                        room["white_time"] = 0
                        finish_room(code, "black", "time")
                else:
                    room["black_time"] -= elapsed

                    if room["black_time"] <= 0:
                        room["black_time"] = 0
                        finish_room(code, "white", "time")

                if (
                    not room["over"]
                    and now - room.get("last_clock_emit", 0) >= 1
                ):
                    room["last_clock_emit"] = now
                    socketio.emit(
                        "clock_update",
                        {
                            "white_time": max(0, room["white_time"]),
                            "black_time": max(0, room["black_time"]),
                            "turn": room["turn"]
                        },
                        room=code
                    )


# ============================================================
# SOCKET PRESENCE, SPECTATING, FRIEND INVITES AND REMATCHES
# ============================================================

@socketio.on("connect")
def socket_connected(auth=None):
    player = current_player()
    if player:
        player_sids[player["id"]] = request.sid
        connected_sids.add(request.sid)


@socketio.on("register_presence")
def register_socket_presence():
    player = current_player()
    if player:
        player_sids[player["id"]] = request.sid
        connected_sids.add(request.sid)


@socketio.on("unregister_presence")
def unregister_socket_presence():
    player = current_player()
    if player and player_sids.get(player["id"]) == request.sid:
        player_sids.pop(player["id"], None)
    connected_sids.discard(request.sid)


@socketio.on("request_game_state")
def request_game_state(data):
    code = str((data or {}).get("room", "")).strip().upper()
    room = rooms.get(code)
    if not room or room["over"]:
        emit("match_error", {"error": "Game is no longer active."})
        return
    if sid_room.get(request.sid) != code and spectator_rooms.get(request.sid) != code:
        emit("match_error", {"error": "You are not allowed to view this game."})
        return
    emit("game_started", room_snapshot(room))


@socketio.on("spectate_game")
def spectate_game(data):
    code = str((data or {}).get("room", "")).strip().upper()
    room = rooms.get(code)
    if not room or room["over"] or room.get("private") or not room.get("black"):
        emit("spectator_error", {"error": "This game cannot be spectated."})
        return
    spectator_rooms[request.sid] = code
    join_room(code)
    emit("game_started", room_snapshot(room))


@socketio.on("request_rematch")
def request_rematch(data):
    code = str((data or {}).get("room", "")).strip().upper()
    room = rooms.get(code)
    if not room or not room["over"] or room.get("tournament_match_id"):
        emit("rematch_error", {"error": "A rematch is not available."})
        return
    if request.sid not in (room.get("white"), room.get("black")):
        emit("rematch_error", {"error": "You were not a player in this game."})
        return
    opponent_sid = room["black"] if room["white"] == request.sid else room["white"]
    if not opponent_sid or sid_room.get(opponent_sid) != code:
        emit("rematch_error", {"error": "Your opponent is no longer connected."})
        return
    rematch_requests[code] = request.sid
    player = current_player()
    socketio.emit("rematch_offer", {
        "room": code,
        "username": player["username"] if player else "Your opponent"
    }, to=opponent_sid)
    emit("rematch_status", {"message": "Rematch request sent."})


@socketio.on("respond_rematch")
def respond_rematch(data):
    data = data or {}
    code = str(data.get("room", "")).strip().upper()
    room = rooms.get(code)
    requester_sid = rematch_requests.get(code)
    if not room or not room["over"] or not requester_sid or requester_sid == request.sid:
        emit("rematch_error", {"error": "This rematch request is no longer available."})
        return
    if request.sid not in (room.get("white"), room.get("black")):
        emit("rematch_error", {"error": "You were not a player in this game."})
        return
    if not data.get("accept"):
        rematch_requests.pop(code, None)
        socketio.emit("rematch_status", {"message": "Your opponent declined the rematch."}, to=requester_sid)
        return
    if sid_room.get(requester_sid) != code or sid_room.get(request.sid) != code:
        rematch_requests.pop(code, None)
        emit("rematch_error", {"error": "Both players must still be connected."})
        return

    rematch_requests.pop(code, None)
    if random.choice([True, False]):
        white_sid, black_sid = requester_sid, request.sid
        white_id, black_id = room["white_id"] if room["white"] == requester_sid else room["black_id"], room["black_id"] if room["black"] == request.sid else room["white_id"]
    else:
        white_sid, black_sid = request.sid, requester_sid
        white_id, black_id = room["white_id"] if room["white"] == request.sid else room["black_id"], room["white_id"] if room["white"] == requester_sid else room["black_id"]

    seconds = 0 if room["unlimited"] else room["seconds"]
    new_game = new_room(
        white_sid, black_sid, seconds, room["increment"], room["rated"],
        white_id, black_id, private=room.get("private", False)
    )
    join_room(new_game["code"], sid=white_sid)
    join_room(new_game["code"], sid=black_sid)
    for side, sid, opponent_id in (
        ("white", white_sid, black_id),
        ("black", black_sid, white_id)
    ):
        opponent = get_player_by_id(opponent_id)
        socketio.emit("match_found", {
            "room": new_game["code"],
            "color": side,
            "seconds": seconds,
            "increment": new_game["increment"],
            "rated": new_game["rated"],
            "unlimited": new_game["unlimited"],
            "opponent": opponent["username"] if opponent else "Opponent"
        }, to=sid)
    socketio.emit("game_started", room_snapshot(new_game), room=new_game["code"])


@socketio.on("invite_friend")
def invite_friend(data):
    player, error = require_player()
    if error:
        emit("invite_error", {"error": "Log in before inviting a friend."})
        return
    friend_id = str((data or {}).get("friend_id", "")).strip()
    if not friend_id or friend_id == player["id"]:
        emit("invite_error", {"error": "Choose a valid friend."})
        return
    friendship = (
        supabase.table("friend_requests")
        .select("id")
        .or_(
            f"and(sender_id.eq.{player['id']},receiver_id.eq.{friend_id}),"
            f"and(sender_id.eq.{friend_id},receiver_id.eq.{player['id']})"
        )
        .eq("status", "accepted")
        .limit(1)
        .execute()
    )
    if not friendship.data:
        emit("invite_error", {"error": "You can only invite accepted friends."})
        return
    target_sid = player_sids.get(friend_id)
    if not target_sid:
        emit("invite_error", {"error": "That friend is not online right now."})
        return
    invite_id = str(uuid.uuid4())
    game_invites[invite_id] = {
        "from_id": player["id"], "from_sid": request.sid,
        "to_id": friend_id, "seconds": 300, "increment": 0,
        "expires": time.time() + 120
    }
    socketio.emit("game_invite", {
        "invite_id": invite_id, "username": player["username"],
        "time_control": "5 minutes"
    }, to=target_sid)
    emit("invite_status", {"message": "Game invitation sent."})


@socketio.on("respond_game_invite")
def respond_game_invite(data):
    player, error = require_player()
    if error:
        emit("invite_error", {"error": "Log in before responding."})
        return
    data = data or {}
    invite_id = str(data.get("invite_id", ""))
    invite = game_invites.get(invite_id)
    if not invite or invite["to_id"] != player["id"] or invite["expires"] < time.time():
        game_invites.pop(invite_id, None)
        emit("invite_error", {"error": "This game invitation has expired."})
        return
    game_invites.pop(invite_id, None)
    inviter_sid = invite["from_sid"]
    if not data.get("accept"):
        socketio.emit("invite_declined", {"message": player["username"] + " declined your invitation."}, to=inviter_sid)
        return
    if player_sids.get(invite["from_id"]) != inviter_sid:
        emit("invite_error", {"error": "Your friend is no longer online."})
        return
    inviter = get_player_by_id(invite["from_id"])
    if not inviter:
        emit("invite_error", {"error": "The inviting account is unavailable."})
        return
    room = new_room(
        inviter_sid, request.sid, invite["seconds"], invite["increment"],
        False, invite["from_id"], player["id"], private=True
    )
    join_room(room["code"], sid=inviter_sid)
    join_room(room["code"], sid=request.sid)
    for side, sid, opponent_name in (
        ("white", inviter_sid, player["username"]),
        ("black", request.sid, inviter["username"])
    ):
        socketio.emit("match_found", {
            "room": room["code"], "color": side,
            "seconds": room["seconds"], "increment": room["increment"],
            "rated": False, "unlimited": False, "opponent": opponent_name
        }, to=sid)
    socketio.emit("game_started", room_snapshot(room), room=room["code"])


# ============================================================
# MATCHMAKING
# ============================================================

@app.route("/api/live-games")
def live_games():
    try:
        games = []
        for room in list(rooms.values()):
            if room["over"] or not room["active"] or room.get("private") or not room.get("black"):
                continue
            white = get_player_by_id(room.get("white_id"))
            black = get_player_by_id(room.get("black_id"))
            games.append({
                "room": room["code"],
                "white": white["username"] if white else "White",
                "black": black["username"] if black else "Black",
                "turn": room["turn"],
                "white_time": room["white_time"],
                "black_time": room["black_time"],
                "rated": room["rated"],
                "tournament": bool(room.get("tournament_match_id"))
            })
        return jsonify({"games": games})
    except Exception:
        return database_error("loading live games")


@app.route("/api/online/leave", methods=["POST"])
def leave_queue():
    player, error = require_player()

    if error:
        return error

    with lock:
        waiting_players[:] = [
            item for item in waiting_players
            if item.get("player_id") != player["id"]
        ]

    return jsonify({"success": True})


@socketio.on("find_match")
def find_match(data):
    player = current_player()

    if not player:
        emit("match_error", {
            "error": "Log in before playing online."
        })
        return

    data = data or {}

    try:
        seconds = int(data.get("seconds", 300))
        increment = int(data.get("increment", 0))
    except (TypeError, ValueError):
        emit("match_error", {"error": "Invalid time control."})
        return

    if seconds < 0 or seconds > 7200:
        emit("match_error", {"error": "Invalid time control."})
        return

    if increment < 0 or increment > 60:
        emit("match_error", {"error": "Invalid increment."})
        return

    with lock:
        waiting_players[:] = [
            item for item in waiting_players
            if item["sid"] != request.sid
            and item["player_id"] != player["id"]
        ]

        rating = int(player["rating"])
        now = time.time()

        opponent_entry = None
        fallback_entries = []

        for item in waiting_players:
            waited = now - item["joined"]
            allowed_difference = 200 + int(waited * 25)

            other_player = get_player_by_id(item["player_id"])

            if not other_player:
                continue

            if item["seconds"] != seconds or item["increment"] != increment:
                continue

            if abs(other_player["rating"] - rating) <= allowed_difference:
                opponent_entry = item
                break

            # After 30 seconds, stop restricting matchmaking by rating.
            if waited >= 30:
                fallback_entries.append(item)

        if opponent_entry is None and fallback_entries:
            opponent_entry = random.choice(fallback_entries)

        if opponent_entry:
            waiting_players.remove(opponent_entry)

            other_sid = opponent_entry["sid"]
            other_player = get_player_by_id(
                opponent_entry["player_id"]
            )

            if not other_player:
                emit("match_error", {
                    "error": "Opponent account unavailable."
                })
                return

            if random.choice([True, False]):
                white_sid, black_sid = request.sid, other_sid
                white_id, black_id = player["id"], other_player["id"]
            else:
                white_sid, black_sid = other_sid, request.sid
                white_id, black_id = other_player["id"], player["id"]

            room = new_room(
                white_sid,
                black_sid,
                seconds,
                increment,
                True,
                white_id,
                black_id
            )

            join_room(room["code"], sid=white_sid)
            join_room(room["code"], sid=black_sid)

            socketio.emit(
                "match_found",
                {
                    "room": room["code"],
                    "color": "white",
                    "seconds": seconds,
                    "increment": increment,
                    "rated": True,
                    "unlimited": room["unlimited"],
                    "opponent": other_player["username"]
                },
                to=white_sid
            )

            socketio.emit(
                "match_found",
                {
                    "room": room["code"],
                    "color": "black",
                    "seconds": seconds,
                    "increment": increment,
                    "rated": True,
                    "unlimited": room["unlimited"],
                    "opponent": player["username"]
                },
                to=black_sid
            )

        else:
            waiting_players.append({
                "sid": request.sid,
                "player_id": player["id"],
                "seconds": seconds,
                "increment": increment,
                "joined": now
            })

            emit("queue_joined", {
                "message": "Searching for an opponent..."
            })


# ============================================================
# PRIVATE LOBBIES
# ============================================================

@socketio.on("create_private_lobby")
def create_private_lobby(data):
    player = current_player()

    if not player:
        emit("match_error", {
            "error": "Log in before creating a private game."
        })
        return

    data = data or {}

    rated = bool(data.get("rated", False))

    try:
        seconds = int(data.get("seconds", 300))
        increment = int(data.get("increment", 0))
    except (TypeError, ValueError):
        emit("match_error", {"error": "Invalid time control."})
        return

    if seconds < 0 or seconds > 7200 or increment < 0 or increment > 60:
        emit("match_error", {
            "error": "Invalid time control."
        })
        return

    room = new_room(
        request.sid,
        None,
        seconds,
        increment,
        rated,
        player["id"],
        None,
        private=True
    )

    join_room(room["code"])

    emit("private_lobby_created", {
        "room": room["code"],
        "code": room["code"],
        "rated": rated,
        "seconds": seconds,
        "increment": increment
    })


@socketio.on("join_private_lobby")
def join_private_lobby(data):
    player = current_player()

    if not player:
        emit("match_error", {
            "error": "Log in before joining a private game."
        })
        return

    data = data or {}
    code = str(data.get("code", "")).strip().upper()

    with lock:
        room = rooms.get(code)

        if not room or room["over"]:
            emit("match_error", {
                "error": "Lobby not found."
            })
            return

        if room["black"]:
            emit("match_error", {
                "error": "This lobby is already full."
            })
            return

        if room["white_id"] == player["id"]:
            emit("match_error", {
                "error": "You cannot join your own lobby."
            })
            return

        room["black"] = request.sid
        room["black_id"] = player["id"]
        room["last"] = time.time()

        sid_room[request.sid] = code
        join_room(code)

        white_player = get_player_by_id(room["white_id"])

        socketio.emit(
            "match_found",
            {
                "room": code,
                "color": "white",
                "seconds": room["seconds"],
                "increment": room["increment"],
                "rated": room["rated"],
                "unlimited": room["unlimited"],
                "opponent": player["username"]
            },
            to=room["white"]
        )

        emit("match_found", {
            "room": code,
            "color": "black",
            "seconds": room["seconds"],
            "increment": room["increment"],
            "rated": room["rated"],
            "unlimited": room["unlimited"],
            "opponent": (
                white_player["username"]
                if white_player else "Opponent"
            )
        })

        socketio.emit(
            "game_started",
            room_snapshot(room),
            room=code
        )


# ============================================================
# TOURNAMENT MATCH CONNECTION
# ============================================================

@socketio.on("join_tournament_match")
def join_tournament_match(data):
    player = current_player()
    if not player:
        emit("match_error", {"error": "Log in before joining a tournament match."})
        return

    match_id = str((data or {}).get("match_id", "")).strip()
    if not match_id:
        emit("match_error", {"error": "Tournament match ID is missing."})
        return

    try:
        result = (
            supabase.table("tournament_matches")
            .select("*")
            .eq("id", match_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            emit("match_error", {"error": "Tournament match not found."})
            return
        match = result.data[0]
        if player["id"] not in (match["player1_id"], match["player2_id"]):
            emit("match_error", {"error": "You are not part of this tournament match."})
            return
        if match.get("status") == "completed":
            emit("match_error", {"error": "This tournament match is complete."})
            return
        if match.get("status") not in ("ready", "playing"):
            emit("match_error", {"error": "This tournament round is not ready."})
            return
        if match.get("game_match_id"):
            emit("match_error", {"error": "This tournament match already has a game in progress."})
            return

        with tournament_lock:
            waiting = tournament_waiting.setdefault(match_id, {})
            waiting[player["id"]] = request.sid
            required = (match["player1_id"], match["player2_id"])
            if not all(player_id in waiting for player_id in required):
                emit("tournament_waiting", {
                    "message": "Waiting for your tournament opponent to open the match."
                })
                return

            white_sid = waiting[match["player1_id"]]
            black_sid = waiting[match["player2_id"]]
            white_player = get_player_by_id(match["player1_id"])
            black_player = get_player_by_id(match["player2_id"])
            if not white_player or not black_player:
                emit("match_error", {"error": "A tournament player account could not be loaded."})
                return

            room = new_room(
                white_sid, black_sid, 300, 0, False,
                match["player1_id"], match["player2_id"],
                match["tournament_id"], match_id
            )
            join_room(room["code"], sid=white_sid)
            join_room(room["code"], sid=black_sid)
            supabase.table("tournament_matches").update({
                "status": "playing",
                "game_match_id": room["match_id"]
            }).eq("id", match_id).execute()
            tournament_waiting.pop(match_id, None)

            socketio.emit("match_found", {
                "room": room["code"], "color": "white",
                "seconds": 300, "increment": 0, "rated": False,
                "unlimited": False, "tournament": True,
                "opponent": black_player["username"]
            }, to=white_sid)
            socketio.emit("match_found", {
                "room": room["code"], "color": "black",
                "seconds": 300, "increment": 0, "rated": False,
                "unlimited": False, "tournament": True,
                "opponent": white_player["username"]
            }, to=black_sid)
            socketio.emit("game_started", room_snapshot(room), room=room["code"])
    except Exception:
        app.logger.exception("Could not start tournament match.")
        emit("match_error", {"error": "Could not start the tournament match. Please try again."})


# ============================================================
# ONLINE MOVES — TRACKS INTENDED AND ACTUAL DESTINATIONS
# ============================================================

@socketio.on("online_move")
def online_move(data):
    data = data or {}

    with lock:
        code = sid_room.get(request.sid)
        room = rooms.get(code) if code else None

        if not room or room["over"] or not room["black"]:
            emit("move_error", {
                "error": "Game is not active."
            })
            return

        color = (
            "white" if room["white"] == request.sid
            else "black" if room["black"] == request.sid
            else None
        )

        if not color or color != room["turn"]:
            emit("move_error", {
                "error": "It is not your turn."
            })
            return

        start = parse_square(data.get("from"))
        end = parse_square(data.get("to"))

        intended_end = parse_square(
            data.get("intended_to", data.get("to"))
        )

        if not start or not end or not intended_end:
            emit("move_error", {
                "error": "Invalid square."
            })
            return

        piece = room["board"][start[0]][start[1]]

        if piece == "." or piece_color(piece) != color:
            emit("move_error", {
                "error": "You cannot move that piece."
            })
            return

        promotion = str(
            data.get("promotion", "q")
        ).lower()

        if promotion not in PROMOTIONS:
            promotion = "q"

        player_id = room.get(color + "_id")

        try:
            move = make_move(
                room,
                start,
                end,
                promotion,
                player_id,
                intended_end
            )
        except ValueError as exc:
            emit("move_error", {
                "error": str(exc)
            })
            return

        if not room["unlimited"]:
            if color == "white":
                room["white_time"] += room["increment"]
            else:
                room["black_time"] += room["increment"]

        room["last"] = time.time()

        if color == "white":
            room["white_time"] = max(0, room["white_time"])
        else:
            room["black_time"] = max(0, room["black_time"])

        if move["captured_piece"] in ("k", "K"):
            finish_room(
                code,
                color,
                "king captured"
            )
            return

        socketio.emit(
            "move_made",
            {
                "board": room["board"],
                "move": {
                    "from": move["from"],
                    "to": move["to"],
                    "piece": move["piece"],
                    "captured": move["captured_piece"],
                    "promotion": move["promotion"]
                },
                "turn": room["turn"],
                "white_time": room["white_time"],
                "black_time": room["black_time"],
                "moves": room["moves"]
            },
            room=code
        )


# ============================================================
# RESIGNATION AND LEAVING
# ============================================================

@socketio.on("resign")
def resign():
    code = sid_room.get(request.sid)
    room = rooms.get(code) if code else None

    if not room or room["over"]:
        return

    if room["white"] == request.sid:
        finish_room(code, "black", "resignation")
    elif room["black"] == request.sid:
        finish_room(code, "white", "resignation")


@socketio.on("leave_game")
def leave_game():
    spectator_code = spectator_rooms.pop(request.sid, None)
    if spectator_code:
        leave_room(spectator_code)
        return
    code = sid_room.get(request.sid)

    if not code:
        return

    with lock:
        room = rooms.get(code)

        if room and not room["over"] and room["black"]:
            if room["white"] == request.sid:
                finish_room(code, "black", "opponent left")
            elif room["black"] == request.sid:
                finish_room(code, "white", "opponent left")

        sid_room.pop(request.sid, None)
        leave_room(code)


@socketio.on("disconnect")
def disconnected():
    with lock:
        connected_sids.discard(request.sid)
        for player_id, sid in list(player_sids.items()):
            if sid == request.sid:
                player_sids.pop(player_id, None)
        spectator_code = spectator_rooms.pop(request.sid, None)
        if spectator_code:
            leave_room(spectator_code)
        for invite_id, invite in list(game_invites.items()):
            if invite["from_sid"] == request.sid:
                game_invites.pop(invite_id, None)
        waiting_players[:] = [
            item for item in waiting_players
            if item["sid"] != request.sid
        ]
        for match_id, waiting in list(tournament_waiting.items()):
            for player_id, sid in list(waiting.items()):
                if sid == request.sid:
                    waiting.pop(player_id, None)
            if not waiting:
                tournament_waiting.pop(match_id, None)

        code = sid_room.get(request.sid)
        room = rooms.get(code) if code else None

        if room and not room["over"] and room["black"]:
            if room["white"] == request.sid:
                finish_room(code, "black", "disconnect")
            elif room["black"] == request.sid:
                finish_room(code, "white", "disconnect")

        sid_room.pop(request.sid, None)
        connected_sids.discard(request.sid)


# ============================================================
# GAME CHAT
# ============================================================

@socketio.on("send_chat")
def send_chat(data):
    player = current_player()

    if not player:
        emit("chat_error", {
            "error": "Log in to send messages."
        })
        return

    data = data or {}

    code = sid_room.get(request.sid)
    room = rooms.get(code) if code else None

    if not room:
        return

    message = str(
        data.get("message", "")
    ).strip()

    if not message or len(message) > 300:
        return

    message_data = {
        "room": code,
        "player_id": player["id"],
        "username": player["username"],
        "message": message,
        "created_at": utc_now()
    }

    socketio.emit(
        "chat_message",
        message_data,
        room=code
    )

    try:
        supabase.table("game_messages").insert({
            "match_id": code,
            "player_id": player["id"],
            "username": player["username"],
            "message": message
        }).execute()
    except Exception:
        app.logger.exception("Could not save game chat.")


# ============================================================
# PART 3 STARTS BELOW
# ============================================================
# ============================================================
# TOURNAMENT HELPERS
# ============================================================

def current_month_start():
    today = datetime.now(RIYADH).date()
    return today.replace(day=1).isoformat()


def get_or_create_monthly_tournament():
    month = current_month_start()

    result = (
        supabase.table("monthly_tournaments")
        .select("*")
        .eq("month_start", month)
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]

    created = (
        supabase.table("monthly_tournaments")
        .insert({
            "month_start": month,
            "status": "registration"
        })
        .execute()
    )

    return created.data[0] if created.data else None


def tournament_entries(tournament_id):
    result = (
        supabase.table("tournament_entries")
        .select("*")
        .eq("tournament_id", tournament_id)
        .order("joined_at")
        .execute()
    )

    return result.data or []


def create_tournament_round(tournament, players, round_number):
    random.shuffle(players)

    match_rows = []

    for index in range(0, len(players), 2):
        player1 = players[index]
        player2 = players[index + 1]

        match_rows.append({
            "tournament_id": tournament["id"],
            "round_number": round_number,
            "match_number": index // 2 + 1,
            "player1_id": player1,
            "player2_id": player2,
            "status": "ready"
        })

    if match_rows:
        supabase.table("tournament_matches").insert(
            match_rows
        ).execute()


def start_tournament(tournament):
    with tournament_lock:
        existing = (
            supabase.table("monthly_tournaments")
            .select("*")
            .eq("id", tournament["id"])
            .limit(1)
            .execute()
        )

        if not existing.data:
            return

        tournament = existing.data[0]

        if tournament["status"] != "registration":
            return

        entries = tournament_entries(tournament["id"])
        scheduled = parse_tournament_datetime(tournament.get("scheduled_at"))

        if len(entries) != 32 or not all(item.get("is_ready") for item in entries):
            return
        if not scheduled or datetime.now(timezone.utc) < scheduled:
            return

        player_ids = [item["player_id"] for item in entries]
        random.shuffle(player_ids)

        for index, entry in enumerate(entries):
            supabase.table("tournament_entries").update({
                "seed": index + 1
            }).eq("id", entry["id"]).execute()

        supabase.table("monthly_tournaments").update({
            "status": "in_progress"
        }).eq("id", tournament["id"]).execute()

        create_tournament_round(tournament, player_ids, 1)


# ============================================================
# TOURNAMENT REGISTRATION
# ============================================================

@app.route("/api/tournament")
def tournament_info():
    try:
        tournament = get_or_create_monthly_tournament()

        if not tournament:
            return jsonify({
                "error": "Could not load tournament."
            }), 500

        entries = tournament_entries(tournament["id"])

        matches_result = (
            supabase.table("tournament_matches")
            .select("*")
            .eq("tournament_id", tournament["id"])
            .order("round_number")
            .order("match_number")
            .execute()
        )

        names = {}
        player_ids = set()

        for entry in entries:
            player_ids.add(entry["player_id"])

        for match in matches_result.data or []:
            if match.get("player1_id"):
                player_ids.add(match["player1_id"])

            if match.get("player2_id"):
                player_ids.add(match["player2_id"])

        for player_id in player_ids:
            player = get_player_by_id(player_id)

            if player:
                names[player_id] = player["username"]

        logged_in_player = current_player()
        my_entry = next(
            (item for item in entries
             if logged_in_player and item["player_id"] == logged_in_player["id"]),
            None
        )

        return jsonify({
            "tournament": tournament,
            "players": [
                {
                    "username": names.get(
                        item["player_id"],
                        "Player"
                    ),
                    "seed": item.get("seed"),
                    "eliminated_round": item.get("eliminated_round")
                }
                for item in entries
            ],
            "matches": matches_result.data or [],
            "player_names": names,
            "maximum_players": 32,
            "joined": len(entries),
            "ready": sum(1 for item in entries if item.get("is_ready")),
            "my_entry_joined": bool(my_entry),
            "my_entry_ready": bool(my_entry and my_entry.get("is_ready"))
        })

    except Exception:
        return database_error("loading tournament")


@app.route("/api/tournament/join", methods=["POST"])
def join_tournament():
    try:
        player, error = require_player()

        if error:
            return error

        tournament = get_or_create_monthly_tournament()

        if not tournament:
            return jsonify({
                "error": "Tournament unavailable."
            }), 500

        if tournament["status"] != "registration":
            return jsonify({
                "error": "This tournament has already started."
            }), 409

        entries = tournament_entries(tournament["id"])

        if any(
            item["player_id"] == player["id"]
            for item in entries
        ):
            return jsonify({
                "error": "You have already joined."
            }), 409

        if len(entries) >= 32:
            return jsonify({
                "error": "The tournament is full."
            }), 409

        supabase.table("tournament_entries").insert({
            "tournament_id": tournament["id"],
            "player_id": player["id"]
        }).execute()

        entries = tournament_entries(tournament["id"])

        return jsonify({
            "success": True,
            "joined": len(entries),
            "started": False
        })

    except Exception:
        return database_error("joining tournament")


@app.route("/api/tournament/ready", methods=["POST"])
def ready_for_tournament():
    try:
        player, error = require_player()
        if error:
            return error

        tournament = get_or_create_monthly_tournament()
        if not tournament or tournament.get("status") != "registration":
            return jsonify({"error": "Registration is closed."}), 409

        entries = tournament_entries(tournament["id"])
        entry = next(
            (item for item in entries if item["player_id"] == player["id"]),
            None
        )
        if not entry:
            return jsonify({"error": "Join the tournament before checking in."}), 409
        if len(entries) != 32:
            return jsonify({"error": "All 32 players must join before anyone can check in."}), 409

        if not entry.get("is_ready"):
            supabase.table("tournament_entries").update({
                "is_ready": True,
                "ready_at": utc_now()
            }).eq("id", entry["id"]).execute()

        entries = tournament_entries(tournament["id"])
        ready_count = sum(1 for item in entries if item.get("is_ready"))
        scheduled_at = tournament.get("scheduled_at")

        if ready_count == 32 and not scheduled_at:
            local_now = datetime.now(RIYADH)
            tournament_day = (local_now.date() + timedelta(days=7))
            scheduled_local = datetime(
                tournament_day.year, tournament_day.month, tournament_day.day,
                17, 0, 0, tzinfo=RIYADH
            )
            scheduled_at = scheduled_local.astimezone(timezone.utc).isoformat()
            supabase.table("monthly_tournaments").update({
                "scheduled_at": scheduled_at,
                "announcement_sent": False,
                "reminder_24h_sent": False,
                "reminder_1h_sent": False
            }).eq("id", tournament["id"]).execute()

            try:
                process_scheduled_tournaments()
            except Exception:
                app.logger.exception("Could not send tournament announcement.")

        return jsonify({
            "success": True,
            "ready": True,
            "ready_count": ready_count,
            "scheduled_at": scheduled_at
        })
    except Exception:
        return database_error("checking in for tournament")


@app.route("/api/push/subscribe", methods=["POST"])
def subscribe_push():
    try:
        player, error = require_player()
        if error:
            return error

        data = request.get_json(silent=True) or {}
        token = str(data.get("token", "")).strip()
        if not token or len(token) > 4096:
            return jsonify({"error": "Invalid notification token."}), 400

        supabase.table("push_subscriptions").upsert({
            "player_id": player["id"],
            "fcm_token": token,
            "updated_at": utc_now()
        }, on_conflict="fcm_token").execute()

        return jsonify({"success": True})
    except Exception:
        return database_error("saving push subscription")


@app.route("/api/push/unsubscribe", methods=["POST"])
def unsubscribe_push():
    try:
        player, error = require_player()
        if error:
            return error

        data = request.get_json(silent=True) or {}
        token = str(data.get("token", "")).strip()
        if token:
            supabase.table("push_subscriptions").delete().eq(
                "player_id", player["id"]
            ).eq("fcm_token", token).execute()

        return jsonify({"success": True})
    except Exception:
        return database_error("removing push subscription")


# ============================================================
# TOURNAMENT MATCH COMPLETION
# ============================================================

def complete_tournament_match(match_id, winner_id):
    with tournament_lock:
        result = (
            supabase.table("tournament_matches")
            .select("*")
            .eq("id", match_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return

        match = result.data[0]

        if match["status"] == "completed":
            return

        if winner_id not in (
            match["player1_id"],
            match["player2_id"]
        ):
            return

        supabase.table("tournament_matches").update({
            "winner_id": winner_id,
            "status": "completed",
            "completed_at": utc_now()
        }).eq("id", match_id).execute()

        loser_id = (
            match["player2_id"]
            if winner_id == match["player1_id"]
            else match["player1_id"]
        )

        supabase.table("tournament_entries").update({
            "eliminated_round": match["round_number"]
        }).eq(
            "tournament_id", match["tournament_id"]
        ).eq(
            "player_id", loser_id
        ).execute()

        round_matches = (
            supabase.table("tournament_matches")
            .select("*")
            .eq("tournament_id", match["tournament_id"])
            .eq("round_number", match["round_number"])
            .execute()
        )

        matches = round_matches.data or []

        # Wait until every match in this round is completed.
        if not matches or any(
            item["status"] != "completed"
            for item in matches
        ):
            return

        winners = [
            item["winner_id"]
            for item in matches
            if item.get("winner_id")
        ]

        if len(winners) == 1:
            supabase.table("monthly_tournaments").update({
                "status": "completed",
                "champion_id": winners[0]
            }).eq(
                "id", match["tournament_id"]
            ).execute()
            return

        next_round = match["round_number"] + 1

        if next_round > 5:
            return

        create_tournament_round(
            {"id": match["tournament_id"]},
            winners,
            next_round
        )


# ============================================================
# TOURNAMENT GAME START
# ============================================================

@app.route(
    "/api/tournament/match/<match_id>/start",
    methods=["POST"]
)
def start_tournament_match(match_id):
    try:
        player, error = require_player()

        if error:
            return error

        result = (
            supabase.table("tournament_matches")
            .select("*")
            .eq("id", match_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return jsonify({
                "error": "Tournament match not found."
            }), 404

        match = result.data[0]

        if player["id"] not in (
            match["player1_id"],
            match["player2_id"]
        ):
            return jsonify({
                "error": "You are not in this match."
            }), 403

        if match["status"] == "completed":
            return jsonify({
                "error": "This match is already complete."
            }), 409

        if match["status"] not in ("ready", "playing"):
            return jsonify({
                "error": "This round is not ready."
            }), 409

        opponent_id = (
            match["player2_id"]
            if player["id"] == match["player1_id"]
            else match["player1_id"]
        )

        opponent_player = get_player_by_id(opponent_id)

        if not opponent_player:
            return jsonify({
                "error": "Opponent unavailable."
            }), 404

        return jsonify({
            "success": True,
            "opponent": opponent_player["username"],
            "match_id": match_id,
            "status": match["status"]
        })

    except Exception:
        return database_error("starting tournament match")


# ============================================================
# DAILY CHALLENGE COMPLETION
# ============================================================

@app.route("/api/daily-challenge")
def daily_challenge():
    try:
        player = current_player()
        today = datetime.now(RIYADH).date().isoformat()
        challenge_result = (
            supabase.table("daily_challenges")
            .select("*")
            .eq("challenge_date", today)
            .limit(1)
            .execute()
        )
        if not challenge_result.data:
            return jsonify({"challenge": None, "completed": False})
        challenge = challenge_result.data[0]
        completed = False
        if player:
            completion = (
                supabase.table("daily_challenge_completions")
                .select("id")
                .eq("challenge_id", challenge["id"])
                .eq("player_id", player["id"])
                .limit(1)
                .execute()
            )
            completed = bool(completion.data)
        return jsonify({
            "challenge": challenge,
            "completed": completed
        })
    except Exception:
        return database_error("loading daily challenge")


@app.route(
    "/api/daily-challenge/complete",
    methods=["POST"]
)
def complete_daily_challenge():
    try:
        player, error = require_player()

        if error:
            return error

        today = datetime.now(RIYADH).date().isoformat()

        challenge_result = (
            supabase.table("daily_challenges")
            .select("*")
            .eq("challenge_date", today)
            .limit(1)
            .execute()
        )

        if not challenge_result.data:
            return jsonify({
                "error": "No daily challenge today."
            }), 404

        challenge = challenge_result.data[0]

        existing = (
            supabase.table("daily_challenge_completions")
            .select("*")
            .eq("challenge_id", challenge["id"])
            .eq("player_id", player["id"])
            .limit(1)
            .execute()
        )

        if existing.data:
            return jsonify({
                "error": "You have already completed this challenge."
            }), 409

        supabase.table(
            "daily_challenge_completions"
        ).insert({
            "challenge_id": challenge["id"],
            "player_id": player["id"]
        }).execute()

        return jsonify({
            "success": True
        })

    except Exception:
        return database_error("completing daily challenge")


# ============================================================
# GAME OF THE DAY
# ============================================================

@app.route("/api/game-of-the-day")
def game_of_the_day():
    try:
        result = (
            supabase.table("game_of_the_day")
            .select("*")
            .limit(1)
            .execute()
        )

        if not result.data:
            return jsonify({
                "game": None
            })

        selected = result.data[0]

        match = (
            supabase.table("match_history")
            .select("*")
            .eq("match_id", selected["match_id"])
            .limit(1)
            .execute()
        )

        return jsonify({
            "game": match.data[0] if match.data else None
        })

    except Exception:
        return database_error("loading game of the day")


# ============================================================
# HEALTH CHECK AND STARTUP
# ============================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "application": "Mouseslip Chess"
    })


if __name__ == "__main__":
    socketio.start_background_task(clock_worker)
    socketio.start_background_task(tournament_scheduler)

    port = int(os.environ.get("PORT", 5000))

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True
    )
