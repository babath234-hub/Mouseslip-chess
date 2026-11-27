MOUSESLIP CHESS
===============

Mouseslip Chess is a chess variant where a selected move can slip to a
nearby legal destination. Capturing the opposing king wins the game.

Run locally on Windows
-----------------------
1. Install Python 3.12.11.
2. Open Command Prompt in this folder.
3. Install the dependencies:

   py -m pip install -r requirements.txt

4. Set the required environment variables before starting the app:

   SUPABASE_URL
   SUPABASE_SECRET_KEY
   FLASK_SECRET_KEY

   Firebase tournament notifications also require the Firebase service
   account JSON and VAPID key configured in the environment.

5. Start the server:

   py app.py

6. Open:

   http://127.0.0.1:5000

Deployment
----------
Deploy the Flask-SocketIO application on a host that supports persistent
WebSocket connections. Configure the environment variables in the hosting
provider's dashboard. Never commit Supabase service-role keys, Flask secrets,
or Firebase service-account credentials to Git.

Database setup
--------------
The application uses an existing Supabase database, including its core
players, matches, statistics, friends, comments, feedback, tournament,
customization, and notification tables.

Run these setup scripts in the Supabase SQL Editor when enabling the
corresponding features:

- daily_challenge_setup.sql
- killer_awards_setup.sql

The setup scripts depend on the existing public.players table.

Current features
----------------
- Local two-player Mouseslip Chess
- Play against a bot with selectable difficulty and time controls
- Online random matchmaking with rating-based matching and fallback
- Private lobbies and friend invitations
- Online clocks with optional increments
- Rated and unrated games
- King capture wins the game
- Mouseslip rules for all pieces, including captures and promotion
- Knight directional exception
- Castling and en passant
- Player accounts, ratings, statistics, match history, and replays
- Achievements and Killer Awards
- Friends, community comments, and private feedback
- Monthly tournaments and tournament notifications
- Live-game spectating
- Daily Challenge submissions and play
- Game of the Day
- Board and piece customisation

Project files
-------------
- app.py: Flask application, game rules, online rooms, APIs, and background
  tasks.
- templates/index.html: website interface, styling, and browser-side game
  logic.
- static/firebase-messaging-sw.js: Firebase background notification worker.
- daily_challenge_setup.sql: Daily Challenge database tables.
- killer_awards_setup.sql: Achievement unlock and Killer Award progress tables.
- requirements.txt: Python dependencies.
- .python-version: Render Python runtime version.
- .github/workflows/validate.yml: Python and JavaScript syntax checks.
