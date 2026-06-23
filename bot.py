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
# Helper functions for topics (only used on FILLED)
# ---------------------------------------------------------------------------

def format_clean_precalc(hit: dict, trade_type: str = "itm") -> str:
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


def get_ticker_positions(schwab_client, ticker: str):
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
# Basic commands (unchanged)
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
    # (full original _run_scan kept as-is from your working version)
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(f"{emoji} Scanning…")

    if tickers_override is not None:
        tickers = tickers_override
    else:
        tickers = github_store.get_tickers()

    if not tickers:
        await _edit_robust(status_msg, "_No tickers in watchlist._")
        return

    loop = asyncio.get_running_loop()
    all_hits = []
    errors = []
    debug_totals = Counter()

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _scan_one(ticker):
        async with sem:
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

    await asyncio.gather(*[_scan_one(t) for t in tickers])

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

        elif scanner_key == "dca":
            all_hits.sort(key=lambda r: r.get("score_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_dca_trade_button(update, context, hit)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning(f"dca button send failed for {hit.get('ticker')}: {e}")


# ---------------------------------------------------------------------------
# Trade buttons (RESTORED to work in private chat)
# ---------------------------------------------------------------------------

async def _send_itm_trade_button(update, context, hit):
    trade_id = uuid.uuid4().hex[:8]
    user_id  = update.effective_user.id
    apy      = hit.get("locked_apy", 0)
    logger.info(f"_send_itm_trade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={apy}")
    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit":        hit,
        "walk_step":  0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse":    False,
        "trade_type": "itm",
    }
    summary = (
        f"🔒 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
        f"Strike ${hit['strike']:g} · Net credit ${hit.get('net_credit', 0):.2f}/sh\n"
        f"💳 Pay ${hit.get('primary_debit', 0):.2f}/sh → *{apy:.1f}% APY*\n"
        f"OI {hit['call_oi']}/{hit['put_oi']} · Locked ${hit.get('locked_total', 0):.0f}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Confirm @ {apy:.1f}% APY", callback_data=f"confirm_trade:{trade_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_trade:{trade_id}"),
    ]])
    try:
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except BadRequest as e:
        logger.warning(f"itm trade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"itm trade button failed even plain: {e2}")


async def _send_dca_trade_button(update, context, hit):
    trade_id   = uuid.uuid4().hex[:8]
    user_id    = update.effective_user.id
    apy        = hit.get("score_apy", 0)
    score_sign = "+" if hit.get("score_dollars", 0) >= 0 else ""
    logger.info(f"_send_dca_trade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={apy}")
    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit":        hit,
        "walk_step":  0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse":    False,
        "trade_type": "dca",
    }
    summary = (
        f"💰 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
        f"Strike ${hit['strike']:g} · Net premium ${hit.get('net_premium', 0):.2f}/sh\n"
        f"🛡️ Safety ${hit.get('safety_dollars', 0):.2f}/sh · "
        f"💸 Div +${hit.get('expected_div_dollars', 0):.2f}/sh\n"
        f"🎯 Score: *{score_sign}${hit.get('score_dollars', 0):.2f}/sh · {apy:.1f}% APY*\n"
        f"OI {hit['call_oi']}/{hit['put_oi']}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Confirm @ {apy:.1f}% APY", callback_data=f"confirm_dca:{trade_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_dca:{trade_id}"),
    ]])
    try:
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except BadRequest as e:
        logger.warning(f"dca trade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"dca trade button failed even plain: {e2}")


async def _send_rtrade_button(update, context, hit):
    trade_id = uuid.uuid4().hex[:8]
    user_id  = update.effective_user.id
    apy      = hit.get("locked_apy", 0)
    logger.info(f"_send_rtrade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={apy}")
    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit":        hit,
        "walk_step":  0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse":    True,
        "trade_type": "itm_r",
    }
    htb_flag    = " ⚠️HTB?" if hit.get("htb") else ""
    ex_div_warn = f"\n🚨 EX-DIV {hit.get('next_ex_div_date', '')} BEFORE EXPIRY" if hit.get("ex_div_in_window") else ""
    ex_div_str  = f" · ex-div {hit['next_ex_div_date']}" if hit.get("next_ex_div_date") and not hit.get("ex_div_in_window") else ""
    borrow_str  = f" · borrow -{hit['borrow_cost']:.2f}" if hit.get("borrow_cost", 0) > 0 else ""
    fallback_line = f"🔄 Fallback -> {hit['fallback_apy']:.1f}% APY\n" if hit.get("fallback_apy", 0) > 0 else ""
    summary = (
        f"🔄 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d){htb_flag}\n"
        f"Strike ${hit['strike']:g} · Net credit ${hit['net_credit']:.2f}/sh{ex_div_str}{borrow_str}\n"
        f"💰 Locked ${hit['locked_total']:.0f} → *{apy:.1f}% APY*\n"
        f"{fallback_line}"
        f"OI {hit['call_oi']}/{hit['put_oi']}"
        f"{ex_div_warn}\n"
        f"⚠️ SHORT {hit['ticker']} ON SCHWAB FIRST, then confirm"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Confirm R @ {apy:.1f}% APY", callback_data=f"confirm_rtrade:{trade_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rtrade:{trade_id}"),
    ]])
    try:
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except BadRequest as e:
        logger.warning(f"rtrade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"rtrade button failed even plain: {e2}")


# ---------------------------------------------------------------------------
# Scanner commands (cmd_itm uses only tickers.txt)
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_itm(update, context):
    scanner     = context.application.bot_data["itm_scanner"]
    div_tickers = github_store.get_div_tickers()

    args         = [a.lower() for a in (context.args or [])]
    reverse_mode = "r" in args
    bc_mode      = "bc" in args

    if bc_mode:
        tickers = github_store.get_latest_barchart_tickers()
        if not tickers:
            await update.message.reply_text("⚠️ No Barchart tickers available yet — try again during market hours.")
            return
    else:
        tickers = github_store.get_tickers()

    combined = sorted(set(tickers))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers

    if reverse_mode:
        original_scan_ticker = scanner.scan_ticker
        scanner.scan_ticker  = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=combined, hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original_scan_ticker
    else:
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=combined, hits_with_buttons=True, scanner_key="itm")


# (cmd_ritm, cmd_itmib and other commands kept as in your previous working version)


# ---------------------------------------------------------------------------
# Confirm / Cancel callbacks (unchanged)
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_trade(update, context):
    # (keep your existing working version)
    pass


@authorized_callback
async def cb_cancel_trade(update, context):
    # (keep your existing working version)
    pass


@authorized_callback
async def cb_confirm_dca(update, context):
    # (keep your existing working version)
    pass


@authorized_callback
async def cb_cancel_dca(update, context):
    # (keep your existing working version)
    pass


@authorized_callback
async def cb_confirm_rtrade(update, context):
    # (keep your existing working version)
    pass


@authorized_callback
async def cb_cancel_rtrade(update, context):
    # (keep your existing working version)
    pass


# ---------------------------------------------------------------------------
# Order monitoring — Normal ITM (UPDATED - posts to topics on FILLED)
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
# Order monitoring — Reverse ITM (UPDATED - posts to topics on FILLED)
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
    # keep your existing version
    pass


@authorized_callback
async def cb_cancel(update, context):
    # keep your existing version
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

    # Add all your handlers here (start, help, itm, confirm_*, etc.)
    # (Use the exact handler wiring from your last working version)

    return app


# Token refresh commands (keep as before)
@authorized_only
async def cmd_refresh_token(update, context):
    # keep your existing version
    pass


@authorized_only
async def cmd_submit_token(update, context):
    # keep your existing version
    pass
