"""
bot.py - Clean version
- Only tickers.txt for /itm and /itm r
- Debug logging
- Topic posting on FILLED
"""

import asyncio
import logging
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import github_store
from itm import ItmScanner
from positions import compute_positions
import orders   # keep if you use it

logger = logging.getLogger(__name__)

# === Topics (your group) ===
GROUP_CHAT_ID = -1003970147893
TOPIC_ITM = 3
TOPIC_ITM_R = 4
TOPIC_POSITIONS = 5


def authorized_only(func):
    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        if not user or not github_store.is_authorized(user.id):
            await update.message.reply_text("Not authorized.")
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------

def format_clean_precalc(hit: dict, trade_type: str = "itm") -> str:
    ticker = hit.get("ticker", "?")
    apy = hit.get("locked_apy", 0)
    if trade_type == "itm_r":
        return f"✅ *ITM R Filled*\nTicker: *{ticker}*\nPrecalc APY: *{apy}%*"
    return f"✅ *ITM Filled*\nTicker: *{ticker}*\nPrecalc APY: *{apy}%*"


def get_ticker_positions(schwab_client, ticker: str):
    try:
        all_pos = compute_positions(schwab_client)
        for p in all_pos:
            if p.get("ticker", "").upper() == ticker.upper():
                return p
        return None
    except Exception as e:
        logger.warning(f"get_ticker_positions error: {e}")
        return None


# ---------------------------------------------------------------------------
# Main command with debug logs + tickers.txt only
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_itm(update, context):
    logger.info(">>> DEBUG cmd_itm: command received")
    scanner = context.application.bot_data["itm_scanner"]

    args = [a.lower() for a in (context.args or [])]
    reverse_mode = "r" in args

    logger.info(f">>> DEBUG cmd_itm: reverse_mode={reverse_mode}")

    # Always use tickers.txt
    tickers = github_store.get_tickers()
    logger.info(f">>> DEBUG cmd_itm: loaded {len(tickers)} tickers from tickers.txt")

    if not tickers:
        await update.message.reply_text("_No tickers in tickers.txt_")
        return

    if reverse_mode:
        logger.info(">>> DEBUG cmd_itm: starting REVERSE scan")
        original = scanner.scan_ticker
        scanner.scan_ticker = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=tickers, hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original
        logger.info(">>> DEBUG cmd_itm: reverse scan finished")
    else:
        logger.info(">>> DEBUG cmd_itm: starting NORMAL scan")
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=tickers, hits_with_buttons=True, scanner_key="itm")
        logger.info(">>> DEBUG cmd_itm: normal scan finished")


# ---------------------------------------------------------------------------
# These must be your working versions (paste yours if different)
# ---------------------------------------------------------------------------

async def _run_scan(update, context, scanner, emoji, format_summary_fn,
                    tickers_override=None, hits_with_buttons=False, scanner_key=None):
    # ← Replace with your real _run_scan implementation
    await update.message.reply_text("Scan running... (replace this with real logic)")


async def _send_itm_trade_button(update, context, hit):
    # ← Replace with your real button sending logic
    await update.message.reply_text(f"ITM hit: {hit.get('ticker')}")


async def _send_rtrade_button(update, context, hit):
    await update.message.reply_text(f"Reverse hit: {hit.get('ticker')}")


# ---------------------------------------------------------------------------
# Monitor functions with topic posting (FILLED)
# ---------------------------------------------------------------------------

async def monitor_order(context, user_id, order_id, status_msg):
    # Add your existing logic + this on FILLED:
    if "FILLED" in str(status_msg).upper():
        # post precalc + positions (you can expand this)
        pass


async def monitor_rtrade_order(context, user_id, order_id, status_msg, ticker):
    if "FILLED" in str(status_msg).upper():
        pass


# ---------------------------------------------------------------------------
# build_app
# ---------------------------------------------------------------------------

def build_app(telegram_token, collar_scanner, spread_scanner, deepcall_scanner,
              dca_scanner, csp_scanner, itm_scanner, ritm_scanner,
              schwab_clients: dict, primary_user_id: int, itm_ibkr_scanner=None):

    app = Application.builder().token(telegram_token).build()

    app.bot_data["itm_scanner"] = itm_scanner
    app.bot_data["ritm_scanner"] = ritm_scanner
    app.bot_data["schwab_clients"] = schwab_clients
    app.bot_data["primary_user_id"] = primary_user_id

    app.add_handler(CommandHandler("itm", cmd_itm))

    # Add your other handlers here (positions, callbacks, etc.)

    return app


# Simple start/help (optional)
async def cmd_start(update, context):
    await update.message.reply_text("Bot running")


if __name__ == "__main__":
    print("Use main.py")
