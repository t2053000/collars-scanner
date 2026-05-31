"""
bot.py — Telegram handlers and trade flow.
Defensive imports against unknown github_store API.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from schwab_client import SchwabClient
import scanner
import spreads as spreads_mod
import deepcall
import dca
import csp
import itm
import ritm
import orders

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


# ---------- Defensive github_store wrapper ----------
import github_store as _gs

def _gs_call(names, *args, default=None):
    """Try a list of possible function names on github_store; return first that works."""
    for name in names:
        fn = getattr(_gs, name, None)
        if callable(fn):
            try:
                return fn(*args)
            except Exception as e:
                logger.warning(f"github_store.{name}{args} failed: {e}")
                continue
    logger.warning(f"no working github_store fn found among {names}; using default")
    return default


def _load_tickers():
    return _gs_call(
        ["load_tickers", "read_tickers", "get_tickers", "list_tickers", "load_file", "read_file"],
        "tickers.txt",
        default=[]
    ) or []


def _load_whitelist():
    return _gs_call(
        ["load_whitelist", "read_whitelist", "get_whitelist", "load_file", "read_file"],
        "whitelist.txt",
        default=["108893493", "396567390"]
    ) or ["108893493", "396567390"]


def _save_tickers(tickers):
    return _gs_call(
        ["save_tickers", "write_tickers", "write_file", "save_file"],
        "tickers.txt", tickers,
        default=None
    )


def _add_ticker_helper(t):
    fn = getattr(_gs, "add_ticker", None)
    if callable(fn):
        try:
            return fn("tickers.txt", t)
        except Exception as e:
            logger.warning(f"github_store.add_ticker failed: {e}")
    # Fallback: load, append, save
    tickers = _load_tickers()
    if t not in tickers:
        tickers.append(t)
        _save_tickers(tickers)
    return tickers


def _remove_ticker_helper(t):
    fn = getattr(_gs, "remove_ticker", None)
    if callable(fn):
        try:
            return fn("tickers.txt", t)
        except Exception as e:
            logger.warning(f"github_store.remove_ticker failed: {e}")
    tickers = _load_tickers()
    tickers = [x for x in tickers if x != t]
    _save_tickers(tickers)
    return tickers


WHITELIST = _load_whitelist()
logger.info(f"bot.py loaded whitelist: {WHITELIST}")

MAX_TRADE_BUTTONS = 20
TRADE_BUTTON_DELAY = 0.5
TRADE_CONFIRM_TIMEOUT = 60
MONITOR_POLL_SEC = 5
MONITOR_TIMEOUT_SEC = 30

pending_itm_trades = {}
pending_ritm_trades = {}


def _is_whitelisted(user_id):
    return str(user_id) in [str(x) for x in WHITELIST] or user_id in WHITELIST


def _gen_trade_id():
    return str(int(time.time() * 1000))[-8:]


# =========================================================================
# BASIC COMMANDS
# =========================================================================

async def cmd_start(update, ctx):
    await update.message.reply_text("👋 Alpha bot ready. /help for commands.")


async def cmd_help(update, ctx):
    await update.message.reply_text(
        "Commands:\n"
        "/scan  /spreads [T]  /deepcall [N]  /dca  /csp\n"
        "/itm  (with trade buttons)\n"
        "/ritm (parked trades for manual review)\n"
        "/list  /add  /remove  /whoami\n"
        "/refresh_token  /submit_token  /logs"
    )


async def cmd_whoami(update, ctx):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(f"ID: `{uid}`\nName: {name}", parse_mode="Markdown")


async def cmd_list(update, ctx):
    tickers = _load_tickers()
    await update.message.reply_text("Tickers: " + ", ".join(tickers) if tickers else "(none loaded)")


async def cmd_add(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /add TICKER")
        return
    t = ctx.args[0].upper()
    _add_ticker_helper(t)
    await update.message.reply_text(f"Added {t}")


async def cmd_remove(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /remove TICKER")
        return
    t = ctx.args[0].upper()
    _remove_ticker_helper(t)
    await update.message.reply_text(f"Removed {t}")


async def cmd_logs(update, ctx):
    await update.message.reply_text("Logs are on Railway dashboard.")


async def cmd_refresh_token(update, ctx):
    await update.message.reply_text("Use desktop refresh flow.")


async def cmd_submit_token(update, ctx):
    await update.message.reply_text("Use desktop refresh flow.")


# =========================================================================
# SCANNER COMMANDS (passthrough)
# =========================================================================

async def cmd_scan(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running collar scan…")
    try:
        msg = scanner.run_scan()
        await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ scan error: {e}")


async def cmd_spreads(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    ticker = ctx.args[0].upper() if ctx.args else None
    await update.message.reply_text("Running spreads scan…")
    try:
        msg = spreads_mod.run_spreads(ticker)
        await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ spreads error: {e}")


async def cmd_deepcall(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    n = int(ctx.args[0]) if ctx.args else 5
    await update.message.reply_text(f"Running deepcall scan (top {n})…")
    try:
        msg = deepcall.run_deepcall(n)
        await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ deepcall error: {e}")


async def cmd_dca(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running dca scan…")
    try:
        msg = dca.run_dca()
        await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ dca error: {e}")


async def cmd_csp(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running csp scan…")
    try:
        msg = csp.run_csp()
        await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ csp error: {e}")


# =========================================================================
# /itm WITH TRADE BUTTONS
# =========================================================================

async def cmd_itm(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    await update.message.reply_text("Running /itm scan…")
    try:
        hits = itm.scan_itm()
    except Exception as e:
        await update.message.reply_text(f"❌ /itm error: {e}")
        return
    if not hits:
        await update.message.reply_text("No /itm hits.")
        return

    await update.message.reply_text(
        f"📊 /itm found {len(hits)} hits (showing top {min(MAX_TRADE_BUTTONS, len(hits))} as buttons)."
    )
    pending_itm_trades.setdefault(uid, {})
    expires_at = time.time() + 600

    for idx, hit in enumerate(hits[-MAX_TRADE_BUTTONS:], start=1):
        trade_id = _gen_trade_id()
        pending_itm_trades[uid][trade_id] = (hit, 0, expires_at)
        text = itm.format_itm_hit(hit, idx, min(MAX_TRADE_BUTTONS, len(hits)))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💼 Trade @ {hit['locked_apy']:.1f}% APY",
                                 callback_data=f"trade:{trade_id}")
        ]])
        try:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"/itm button send failed for {hit['ticker']}: {e}")
        await asyncio.sleep(TRADE_BUTTON_DELAY)


async def cb_trade(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    logger.info(f"cb_trade FIRED user={uid} data={q.data}")
    await q.answer()

    trade_id = q.data.split(":", 1)[1]
    user_pending = pending_itm_trades.get(uid, {})
    if trade_id not in user_pending:
        await ctx.bot.send_message(uid, "⏱ Trade expired or unknown. Re-run /itm.")
        return

    hit, walk_step, _ = user_pending[trade_id]
    pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    next_pricing = orders.compute_legs_pricing(hit, walk_step=walk_step + 1)
    user_pending[trade_id] = (hit, walk_step, time.time() + TRADE_CONFIRM_TIMEOUT)

    preview = orders.format_order_preview(hit, pricing, next_pricing)
    await ctx.bot.send_message(uid, preview, parse_mode="Markdown")


# =========================================================================
# /ritm WITH PARKED TRADE BUTTONS
# =========================================================================

async def cmd_ritm(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    await update.message.reply_text("Running /ritm scan (borrow 25%)…")
    try:
        hits = ritm.scan_ritm()
    except Exception as e:
        await update.message.reply_text(f"❌ /ritm error: {e}")
        return
    if not hits:
        await update.message.reply_text("No /ritm hits.")
        return

    await update.message.reply_text(
        f"📊 /ritm found {len(hits)} hits.\n"
        f"⚠️ Orders parked $0.50 below fair — will NOT fill until you edit price on Schwab."
    )

    pending_ritm_trades.setdefault(uid, {})
    expires_at = time.time() + 600

    for idx, hit in enumerate(hits[-MAX_TRADE_BUTTONS:], start=1):
        trade_id = _gen_trade_id()
        pending_ritm_trades[uid][trade_id] = (hit, expires_at)
        text = ritm.format_ritm_hit(hit, idx, min(MAX_TRADE_BUTTONS, len(hits)))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🅿️ Park @ {hit['locked_apy']:.1f}% APY",
                                 callback_data=f"rtrade:{trade_id}")
        ]])
        try:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"/ritm button send failed for {hit['ticker']}: {e}")
        await asyncio.sleep(TRADE_BUTTON_DELAY)


async def cb_rtrade(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    logger.info(f"cb_rtrade FIRED user={uid} data={q.data}")
    await q.answer()

    trade_id = q.data.split(":", 1)[1]
    user_pending = pending_ritm_trades.get(uid, {})
    if trade_id not in user_pending:
        await ctx.bot.send_message(uid, "⏱ Trade expired or unknown. Re-run /ritm.")
        return

    hit, _ = user_pending[trade_id]
    pricing = orders.compute_ritm_pricing(hit)
    user_pending[trade_id] = (hit, time.time() + TRADE_CONFIRM_TIMEOUT)

    preview = orders.format_ritm_preview(hit, pricing)
    await ctx.bot.send_message(uid, preview, parse_mode="Markdown")


# =========================================================================
# YES HANDLER (both /itm and /ritm)
# =========================================================================

async def handle_yes_reply(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    text = update.message.text.strip().upper()
    logger.info(f"handle_yes_reply: text='{text}' user={uid}")

    if not text.startswith("YES "):
        return
    parts = text.split()
    if len(parts) < 2:
        return
    ticker_typed = parts[1]
    now = time.time()

    ritm_pending = pending_ritm_trades.get(uid, {})
    for trade_id, val in list(ritm_pending.items()):
        hit, exp = val
        if hit["ticker"].upper() == ticker_typed and exp > now:
            logger.info(f"handle_yes_reply: matched RITM {trade_id}")
            await _submit_ritm_parked(update, ctx, hit)
            del ritm_pending[trade_id]
            return

    itm_pending = pending_itm_trades.get(uid, {})
    for trade_id, val in list(itm_pending.items()):
        hit, walk_step, exp = val
        if hit["ticker"].upper() == ticker_typed and exp > now:
            logger.info(f"handle_yes_reply: matched ITM {trade_id}")
            await _submit_itm(update, ctx, hit, walk_step)
            del itm_pending[trade_id]
            return

    logger.info(f"handle_yes_reply: no match for {ticker_typed}")


async def _submit_itm(update, ctx, hit, walk_step):
    await update.message.reply_text("📤 Submitting /itm order…")
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
        payload = orders.build_itm_conversion_order(hit, pricing)
        client = SchwabClient()
        order_id = client.place_order(payload)
        net_debit = pricing["stock_price"] - pricing["call_limit"] + pricing["put_limit"]
        await update.message.reply_text(
            f"✅ Order #{order_id}\n{hit['ticker']} ITM @ NET_DEBIT ${net_debit:.2f}\nMonitoring 30s…"
        )
        await _monitor_order(update, ctx, order_id, hit, pricing)
    except Exception as e:
        logger.error(f"_submit_itm failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Order failed: {e}")


async def _submit_ritm_parked(update, ctx, hit):
    await update.message.reply_text("📤 Submitting /ritm PARKED order…")
    try:
        pricing = orders.compute_ritm_pricing(hit)
        payload = orders.build_ritm_conversion_order(hit, pricing)
        client = SchwabClient()
        order_id = client.place_order(payload)
        await update.message.reply_text(
            f"🅿️ *PARKED* #{order_id}\n"
            f"{hit['ticker']} RITM strike ${hit['strike']:g} exp {hit['exp_date']} ({hit['dte']}d)\n"
            f"Parked @ ${pricing['parked_net_credit_per_share']:.2f}/sh "
            f"(fair ${pricing['fair_net_credit_per_share']:.2f})\n\n"
            f"⚠️ UNFILLABLE. Edit price up on Schwab to fill, or cancel.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"_submit_ritm_parked failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Parked order failed: {e}")


async def _monitor_order(update, ctx, order_id, hit, pricing):
    client = SchwabClient()
    elapsed = 0
    while elapsed < MONITOR_TIMEOUT_SEC:
        await asyncio.sleep(MONITOR_POLL_SEC)
        elapsed += MONITOR_POLL_SEC
        try:
            status = client.get_order_status(order_id)
            if status == "FILLED":
                await update.message.reply_text(f"✅ #{order_id} FILLED")
                return
            if status in ("CANCELED", "REJECTED", "EXPIRED"):
                await update.message.reply_text(f"⚠️ #{order_id} {status}")
                return
        except Exception as e:
            logger.warning(f"monitor failed: {e}")

    next_pricing = orders.compute_legs_pricing(hit, walk_step=pricing["walk_step"] + 1)
    rows = []
    if orders.can_improve(next_pricing):
        rows.append([InlineKeyboardButton(
            f"🔧 Improve → {next_pricing['apy']:.1f}% APY",
            callback_data=f"improve:{order_id}"
        )])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{order_id}")])
    await update.message.reply_text(
        f"⏳ #{order_id} still WORKING after 30s",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_improve(update, ctx):
    q = update.callback_query
    await q.answer()
    await ctx.bot.send_message(q.from_user.id,
        "🔧 Improve flow: cancel manually on Schwab and re-run /itm.")


async def cb_cancel(update, ctx):
    q = update.callback_query
    await q.answer()
    order_id = q.data.split(":", 1)[1]
    try:
        client = SchwabClient()
        client.cancel_order(order_id)
        await ctx.bot.send_message(q.from_user.id, f"❌ Cancel sent #{order_id}")
    except Exception as e:
        await ctx.bot.send_message(q.from_user.id, f"❌ Cancel failed: {e}")


# =========================================================================
# APP BUILD
# =========================================================================

def build_app(token):
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("refresh_token", cmd_refresh_token))
    app.add_handler(CommandHandler("submit_token", cmd_submit_token))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("spreads", cmd_spreads))
    app.add_handler(CommandHandler("deepcall", cmd_deepcall))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("csp", cmd_csp))
    app.add_handler(CommandHandler("itm", cmd_itm))
    app.add_handler(CommandHandler("ritm", cmd_ritm))
    app.add_handler(CallbackQueryHandler(cb_trade, pattern=r"^trade:"))
    app.add_handler(CallbackQueryHandler(cb_rtrade, pattern=r"^rtrade:"))
    app.add_handler(CallbackQueryHandler(cb_improve, pattern=r"^improve:"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_yes_reply))
    return app
