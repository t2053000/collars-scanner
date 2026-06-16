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
from ibkr_client   import IbkrClient
from itm_ibkr      import ItmIbkrScanner
import github_store
import bot as bot_module


def _configure_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
    )


def _bootstrap_schwab_token():
    log        = logging.getLogger("main")
    token_json = os.getenv("SCHWAB_TOKEN_JSON")
    token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))
    if token_json and not token_path.exists():
        token_path.write_text(token_json)
        log.info(f"Wrote primary Schwab token from env to {token_path}")


def _load_schwab_clients(primary_user_id: int,
                         log: logging.Logger) -> dict[int, SchwabClient]:
    clients = {}
    primary_token_path = os.getenv("SCHWAB_TOKEN_PATH", "token.json")
    try:
        primary = SchwabClient(token_path=primary_token_path)
        primary.initialize()
        clients[primary_user_id] = primary
        log.info(f"Loaded primary Schwab client for user {primary_user_id}")
    except Exception as e:
        log.error(f"Failed to initialise primary Schwab client: {e}")
        sys.exit(1)

    stored_ids = github_store.list_schwab_token_user_ids()
    for uid in stored_ids:
        if uid == primary_user_id:
            continue
        try:
            local_path = github_store.load_schwab_token(uid)
            if local_path:
                client = SchwabClient(token_path=local_path)
                client.initialize()
                clients[uid] = client
                log.info(f"Loaded Schwab client for user {uid} from {local_path}")
        except Exception as e:
            log.warning(f"Failed to load Schwab client for user {uid}: {e}")

    return clients


def _init_ibkr_scanner(initial_div_freqs: dict,
                        log: logging.Logger) -> "ItmIbkrScanner | None":
    """Connect to IBKR Gateway via Tailscale. Returns None if unavailable."""
    try:
        client = IbkrClient()
        client.connect()
        scanner = ItmIbkrScanner(client, initial_div_freqs)
        log.info("IBKR scanner connected")
        return scanner
    except Exception as e:
        log.warning(f"IBKR scanner unavailable: {e}")
        return None


def main():
    _configure_logging()
    log = logging.getLogger("main")

    required = [
        "TELEGRAM_BOT_TOKEN",
        "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET",
        "GITHUB_TOKEN", "GITHUB_REPO",
        "PRIMARY_TELEGRAM_USER_ID",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f"❌ Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    _bootstrap_schwab_token()

    primary_user_id = int(os.environ["PRIMARY_TELEGRAM_USER_ID"])
    schwab_clients  = _load_schwab_clients(primary_user_id, log)

    log.info(f"Schwab clients loaded for users: {list(schwab_clients.keys())}")

    primary_schwab    = schwab_clients[primary_user_id]
    initial_div_freqs = github_store.get_div_tickers()
    log.info(f"Loaded {len(initial_div_freqs)} dividend tickers from GitHub")

    collar_scanner   = CollarScanner(primary_schwab)
    spread_scanner   = SpreadScanner(primary_schwab)
    deepcall_scanner = DeepCallScanner(primary_schwab)
    dca_scanner      = DcaScanner(primary_schwab, initial_div_freqs)
    csp_scanner      = CspScanner(primary_schwab, initial_div_freqs)
    itm_scanner      = ItmScanner(primary_schwab, initial_div_freqs)
    ritm_scanner     = RitmScanner(primary_schwab, initial_div_freqs)

    # IBKR scanner — scanning only, execution stays on Schwab
    # Returns None if gateway unreachable — bot starts normally without it
    itm_ibkr_scanner = _init_ibkr_scanner(initial_div_freqs, log)

    app = bot_module.build_app(
        os.environ["TELEGRAM_BOT_TOKEN"],
        collar_scanner,
        spread_scanner,
        deepcall_scanner,
        dca_scanner,
        csp_scanner,
        itm_scanner,
        ritm_scanner,
        schwab_clients,
        primary_user_id,
        itm_ibkr_scanner,
    )

    log.info("Bot starting – polling Telegram…")
    app.run_polling(allowed_updates=["message", "callback_query"])
if __name__ == "__main__":
    main()
