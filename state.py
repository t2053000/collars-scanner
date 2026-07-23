"""
bot/state.py

Module-level constants and shared mutable state for the Telegram bot.

Other submodules import the *containers* below directly (deque/dict/set) —
that's safe because their identity never changes, only their contents.
`_LAST_TOKEN_SAVE` is a plain float that gets *reassigned*, so anything that
needs to update it does `from . import state; state._LAST_TOKEN_SAVE = ...`
rather than importing the name directly (which would only rebind a local
copy).
"""
from collections import deque

# ── Scanning ────────────────────────────────────────────────────────────
SCAN_CONCURRENCY = 12
TICKER_BLACKLIST = {"VIVO", "GRRR"}

# ── Telegram message length handling ────────────────────────────────────
TG_MAX_LEN = 4000

# Recent scan errors, surfaced by /logs
_LAST_ERRORS: deque = deque(maxlen=30)

# Pending trade confirmations, keyed by (user_id, trade_id)
_PENDING_TRADES: dict = {}
PENDING_TIMEOUT_SEC = 60

# Orders currently being polled for fill, keyed by order_id
_ACTIVE_ORDERS: dict = {}

# ITMT auto-trader run state, keyed by user_id
_ITMT_STOP: set = set()
_ITMT_RUNNING: set = set()

# Schwab token persistence throttling
_LAST_TOKEN_SAVE: float = 0.0
TOKEN_SAVE_INTERVAL = 3600

# ITM trade-card walk steps / button cap
MAX_WALK_STEPS = 8
MAX_TRADE_BUTTONS = 20
