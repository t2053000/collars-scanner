"""
bot.py — Telegram handlers and trade-flow orchestration.

Implements: /start /help /whoami /list /add /remove /logs /refresh_token /submit_token
            /scan /spreads /deepcall /dca /csp /itm (with trade buttons) /ritm (with parked-trade buttons)
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
from github_store import load_tickers, save_tickers, add_ticker, remove_ticker, load_whitelist
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

WHITELIST = load_whitelist("whitelist.txt")
MAX_TRADE_BUTTONS = 20
TRADE_BUTTON_DELAY = 0.5
TRADE_CONFIRM_TIMEOUT = 60
MONITOR_POLL_SEC = 5
MONITOR_TIMEOUT_SEC = 30

# pending_itm_trades[user_id][trade_id] = (hit, walk_step, expires_at)
pending_itm_trades = {}
# pending_ritm_trades[user_id][trade_id] = (hit, expires_at)
pending_ritm_trades = {}


def _is_whitelisted(user_id):
    return str(user_id) in WHITELIST or user_id in WHITELIST


def _gen_trade_id():
    return str(int(time.time() * 1000))[-8:]


# =========================================================================
# BASIC COMMANDS
# =========================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Alpha bot ready.\nTry /help for commands."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/scan — collar scanner\n"
        "/spreads [TICKER] — vertical spread scanner\n"
        "/deepcall [N] — deep-ITM buy-write\n"
        "/dca — dividend collar arbitrage\n"
        "/csp — OTM bull put credit spread\n"
        "/itm — ITM conversion scanner (with trade buttons)\n"
        "/ritm — reverse ITM conversion (parked trades for manual review)\n"
        "/list /add /remove — manage tickers\n"
        "/whoami — show your Telegram ID\n"
        "/refresh_token /submit_token — Schwab OAuth\n"
        "/logs — recent log lines"
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(f"User ID: `{uid}`\nName: {name}", parse_mode="Markdown")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tickers = load_tickers("tickers.txt")
    await update.message.reply_text("Tickers: " + ", ".join(tickers))


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /add TICKER")
        return
    t = ctx.args[0].upper()
    add_ticker("tickers.txt", t)
    await update.message.reply_text(f"Added {t}")


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /remove TICKER")
        return
    t = ctx.args[0].upper()
    remove_ticker("tickers.txt", t)
    await update.message.reply_text(f"Removed {t}")


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Logs are on Railway — open the dashboard.")


# =========================================================================
# SCANNER COMMANDS (passthrough)
# =========================================================================

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running collar scan…")
    msg = scanner.run_scan()
    await update.message.reply_text(msg[:4000])


async def cmd_spreads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    ticker = ctx.args[0].upper() if ctx.args else None
    await update.message.reply_text("Running spreads scan…")
    msg = spreads_mod.run_spreads(ticker)
    await update.message.reply_text(msg[:4000])


async def cmd_deepcall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    n = int(ctx.args[0]) if ctx.args else 5
    await update.message.reply_text(f"Running deepcall scan (top {n})…")
    msg = deepcall.run_deepcall(n)
    await update.message.reply_text(msg[:4000])


async def cmd_dca(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running dca scan…")
    msg = dca.run_dca()
    await update.message.reply_text(msg[:4000])


async def cmd_csp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running csp scan…")
    msg = csp.run_csp()
    await update.message.reply_text(msg[:4000])


# =========================================================================
# /itm SCAN + TRADE BUTTONS
# =========================================================================

async def cmd_itm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    await update.message.reply_text("Running /itm scan…")
    hits = itm.scan_itm()
    if not hits:
        await update.message.reply_text("No /itm hits.")
        return

    summary_lines = [f"📊 /itm found {len(hits)} hits (showing top {min(MAX_TRADE_BUTTONS, len(hits))} as buttons).\n"]
    await update.message.reply_text("".join(summary_lines))

    pending_itm_trades.setdefault(uid, {})
    expires_at = time.time() + 600

    for idx, hit in enumerate(hits[-MAX_TRADE_BUTTONS:], start=1):
        trade_id = _gen_trade_id()
        pending_itm_trades[uid][trade_id] = (hit, 0, expires_at)

        text = itm.format_itm_hit(hit, idx, min(MAX_TRADE_BUTTONS, len(hits)))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💼 Trade @ {hit['locked_apy']:.1f}% APY", callback_data=f"trade:{trade_id}")
        ]])
        try:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"/itm button send failed for {hit['ticker']}: {e}")
        await asyncio.sleep(TRADE_BUTTON_DELAY)


async def cb_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    logger.info(f"cb_trade FIRED user={uid} data={q.data}")
    await q.answer()

    trade_id = q.data.split(":", 1)[1]
    user_pending = pending_itm_trades.get(uid, {})
    if trade_id not in user_pending:
        logger.info(f"cb_trade: pending lookup found=False")
        await ctx.bot.send_message(uid, "⏱ Trade expired or unknown. Re-run /itm.")
        return

    logger.info(f"cb_trade: pending lookup found=True")
    hit, walk_step, _ = user_pending[trade_id]
    logger.info(f"cb_trade: ticker={hit['ticker']} walk_step={walk_step}")

    pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    next_pricing = orders.compute_legs_pricing(hit, walk_step=walk_step + 1)
    logger.info(f"cb_trade: pricing computed, apy={pricing['apy']}")

    user_pending[trade_id] = (hit, walk_step, time.time() + TRADE_CONFIRM_TIMEOUT)

    preview = orders.format_order_preview(hit, pricing, next_pricing)
    logger.info(f"cb_trade: preview built, {len(preview)} chars")
    await ctx.bot.send_message(uid, preview, parse_mode="Markdown")
    logger.info(f"cb_trade: preview sent (markdown)")


# =========================================================================
# /ritm SCAN + PARKED TRADE BUTTONS
# =========================================================================

async def cmd_ritm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    await update.message.reply_text("Running /ritm scan (borrow rate 25%)…")
    hits = ritm.scan_ritm()
    if not hits:
        await update.message.reply_text("No /ritm hits.")
        return

    await update.message.reply_text(
        f"📊 /ritm found {len(hits)} hits (showing top {min(MAX_TRADE_BUTTONS, len(hits))} as buttons).\n"
        f"⚠️ Orders are PARKED $0.50 below fair value — they will NOT fill until you edit the limit price on Schwab.\n"
    )

    pending_ritm_trades.setdefault(uid, {})
    expires_at = time.time() + 600

    for idx, hit in enumerate(hits[-MAX_TRADE_BUTTONS:], start=1):
        trade_id = _gen_trade_id()
        pending_ritm_trades[uid][trade_id] = (hit, expires_at)

        text = ritm.format_ritm_hit(hit, idx, min(MAX_TRADE_BUTTONS, len(hits)))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🅿️ Park Trade @ {hit['locked_apy']:.1f}% APY", callback_data=f"rtrade:{trade_id}")
        ]])
        try:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"/ritm button send failed for {hit['ticker']}: {e}")
        await asyncio.sleep(TRADE_BUTTON_DELAY)


async def cb_rtrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    logger.info(f"cb_rtrade: ticker={hit['ticker']}")

    pricing = orders.compute_ritm_pricing(hit)
    logger.info(f"cb_rtrade: pricing fair={pricing['fair_net_credit_per_share']} parked={pricing['parked_net_credit_per_share']}")

    user_pending[trade_id] = (hit, time.time() + TRADE_CONFIRM_TIMEOUT)

    preview = orders.format_ritm_preview(hit, pricing)
    await ctx.bot.send_message(uid, preview, parse_mode="Markdown")
    logger.info(f"cb_rtrade: preview sent")


# =========================================================================
# YES CONFIRMATION HANDLER (handles both /itm and /ritm)
# =========================================================================

async def handle_yes_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    text = update.message.text.strip().upper()
    logger.info(f"handle_yes_reply: text='{text}'")

    if not text.startswith("YES "):
        return
    parts = text.split()
    if len(parts) < 2:
        return
    ticker_typed = parts[1]
    logger.info(f"handle_yes_reply: ticker_typed={ticker_typed}")

    now = time.time()

    # Try /ritm pending first
    ritm_pending = pending_ritm_trades.get(uid, {})
    for trade_id, (hit, exp) in list(ritm_pending.items()):
        if hit["ticker"].upper() == ticker_typed and exp > now:
            logger.info(f"handle_yes_reply: matched RITM pending trade_id={trade_id}")
            await _submit_ritm_parked(update, ctx, hit)
            del ritm_pending[trade_id]
            return

    # Then /itm
    itm_pending = pending_itm_trades.get(uid, {})
    for trade_id, (hit, walk_step, exp) in list(itm_pending.items()):
        if hit["ticker"].upper() == ticker_typed and exp > now:
            logger.info(f"handle_yes_reply: matched ITM pending trade_id={trade_id}")
            await _submit_itm(update, ctx, hit, walk_step)
            del itm_pending[trade_id]
            return

    logger.info(f"handle_yes_reply: no matching pending for ticker={ticker_typed}")


async def _submit_itm(update, ctx, hit, walk_step):
    uid = update.effective_user.id
    await update.message.reply_text("📤 Submitting /itm order…")
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
        order_payload = orders.build_itm_conversion_order(hit, pricing)
        logger.info(f"_submit_itm: payload built for {hit['ticker']}")

        client = SchwabClient()
        order_id = client.place_order(order_payload)
        logger.info(f"_submit_itm: order placed, id={order_id}")

        await update.message.reply_text(
            f"✅ Order placed: #{order_id}\n"
            f"{hit['ticker']} ITM conversion at NET_DEBIT ${pricing['stock_price'] - pricing['call_limit'] + pricing['put_limit']:.2f}\n"
            f"Monitoring for 30s…"
        )
        await _monitor_order(update, ctx, order_id, hit, pricing)
    except Exception as e:
        logger.error(f"_submit_itm failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Order build/place failed: {e}")


async def _submit_ritm_parked(update, ctx, hit):
    uid = update.effective_user.id
    await update.message.reply_text("📤 Submitting /ritm PARKED order…")
    try:
        pricing = orders.compute_ritm_pricing(hit)
        order_payload = orders.build_ritm_conversion_order(hit, pricing)
        logger.info(f"_submit_ritm_parked: payload built for {hit['ticker']}")

        client = SchwabClient()
        order_id = client.place_order(order_payload)
        logger.info(f"_submit_ritm_parked: order placed, id={order_id}")

        await update.message.reply_text(
            f"🅿️ *PARKED ORDER placed:* `#{order_id}`\n\n"
            f"{hit['ticker']} reverse conversion\n"
            f"Strike ${hit['strike']:g}  ·  exp {hit['exp_date']} ({hit['dte']}d)\n\n"
            f"Submitted at NET_CREDIT *${pricing['parked_net_credit_per_share']:.2f}/sh*\n"
            f"Fair value: ${pricing['fair_net_credit_per_share']:.2f}/sh\n\n"
            f"⚠️ Order is UNFILLABLE at this price.\n"
            f"👉 Go to Schwab → Orders → #{order_id}\n"
            f"   Cancel/Replace → edit price UP to ~${pricing['fair_net_credit_per_share']:.2f} to fill.\n"
            f"   Or cancel if you don't want it.\n\n"
            f"_Bot will NOT monitor this order._",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"_submit_ritm_parked failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Parked order build/place failed: {e}")


async def _monitor_order(update, ctx, order_id, hit, pricing):
    """Poll Schwab for 30s; if not filled, offer Improve/Cancel buttons."""
    uid = update.effective_user.id
    client = SchwabClient()
    elapsed = 0
    while elapsed < MONITOR_TIMEOUT_SEC:
        await asyncio.sleep(MONITOR_POLL_SEC)
        elapsed += MONITOR_POLL_SEC
        try:
            status = client.get_order_status(order_id)
            logger.info(f"_monitor_order: id={order_id} elapsed={elapsed} status={status}")
            if status == "FILLED":
                await update.message.reply_text(f"✅ Order #{order_id} FILLED.")
                return
            if status in ("CANCELED", "REJECTED", "EXPIRED"):
                await update.message.reply_text(f"⚠️ Order #{order_id} status: {status}")
                return
        except Exception as e:
            logger.warning(f"_monitor_order status check failed: {e}")

    # Not filled in 30s — offer improve/cancel
    next_pricing = orders.compute_legs_pricing(hit, walk_step=pricing["walk_step"] + 1)
    kb_rows = []
    if orders.can_improve(next_pricing):
        kb_rows.append([InlineKeyboardButton(
            f"🔧 Improve → {next_pricing['apy']:.1f}% APY",
            callback_data=f"improve:{order_id}"
        )])
    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{order_id}")])
    await update.message.reply_text(
        f"⏳ Order #{order_id} still WORKING after 30s. Choose:",
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )


async def cb_improve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    logger.info(f"cb_improve FIRED user={uid} data={q.data}")
    await q.answer()
    # Improve logic: cancel current, re-submit at next walk_step
    await ctx.bot.send_message(uid, "🔧 Improve flow not yet wired — cancel manually on Schwab and re-run /itm.")


async def cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    logger.info(f"cb_cancel FIRED user={uid} data={q.data}")
    await q.answer()
    order_id = q.data.split(":", 1)[1]
    try:
        client = SchwabClient()
        client.cancel_order(order_id)
        await ctx.bot.send_message(uid, f"❌ Cancel sent for #{order_id}")
    except Exception as e:
        await ctx.bot.send_message(uid, f"❌ Cancel failed: {e}")


# =========================================================================
# TOKEN MANAGEMENT
# =========================================================================

async def cmd_refresh_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Refresh token flow: see /refresh_token instructions in your notes."
    )


async def cmd_submit_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Submit token: use desktop refresh for now.")


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
