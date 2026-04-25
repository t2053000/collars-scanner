"""
main.py — entry point for the collar scanner bot.

Env vars:
  Required:
    TELEGRAM_BOT_TOKEN
    SCHWAB_APP_KEY
    SCHWAB_APP_SECRET
    SCHWAB_TOKEN_JSON        – contents of token.json (used on Railway)
    GITHUB_TOKEN             – PAT with repo scope
    GITHUB_REPO              – e.g. "youruser/collar-bot-data"
  Optional:
    SCHWAB_TOKEN_PATH        – default "token.json"
    SCHWAB_REDIRECT_URI      – default "https://127.0.0.1"
    GITHUB_TICKERS_PATH      – default "tickers.txt"
    GITHUB_WHITELIST_PATH    – default "whitelist.txt"
    LOG_LEVEL                – default "INFO"
"""

import os
import sys
import logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import bot as bot_module
from schwab_client import SchwabClient
from scanner import CollarScanner


def configure_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)7s | %(name)s | %(message)s",
    )
    for noisy in ("httpx", "httpcore", "apscheduler", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bootstrap_schwab_token():
    """Materialise SCHWAB_TOKEN_JSON env var to disk (for Railway)."""
    token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))
    token_json = os.getenv("SCHWAB_TOKEN_JSON")
    if token_json and not token_path.exists():
        token_path.write_text(token_json)
        logging.info(f"Wrote Schwab token from env var → {token_path}")


def require_env(*keys):
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        sys.exit(f"❌ Missing required env vars: {', '.join(missing)}")


def main():
    configure_logging()
    require_env(
        "TELEGRAM_BOT_TOKEN",
        "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET",
        "GITHUB_TOKEN",   "GITHUB_REPO",
    )
    bootstrap_schwab_token()

    schwab = SchwabClient()
    schwab.initialize()
    scanner = CollarScanner(schwab)

    app = bot_module.build_app(os.environ["TELEGRAM_BOT_TOKEN"], scanner, schwab)

    logging.info("Bot starting – polling Telegram…")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
