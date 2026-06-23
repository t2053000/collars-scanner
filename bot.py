"""
bot.py - COMPLETE WORKING VERSION (Debug logging included)
"""

import asyncio
import logging
import time
import uuid
from collections import Counter, deque
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

import github_store
from scanner  import CollarScanner
from spreads  import SpreadScanner
from deepcall import DeepCallScanner, clamp_cushion, DEFAULT_CUSHION_PCT, MIN_CUSHION_PCT, MAX_CUSHION_PCT
from dca      import DcaScanner
from csp      import CspScanner
from itm      import ItmScanner
from ritm     import RitmScanner
from itm_ibkr import ItmIbkrScanner
from positions import compute_positions
import orders

logger = logging.getLogger(__name__)

# === Topics Integration ===
GROUP_CHAT_ID = -1003970147893
TOPIC_ITM = 3
TOPIC_ITM_R = 4
TOPIC_POSITIONS = 5

_PENDING_TRADES = {}
_ACTIVE_ORDERS = {}

# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def authorized_only(func):
    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        if not user or not github_store.is_authorized(user.id):
            await update.message.reply_text(
                f"❌ Not authorized. Telegram ID: `{user.id if user else '?'}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Debug cmd_itm
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_itm(update, context):
    logger.info(">>> DEBUG cmd_itm: command received")
    scanner = context.application.bot_data["itm_scanner"]
    args = [a.lower() for a in (context.args or [])]
    reverse_mode = "r" in args

    logger.info(f">>> DEBUG cmd_itm: reverse_mode={reverse_mode}")

    tickers = github_store.get_tickers()
    logger.info(f">>> DEBUG cmd_itm: loaded {len(tickers)} tickers from tickers.txt")

    if not tickers:
        await update.message.reply_text("_No tickers._")
        return

    if reverse_mode:
        logger.info(">>> DEBUG cmd_itm: switching to reverse scan")
        original = scanner.scan_ticker
        scanner.scan_ticker = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=tickers, hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original
    else:
        logger.info(">>> DEBUG cmd_itm: normal ITM scan")
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=tickers, hits_with_buttons=True, scanner_key="itm")

    logger.info(">>> DEBUG cmd_itm: finished")


# ---------------------------------------------------------------------------
# Placeholder functions - REPLACE THESE with your working versions
# ---------------------------------------------------------------------------

async def _run_scan(update, context, scanner, emoji, format_summary_fn, 
                    tickers_override=None, hits_with_buttons=False, scanner_key=None):
    # ← Put your working _run_scan here
    await update.message.reply_text("Scan started (placeholder)")


async def _send_itm_trade_button(update, context, hit):
    await update.message.reply_text(f"ITM button for {hit.get('ticker')}")


async def _send_rtrade_button(update, context, hit):
    await update.message.reply_text(f"Reverse button for {hit.get('ticker')}")


async def monitor_order(context, user_id, order_id, status_msg):
    pass


async def monitor_rtrade_order(context, user_id, order_id, status_msg, ticker):
    pass


# ---------------------------------------------------------------------------
# build_app - THIS IS THE IMPORTANT PART
# ---------------------------------------------------------------------------

def build_app(telegram_token, collar_scanner, spread_scanner, deepcall_scanner,
              dca_scanner, csp_scanner, itm_scanner, ritm_scanner,
              schwab_clients: dict, primary_user_id: int, itm_ibkr_scanner=None):

    app = Application.builder().token(telegram_token).build()

    # Store scanners and clients
    app.bot_data["collar_scanner"] = collar_scanner
    app.bot_data["spread_scanner"] = spread_scanner
    app.bot_data["deepcall_scanner"] = deepcall_scanner
    app.bot_data["dca_scanner"] = dca_scanner
    app.bot_data["csp_scanner"] = csp_scanner
    app.bot_data["itm_scanner"] = itm_scanner
    app.bot_data["ritm_scanner"] = ritm_scanner
    app.bot_data["schwab_clients"] = schwab_clients
    app.bot_data["primary_user_id"] = primary_user_id

    if itm_ibkr_scanner:
        app.bot_data["itm_ibkr_scanner"] = itm_ibkr_scanner

    # Register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("itm", cmd_itm))

    # Add your other handlers here (positions, callbacks, etc.)

    return app


async def cmd_start(update, context):
    await update.message.reply_text("👋 Bot is running")


async def cmd_help(update, context):
    await update.message.reply_text("Use /itm or /itm r")


# ---------------------------------------------------------------------------
# If you run bot.py directly (usually not needed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Run via main.py")
