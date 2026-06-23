"""
bot.py
Telegram bot — scanners + ITM/DCA trade execution with improve/cancel flow.
Heavy logging on trade flow for debugging.
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

SCAN_CONCURRENCY = 5
TG_MAX_LEN = 4000
_LAST_ERRORS: deque = deque(maxlen=30)

_PENDING_TRADES: dict = {}
PENDING_TIMEOUT_SEC = 60
ORDER_FILL_TIMEOUT_SEC = 10
_ACTIVE_ORDERS: dict = {}

MAX_TRADE_BUTTONS = 20

# === Topics Integration (Collars conversion group) ===
GROUP_CHAT_ID = -1003970147893
TOPIC_ITM = 3
TOPIC_ITM_R = 4
TOPIC_POSITIONS = 5


def _get_schwab_for_user(context, user_id: int):
    clients     = context.application.bot_data["schwab_clients"]
    primary_uid = context.application.bot_data["primary_user_id"]
    client      = clients.get(user_id) or clients.get(primary_uid)
    if client is None:
        raise RuntimeError(
            f"No Schwab client available for user {user_id} "
            f"and no primary fallback."
        )
    logger.info(
        f"_get_schwab_for_user: user={user_id} "
        f"using={'own' if user_id in clients else 'primary'} client"
    )
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
    except BadRequest as e:
        logger.warning(f"BadRequest with markdown: {e}")
    try:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        plain = _truncate(plain)
        if reply_markup:
            await send_callable(plain, reply_markup=reply_markup)
        else:
            await send_callable(plain)
    except BadRequest as e:
        logger.error(f"BadRequest plain text: {e}")


async def _edit_robust(message, text, reply_markup=None):
    safe = _truncate(text)
    try:
        if reply_markup is not None:
            await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        else:
            await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest as e:
        logger.warning(f"Edit BadRequest with markdown: {e}")
    try:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        plain = _truncate(plain)
        if reply_markup is not None:
            await message.edit_text(plain, reply_markup=reply_markup)
        else:
            await message.edit_text(plain)
    except BadRequest as e:
        logger.error(f"Edit BadRequest plain text: {e}")


# ---------------------------------------------------------------------------
# Helper: Clean precalc message for topics
# ---------------------------------------------------------------------------

def format_clean_precalc(hit: dict, trade_type: str = "itm") -> str:
    """Create a clean, readable precalc message for the topic."""
    ticker = hit.get("ticker", "?")
    spot = hit.get("spot", 0)
    strike = hit.get("strike", 0)
    exp = hit.get("exp_date", "?")
    dte = hit.get("dte", 0)
    apy = hit.get("locked_apy", 0)
    locked = hit.get("locked_total", 0)

    if trade_type == "itm_r":
        borrow = hit.get("borrow_cost", 0)
        return (
            f"✅ *ITM R Trade Filled*\n"
            f"Ticker: *{ticker}*\n"
            f"Precalc APY: *{apy}%*\n"
            f"Strike: ${strike:g} | Exp: {exp} ({dte}d)\n"
            f"Expected Locked: ${locked:.2f}\n"
            f"Borrow cost: ${borrow:.2f}\n"
            f"Spot at scan: ${spot}"
        )
    else:
        return (
            f"✅ *ITM Trade Filled*\n"
            f"Ticker: *{ticker}*\n"
            f"Precalc APY: *{apy}%*\n"
            f"Strike: ${strike:g} | Exp: {exp} ({dte}d)\n"
            f"Expected Locked Profit: ${locked:.2f}\n"
            f"Spot at scan: ${spot}"
        )


# ---------------------------------------------------------------------------
# Helper: Get positions for one specific ticker only
# ---------------------------------------------------------------------------

def get_ticker_positions(schwab_client, ticker: str):
    """Return positions projected P/L for a single ticker."""
    try:
        all_positions = compute_positions(schwab_client)
        for pos in all_positions:
            if pos.get("ticker", "").upper() == ticker.upper():
                return pos
        return None
    except Exception as e:
        logger.warning(f"get_ticker_positions failed for {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

async def cmd_start(update, context):
    await update.message.reply_text(
        "👋 *Options Scanner Bot*\n\n"
        "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm` `/itmib`\n"
        "`/positions` — P/L for positions expiring this Friday\n"
        "Send `/help` for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update, context):
    await update.message.reply_text(
        "*Commands*\n"
        "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm` `/itmib`\n"
        "`/positions` `/list` `/add` `/remove` `/logs` `/whoami`\n"
        "`/refresh_token` `/submit_token`\n\n"
        "*Trading:*\n"
        "· `/itm` — Confirm places order (auto-cancels after 10s if unfilled)\n"
        "· `/itm r` — Reverse ITM scan (auto-cancels after 10s if unfilled)\n"
        "· `/itmib` — Reverse ITM scan via IBKR\n"
        "· `/dca` — tap Confirm to place order instantly\n"
        "· `/positions` — projected P/L at expiry (this Friday)\n"
        "_Borrow cost (20% APR) deducted from /itm r APY._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_whoami(update, context):
    u = update.effective_user
    await update.message.reply_text(
        f"👤 `{u.id}`  —  {u.full_name}", parse_mode=ParseMode.MARKDOWN
    )


@authorized_only
async def cmd_list(update, context):
    tickers = github_store.get_tickers()
    if not tickers:
        await update.message.reply_text("_Empty._", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        f"*Watchlist ({len(tickers)})*\n" + ", ".join(f"`{t}`" for t in tickers),
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def cmd_add(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/add AAPL`", parse_mode=ParseMode.MARKDOWN)
        return
    lines = [github_store.add_ticker(t)[1] for t in context.args]
    await update.message.reply_text("\n".join(lines))


@authorized_only
async def cmd_remove(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/remove AAPL`", parse_mode=ParseMode.MARKDOWN)
        return
    lines = [github_store.remove_ticker(t)[1] for t in context.args]
    await update.message.reply_text("\n".join(lines))


@authorized_only
async def cmd_logs(update, context):
    if not _LAST_ERRORS:
        await update.message.reply_text("_No errors._", parse_mode=ParseMode.MARKDOWN)
        return
    body = "\n".join(_LAST_ERRORS)
    if len(body) > 3800:
        body = body[-3800:]
    await update.message.reply_text(
        "*Recent errors:*\n```\n" + body + "\n```",
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def cmd_positions(update, context):
    user_id = update.effective_user.id
    schwab  = _get_schwab_for_user(context, user_id)
    msg     = await update.message.reply_text("📊 Fetching positions…")
    loop    = asyncio.get_running_loop()
    try:
        positions = await loop.run_in_executor(None, compute_positions, schwab)
        if not positions:
            await _edit_robust(msg, "_No positions expiring this Friday._")
            return
        lines = ["*Positions expiring this Friday*\n"]
        for p in positions:
            lines.append(
                f"*{p['ticker']}* {p['qty']}× @ ${p['avg_price']:.2f} → "
                f"Est P/L: ${p['est_pl']:.2f} ({p['est_pl_pct']:.1f}%)"
            )
        await _edit_robust(msg, "\n".join(lines))
    except Exception as e:
        logger.exception("cmd_positions failed")
        await _edit_robust(msg, f"Error: {e}")


# ---------------------------------------------------------------------------
# Generic scan runner (unchanged)
# ---------------------------------------------------------------------------

async def _run_scan(update, context, scanner, emoji, format_summary_fn,
                    tickers_override=None, hits_with_buttons=False,
                    scanner_key=None, scan_kwargs=None, summary_kwargs=None):
    # ... (keep the full original _run_scan function here - it was not changed)
    pass


# ---------------------------------------------------------------------------
# Trade buttons (unchanged)
# ---------------------------------------------------------------------------

async def _send_itm_trade_button(update, context, hit):
    # ... (keep original)
    pass


async def _send_dca_trade_button(update, context, hit):
    # ... (keep original)
    pass


async def _send_rtrade_button(update, context, hit):
    # ... (keep original)
    pass


# ---------------------------------------------------------------------------
# Scanner commands (unchanged - including cmd_itm with only tickers.txt)
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_itm(update, context):
    # ... (keep the version that uses only get_tickers())
    pass


# ... (cmd_ritm, cmd_itmib, etc. - keep as before)


# ---------------------------------------------------------------------------
# Confirm callbacks (unchanged)
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_trade(update, context):
    # ... (keep original)
    pass


@authorized_callback
async def cb_cancel_trade(update, context):
    # ... (keep original)
    pass


@authorized_callback
async def cb_confirm_dca(update, context):
    # ... (keep original)
    pass


@authorized_callback
async def cb_cancel_dca(update, context):
    # ... (keep original)
    pass


@authorized_callback
async def cb_confirm_rtrade(update, context):
    # ... (keep original)
    pass


@authorized_callback
async def cb_cancel_rtrade(update, context):
    # ... (keep original)
    pass


# ---------------------------------------------------------------------------
# Order monitoring — Normal ITM (UPDATED)
# ---------------------------------------------------------------------------

async def monitor_order(context, user_id, order_id, status_msg):
    logger.info(f"monitor_order START: user={user_id} order={order_id}")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    start  = time.time()
    filled = False

    while time.time() - start < ORDER_FILL_TIMEOUT_SEC:
        await asyncio.sleep(5)
        try:
            status     = await loop.run_in_executor(None, schwab.get_order_status, order_id)
            status_str = status.get("status", "UNKNOWN")
            logger.info(f"monitor_order: order={order_id} status={status_str}")
            if status_str == "FILLED":
                filled = True
                break
            if status_str in ("CANCELED", "REJECTED", "EXPIRED"):
                active = _ACTIVE_ORDERS.pop(user_id, None)
                tkr    = active["hit"]["ticker"] if active else "?"
                await _edit_robust(status_msg, f"Order {order_id} for {tkr} ended: {status_str}")
                return
        except Exception as e:
            logger.warning(f"order status poll failed: {e}")
            continue

    active = _ACTIVE_ORDERS.pop(user_id, None)
    if filled and active:
        hit = active["hit"]
        ticker = hit.get("ticker", "?")

        # Post clean precalc to ITM topic
        precalc_text = format_clean_precalc(hit, trade_type="itm")
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=precalc_text,
                parse_mode=ParseMode.MARKDOWN,
                message_thread_id=TOPIC_ITM
            )
        except Exception as e:
            logger.warning(f"Failed to post precalc to ITM topic: {e}")

        # Post targeted positions to Positions topic
        pos = await loop.run_in_executor(None, get_ticker_positions, schwab, ticker)
        if pos:
            pos_text = (
                f"📊 *Positions Update - {ticker}*\n"
                f"Qty: {pos.get('qty', '?')} | Avg: ${pos.get('avg_price', 0):.2f}\n"
                f"Projected P/L: ${pos.get('est_pl', 0):.2f} ({pos.get('est_pl_pct', 0):.1f}%)"
            )
            try:
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=pos_text,
                    parse_mode=ParseMode.MARKDOWN,
                    message_thread_id=TOPIC_POSITIONS
                )
            except Exception as e:
                logger.warning(f"Failed to post positions: {e}")

        await _edit_robust(status_msg, f"FILLED — order {order_id} for {ticker}")
        return

    # Not filled → auto cancel
    if active:
        ticker = active["hit"]["ticker"]
        try:
            await loop.run_in_executor(None, schwab.cancel_order, order_id)
            await _edit_robust(status_msg,
                f"Order {order_id} · {ticker} not filled after {ORDER_FILL_TIMEOUT_SEC}s — auto-cancelled.")
            logger.info(f"monitor_order: auto-cancelled {order_id} for {ticker}")
        except Exception as e:
            logger.warning(f"monitor_order: auto-cancel failed: {e}")


# ---------------------------------------------------------------------------
# Order monitoring — Reverse ITM (UPDATED)
# ---------------------------------------------------------------------------

async def monitor_rtrade_order(context, user_id, order_id, status_msg, ticker):
    logger.info(f"monitor_rtrade_order START: user={user_id} order={order_id}")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    start  = time.time()
    filled = False

    while time.time() - start < ORDER_FILL_TIMEOUT_SEC:
        await asyncio.sleep(5)
        try:
            status     = await loop.run_in_executor(None, schwab.get_order_status, order_id)
            status_str = status.get("status", "UNKNOWN")
            logger.info(f"monitor_rtrade_order: id={order_id} status={status_str}")
            if status_str == "FILLED":
                filled = True
                break
            if status_str in ("CANCELED", "REJECTED", "EXPIRED"):
                _ACTIVE_ORDERS.pop(user_id, None)
                await _edit_robust(status_msg, f"Order {order_id} for {ticker} ended: {status_str}")
                return
        except Exception as e:
            logger.warning(f"monitor_rtrade_order: poll failed: {e}")
            continue

    _ACTIVE_ORDERS.pop(user_id, None)

    if filled:
        # Post clean precalc to ITM R topic
        precalc_text = format_clean_precalc({"ticker": ticker}, trade_type="itm_r")
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=precalc_text,
                parse_mode=ParseMode.MARKDOWN,
                message_thread_id=TOPIC_ITM_R
            )
        except Exception as e:
            logger.warning(f"Failed to post precalc to ITM R topic: {e}")

        # Targeted positions
        pos = await loop.run_in_executor(None, get_ticker_positions, schwab, ticker)
        if pos:
            pos_text = (
                f"📊 *Positions Update - {ticker}*\n"
                f"Qty: {pos.get('qty', '?')} | Avg: ${pos.get('avg_price', 0):.2f}\n"
                f"Projected P/L: ${pos.get('est_pl', 0):.2f} ({pos.get('est_pl_pct', 0):.1f}%)"
            )
            try:
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=pos_text,
                    parse_mode=ParseMode.MARKDOWN,
                    message_thread_id=TOPIC_POSITIONS
                )
            except Exception as e:
                logger.warning(f"Failed to post positions: {e}")

        await _edit_robust(status_msg, f"FILLED — order {order_id} for {ticker}")
        return

    # Not filled
    try:
        await loop.run_in_executor(None, schwab.cancel_order, order_id)
        await _edit_robust(status_msg,
            f"Order {order_id} · {ticker} not filled after {ORDER_FILL_TIMEOUT_SEC}s — auto-cancelled.")
        logger.info(f"monitor_rtrade_order: auto-cancelled {order_id} for {ticker}")
    except Exception as e:
        logger.warning(f"monitor_rtrade_order: auto-cancel failed: {e}")


# ---------------------------------------------------------------------------
# Improve / Cancel (unchanged)
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_improve(update, context):
    # ... (keep original)
    pass


@authorized_callback
async def cb_cancel(update, context):
    # ... (keep original)
    pass


# ---------------------------------------------------------------------------
# Wire everything up
# ---------------------------------------------------------------------------

def build_app(telegram_token, collar_scanner, spread_scanner, deepcall_scanner,
              dca_scanner, csp_scanner, itm_scanner, ritm_scanner,
              schwab_clients: dict, primary_user_id: int, itm_ibkr_scanner=None):
    app = Application.builder().token(telegram_token).build()
    app.bot_data["collar_scanner"]    = collar_scanner
    app.bot_data["spread_scanner"]    = spread_scanner
    app.bot_data["deepcall_scanner"]  = deepcall_scanner
    app.bot_data["dca_scanner"]       = dca_scanner
    app.bot_data["csp_scanner"]       = csp_scanner
    app.bot_data["itm_scanner"]       = itm_scanner
    app.bot_data["ritm_scanner"]      = ritm_scanner
    app.bot_data["schwab_clients"]    = schwab_clients
    app.bot_data["primary_user_id"]   = primary_user_id
    if itm_ibkr_scanner:
        app.bot_data["itm_ibkr_scanner"] = itm_ibkr_scanner

    # Add all handlers here (start, help, itm, confirm callbacks, etc.)
    # ... (keep the full original wiring from your previous working version)

    return app


# Token refresh commands (keep as before)
@authorized_only
async def cmd_refresh_token(update, context):
    # ... keep original
    pass


@authorized_only
async def cmd_submit_token(update, context):
    # ... keep original
    pass
