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
import github_store
import bot as bot_module


def _configure_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
    )


def _bootstrap_schwab_token():
    token_json = os.getenv("SCHWAB_TOKEN_JSON")
    token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))
    if token_json and not token_path.exists():
        token_path.write_text(token_json)
        logging.getLogger(__name__).info(
            f"Wrote Schwab token from env to {token_path}"
        )


def main():
    _configure_logging()
    log = logging.getLogger("main")

    required = [
        "TELEGRAM_BOT_TOKEN",
        "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET",
        "GITHUB_TOKEN", "GITHUB_REPO",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f"❌ Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    _bootstrap_schwab_token()

    schwab = SchwabClient()
    schwab.initialize()

    initial_div_freqs = github_store.get_div_tickers()
    log.info(f"Loaded {len(initial_div_freqs)} dividend tickers from GitHub")

    collar_scanner   = CollarScanner(schwab)
    spread_scanner   = SpreadScanner(schwab)
    deepcall_scanner = DeepCallScanner(schwab)
    dca_scanner      = DcaScanner(schwab, initial_div_freqs)
    csp_scanner      = CspScanner(schwab)

    app = bot_module.build_app(
        os.environ["TELEGRAM_BOT_TOKEN"],
        collar_scanner,
        spread_scanner,
        deepcall_scanner,
        dca_scanner,
        csp_scanner,
        schwab,
    )

    log.info("Bot starting – polling Telegram…")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
