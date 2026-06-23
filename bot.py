"""
bot.py - DEBUG VERSION
Extra logging for /itm and /itm r
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

# ... (all constants, helpers, _get_schwab_for_user, authorized_only, etc. remain the same as in the last working version)

# === Topics Integration ===
GROUP_CHAT_ID = -1003970147893
TOPIC_ITM = 3
TOPIC_ITM_R = 4
TOPIC_POSITIONS = 5


# ---------------------------------------------------------------------------
# Helper functions (unchanged)
# ---------------------------------------------------------------------------

def format_clean_precalc(hit: dict, trade_type: str = "itm") -> str:
    # same as previous
    pass


def get_ticker_positions(schwab_client, ticker: str):
    # same as previous
    pass


# ---------------------------------------------------------------------------
# Scanner commands with DEBUG logging
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_itm(update, context):
    logger.info(">>> DEBUG cmd_itm: command received")
    scanner     = context.application.bot_data["itm_scanner"]
    div_tickers = github_store.get_div_tickers()

    args         = [a.lower() for a in (context.args or [])]
    reverse_mode = "r" in args
    bc_mode      = "bc" in args

    logger.info(f">>> DEBUG cmd_itm: reverse_mode={reverse_mode}, bc_mode={bc_mode}")

    if bc_mode:
        tickers = github_store.get_latest_barchart_tickers()
        logger.info(f">>> DEBUG cmd_itm: using Barchart tickers ({len(tickers) if tickers else 0})")
        if not tickers:
            await update.message.reply_text("⚠️ No Barchart tickers available yet.")
            return
    else:
        tickers = github_store.get_tickers()
        logger.info(f">>> DEBUG cmd_itm: using tickers.txt ({len(tickers)} tickers)")

    if not tickers:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return

    scanner.ticker_freqs = div_tickers
    logger.info(f">>> DEBUG cmd_itm: starting _run_scan with {len(tickers)} tickers")

    if reverse_mode:
        logger.info(">>> DEBUG cmd_itm: switching to reverse scan mode")
        original_scan_ticker = scanner.scan_ticker
        scanner.scan_ticker  = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=tickers, hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original_scan_ticker
        logger.info(">>> DEBUG cmd_itm: reverse scan completed")
    else:
        logger.info(">>> DEBUG cmd_itm: normal ITM scan")
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=tickers, hits_with_buttons=True, scanner_key="itm")
        logger.info(">>> DEBUG cmd_itm: normal scan completed")


# (keep all other commands and functions as in your working version)

# The monitor_order and monitor_rtrade_order remain the same as the previous corrected version (topic posting on FILLED)
