"""
bot.py - Fixed version (only syntax fix in _run_scan)
"""

import asyncio
import logging
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

SCAN_CONCURRENCY = 5
TG_MAX_LEN = 4000
_LAST_ERRORS: deque = deque(maxlen=30)

_PENDING_TRADES: dict = {}
PENDING_TIMEOUT_SEC = 60
ORDER_FILL_TIMEOUT_SEC = 10
_ACTIVE_ORDERS: dict = {}

MAX_TRADE_BUTTONS = 20

# === Topics Integration ===
GROUP_CHAT_ID = -1003970147893
TOPIC_ITM = 3
TOPIC_ITM_R = 4
TOPIC_POSITIONS = 5


def _get_schwab_for_user(context, user_id: int):
    clients     = context.application.bot_data["schwab_clients"]
    primary_uid = context.application.bot_data["primary_user_id"]
    client      = clients.get(user_id) or clients.get(primary_uid)
    if client is None:
        raise RuntimeError(f"No Schwab client available for user {user_id}")
    return client


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


def authorized_callback(func):
    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        if not user or not github_store.is_authorized(user.id):
            await update.callback_query.answer("Not authorized.")
            return
        return await func(update, context)
    return wrapper


def _truncate(text, limit=TG_MAX_LEN):
    if len(text) <= limit:
        return text
    return text[:limit - 30] + "\n\n_(truncated…)_"


async def _send_robust(send_callable, text, reply_markup=None):
    safe = _truncate(text)
    try:
        if reply_markup:
            await send_callable(safe, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        else:
            await send_callable(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        if reply_markup:
            await send_callable(plain, reply_markup=reply_markup)
        else:
            await send_callable(plain)


async def _edit_robust(message, text, reply_markup=None):
    safe = _truncate(text)
    try:
        if reply_markup is not None:
            await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        else:
            await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        if reply_markup is not None:
            await message.edit_text(plain, reply_markup=reply_markup)
        else:
            await message.edit_text(plain)


# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------

def format_clean_precalc(hit: dict, trade_type: str = "itm") -> str:
    ticker = hit.get("ticker", "?")
    apy = hit.get("locked_apy", 0)
    if trade_type == "itm_r":
        return f"✅ *ITM R Trade Filled*\nTicker: *{ticker}*\nPrecalc APY: *{apy}%*"
    return f"✅ *ITM Trade Filled*\nTicker: *{ticker}*\nPrecalc APY: *{apy}%*"


def get_ticker_positions(schwab_client, ticker: str):
    try:
        all_pos = compute_positions(schwab_client)
        for p in all_pos:
            if p.get("ticker", "").upper() == ticker.upper():
                return p
        return None
    except Exception as e:
        logger.warning(f"get_ticker_positions failed: {e}")
        return None


# ---------------------------------------------------------------------------
# cmd_itm - only tickers.txt
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
    combined = sorted(set(tickers))

    if reverse_mode:
        logger.info(">>> DEBUG cmd_itm: switching to reverse scan mode")
        original_scan_ticker = scanner.scan_ticker
        scanner.scan_ticker  = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=combined, hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original_scan_ticker
        logger.info(">>> DEBUG cmd_itm: reverse scan finished")
    else:
        logger.info(">>> DEBUG cmd_itm: normal ITM scan")
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=combined, hits_with_buttons=True, scanner_key="itm")
        logger.info(">>> DEBUG cmd_itm: normal scan finished")


# ---------------------------------------------------------------------------
# _run_scan - FIXED (only the try/except structure around gather)
# ---------------------------------------------------------------------------

async def _run_scan(update, context, scanner, emoji, format_summary_fn,
                    tickers_override=None, hits_with_buttons=False,
                    scanner_key=None, scan_kwargs=None, summary_kwargs=None):

    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(f"{emoji} Scanning…")

    tickers = tickers_override if tickers_override is not None else github_store.get_tickers()
    if not tickers:
        await _edit_robust(status_msg, "_No tickers._")
        return

    loop = asyncio.get_running_loop()
    all_hits = []
    errors = []
    debug_totals = Counter()

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _scan_one(ticker):
        async with sem:
            # Defensive guard
            if isinstance(ticker, (list, tuple)):
                ticker = ticker[0] if ticker else None
            if not ticker:
                return

            try:
                if scan_kwargs:
                    hits, debug = await loop.run_in_executor(
                        None, lambda: scanner.scan_ticker(ticker, **scan_kwargs)
                    )
                else:
                    hits, debug = await loop.run_in_executor(
                        None, scanner.scan_ticker, ticker
                    )
                all_hits.extend(hits)
                for k, v in debug.items():
                    debug_totals[k] += v
            except Exception as e:
                errors.append(f"{ticker}: {type(e).__name__}: {e}")
                logger.exception(f"scan failed for {ticker}")

    # === FIXED try/except structure ===
    try:
        await asyncio.gather(*[_scan_one(t) for t in tickers])
    except Exception as e:
        logger.error(f"gather failed: {e}")
        errors.append(f"gather error: {str(e)}")

    try:
        messages = format_summary_fn(
            all_hits=all_hits,
            scanned=len(tickers),
            successful=len(tickers) - len(errors),
            errors=errors,
            debug_totals=debug_totals if debug_totals else None,
            **(summary_kwargs or {})
        )
    except TypeError:
        for k in list(summary_kwargs or {}):
            summary_kwargs.pop(k, None)
        messages = format_summary_fn(
            all_hits=all_hits,
            scanned=len(tickers),
            successful=len(tickers) - len(errors),
            errors=errors,
            debug_totals=debug_totals if debug_totals else None,
        )

    await _edit_robust(status_msg, messages[0])
    for extra in messages[1:]:
        await _send_robust(update.message.reply_text, extra)

    if hits_with_buttons and all_hits:
        if scanner_key == "itm":
            all_hits.sort(key=lambda r: r.get("locked_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_itm_trade_button(update, context, hit)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning(f"trade button send failed for {hit.get('ticker')}: {e}")

        elif scanner_key == "itm_r":
            all_hits.sort(key=lambda r: r.get("locked_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_rtrade_button(update, context, hit)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning(f"rtrade button send failed for {hit.get('ticker')}: {e}")


# ---------------------------------------------------------------------------
# Placeholder button functions (replace with your real ones if needed)
# ---------------------------------------------------------------------------

async def _send_itm_trade_button(update, context, hit):
    await update.message.reply_text(f"ITM hit: {hit.get('ticker')}")


async def _send_rtrade_button(update, context, hit):
    await update.message.reply_text(f"Reverse hit: {hit.get('ticker')}")


# ---------------------------------------------------------------------------
# Monitor functions (keep your existing logic)
# ---------------------------------------------------------------------------

async def monitor_order(context, user_id, order_id, status_msg):
    pass


async def monitor_rtrade_order(context, user_id, order_id, status_msg, ticker):
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

    if itm_ibkr_scanner:
        app.bot_data["itm_ibkr_scanner"] = itm_ibkr_scanner

    app.add_handler(CommandHandler("itm", cmd_itm))

    return app


async def cmd_start(update, context):
    await update.message.reply_text("Bot running")


async def cmd_help(update, context):
    await update.message.reply_text("Use /itm or /itm r")


if __name__ == "__main__":
    print("Use main.py")
