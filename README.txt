MOUSESLIP CHESS
===============

Run locally on Windows
-----------------------
1. Install the requirements:
   py -m pip install -r requirements.txt

2. Start the server:
   py app.py

3. Open:
   http://127.0.0.1:5000

Online matchmaking
------------------
The random matchmaking feature works when the server is running and reachable
by both players. `127.0.0.1` is only your own computer. To play with random
people over the internet, deploy this Flask-SocketIO app to a public host that
supports persistent WebSocket connections. Do not publish your home IP address.

Included features
-----------------
- Main menu and bot setup (difficulty 1-10, color, time choice)
- Local two-player Mouseslip game
- Random-move bot with a simple difficulty-based preference
- Random online matchmaking and move relay
- King capture ends the game
- Knight directional exception
- Legal move filtering and nearby-square mouseslips

Rules implemented for mouseslips
---------------------------------
For non-knights, the slip candidates are legal destinations within one square
of the intended destination (Chebyshev distance <= 1). If there is only one
legal destination for the piece, no slip occurs.

For knights, candidate destinations must also have a rank-direction matching
the intended knight move. This implements the previously described example:
from d4 intending c6, b5, e6 and f5 are permitted, while b3, c2, e2 and f3
are forbidden.

Notes
-----
This is a first complete playable implementation of the core rules. It uses
automatic queen promotion. Advanced chess conveniences such as castling,
en-passant, draw claims, persistent accounts, ratings, chat, and server-side
anti-cheat are not included in this initial build.
