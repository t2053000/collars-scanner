"""
main.py — Entry point.
"""

import os
import sys
import logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from schwab_client import SchwabClient
from scanner       import CollarScanner
from spreads       import SpreadScanner
from deepcall      import DeepCallScanner
from dca           import DcaScanner
from csp           import CspScanner
from itm           import ItmScanner
from ritm          import RitmScanner
import github_store
import bot as bot_module


def _configure_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
    )


def _bootstrap_schwab_token():
    """
    Bootstrap primary Schwab token from env var SCHWAB_TOKEN_JSON.
    Also bootstrap any per-user tokens stored in GitHub.
    """
    log        = logging.getLogger("main")
    token_json = os.getenv("SCHWAB_TOKEN_JSON")
    token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))

    # Primary token (yours) — from env var as before
    if token_json and not token_path.exists():
        token_path.write_text(token_json)
        log.info(f"Wrote primary Schwab token from env to {token_path}")


def _load_schwab_clients(primary_user_id: int,
                         log: logging.Logger) -> dict[int, SchwabClient]:
    """
    Build a dict of {telegram_user_id: SchwabClient} for all users
    who have a stored Schwab token.

    The primary user (you) uses the default token.json path.
    Additional users (e.g. your wife) use token_{user_id}.json loaded from GitHub.
    """
    clients = {}

    # Primary user — default token path
    primary_token_path = os.getenv("SCHWAB_TOKEN_PATH", "token.json")
    try:
        primary = SchwabClient(token_path=primary_token_path)
        primary.initialize()
        clients[primary_user_id] = primary
        log.info(f"Loaded primary Schwab client for user {primary_user_id}")
    except Exception as e:
        log.error(f"Failed to initialise primary Schwab client: {e}")
        sys.exit(1)

    # Additional users — tokens stored in GitHub
    stored_ids = github_store.list_schwab_token_user_ids()
    for uid in stored_ids:
        if uid == primary_user_id:
            continue  # already loaded above
        try:
            local_path = github_store.load_schwab_token(uid)
            if local_path:
                client = SchwabClient(token_path=local_path)
                client.initialize()
                clients[uid] = client
                log.info(f"Loaded Schwab client for user {uid} "
                         f"from {local_path}")
        except Exception as e:
            log.warning(f"Failed to load Schwab client for user {uid}: {e}")

    return clients


def main():
    _configure_logging()
    log = logging.getLogger("main")

    required = [
        "TELEGRAM_BOT_TOKEN",
        "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET",
        "GITHUB_TOKEN", "GITHUB_REPO",
        "PRIMARY_TELEGRAM_USER_ID",   # your Telegram user ID
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f"❌ Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    _bootstrap_schwab_token()

    primary_user_id = int(os.environ["PRIMARY_TELEGRAM_USER_ID"])
    schwab_clients  = _load_schwab_clients(primary_user_id, log)

    log.info(f"Schwab clients loaded for users: "
             f"{list(schwab_clients.keys())}")

    # Scanning uses primary client (yours) — unchanged
    primary_schwab = schwab_clients[primary_user_id]

    initial_div_freqs = github_store.get_div_tickers()
    log.info(f"Loaded {len(initial_div_freqs)} dividend tickers from GitHub")

    collar_scanner   = CollarScanner(primary_schwab)
    spread_scanner   = SpreadScanner(primary_schwab)
    deepcall_scanner = DeepCallScanner(primary_schwab)
    dca_scanner      = DcaScanner(primary_schwab, initial_div_freqs)
    csp_scanner      = CspScanner(primary_schwab, initial_div_freqs)
    itm_scanner      = ItmScanner(primary_schwab, initial_div_freqs)
    ritm_scanner     = RitmScanner(primary_schwab, initial_div_freqs)

    app = bot_module.build_app(
        os.environ["TELEGRAM_BOT_TOKEN"],
        collar_scanner,
        spread_scanner,
        deepcall_scanner,
        dca_scanner,
        csp_scanner,
        itm_scanner,
        ritm_scanner,
        schwab_clients,        # dict instead of single client
        primary_user_id,       # so bot knows whose client is the fallback
    )

    log.info("Bot starting – polling Telegram…")
    app.run_polling(allowed_updates=["message", "callback_query"])

from ibkr_client import IbkrClient
from itm_ibkr   import ItmIbkrScanner

# IBKR scanner — scanning only, execution stays on Schwab
try:
    ibkr_client     = IbkrClient()
    ibkr_client.connect()
    itm_ibkr_scanner = ItmIbkrScanner(ibkr_client, initial_div_freqs)
    log.info("IBKR scanner connected")
except Exception as e:
    itm_ibkr_scanner = None
    log.warning(f"IBKR scanner unavailable: {e}")
if __name__ == "__main__":
    main()
