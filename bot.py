"""
bot.py — Telegram handlers and trade flow.
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

# Injected from main.py via build_app
_itm_scanner = None
_schwab_client = None


# ---------- Defensive github_store wrapper ----------
import github_store as _gs


def _gs_try(names, *args, default=None):
    for name in names:
        fn = getattr(_gs, name, None)
        if not callable(fn):
            continue
        try:
            res = fn(*args)
            if res is not None:
                return res
        except TypeError:
            try:
                res = fn()
                if res is not None:
                    return res
            except Exception as e:
                logger.warning(f"github_store.{name}() no-arg failed: {e}")
        except Exception as e:
            logger.warning(f"github_store.{name}{args} failed: {e}")
    return default


def _load_tickers():
    return _gs_try(
        ["load_tickers", "read_tickers", "get_tickers", "list_tickers", "load_file", "read_file"],
        "tickers.txt", default=[]
    ) or []


def _load_whitelist():
    res = _gs_try(
        ["load_whitelist", "read_whitelist", "get_whitelist", "load_file", "read_file"],
        "whitelist.txt", default=None
    )
    if res:
        return res
    logger.warning("no working github_store whitelist fn; using hardcoded default")
    return ["108893493", "396567390"]


def _save_tickers(tickers):
    return _gs_try(["save_tickers", "write_tickers", "write_file", "save_file"], "tickers.txt", tickers)


def _add_ticker_helper(t):
    fn = getattr(_gs, "add_ticker", None)
    if callable(fn):
        try:
            return fn("tickers.txt", t)
        except Exception as e:
            logger.warning(f"github_store.add_ticker failed: {e}")
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


def _get_schwab():
    if _schwab_client is not None:
        return _schwab_client
    try:
        from schwab_client import SchwabClient
        c = SchwabClient()
        if c is not None:
            return c
    except Exception as e:
        logger.warning(f"_get_schwab: SchwabClient() fallback failed: {e}")
    return None


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

def _run_scanner_module(mod, names, *args):
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            try:
                return fn(*args)
            except TypeError:
                try:
                    return fn()
                except Exception as e:
                    logger.warning(f"{mod.__name__}.{name}() failed: {e}")
            except Exception as e:
                logger.warning(f"{mod.__name__}.{name}{args} failed: {e}")
    return f"❌ No working scanner entry point found in {mod.__name__}"


async def cmd_scan(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running collar scan…")
    msg = _run_scanner_module(scanner, ["run_scan", "scan", "run", "main"])
    await update.message.reply_text(str(msg)[:4000])


async def cmd_spreads(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    ticker = ctx.args[0].upper() if ctx.args else None
    await update.message.reply_text("Running spreads scan…")
    msg = _run_scanner_module(spreads_mod, ["run_spreads", "scan", "run", "main"], ticker)
    await update.message.reply_text(str(msg)[:4000])


async def cmd_deepcall(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    n = int(ctx.args[0]) if ctx.args else 5
    await update.message.reply_text(f"Running deepcall (top {n})…")
    msg = _run_scanner_module(deepcall, ["run_deepcall", "scan", "run", "main"], n)
    await update.message.reply_text(str(msg)[:4000])


async def cmd_dca(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running dca scan…")
    msg = _run_scanner_module(dca, ["run_dca", "scan", "run", "main"])
    await update.message.reply_text(str(msg)[:4000])


async def cmd_csp(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    await update.message.reply_text("Running csp scan…")
    msg = _run_scanner_module(csp, ["run_csp", "scan", "run", "main"])
    await update.message.reply_text(str(msg)[:4000])


# =========================================================================
# /itm WITH TRADE BUTTONS
# =========================================================================

def _run_itm_scan():
    global _itm_scanner
    if _itm_scanner is None:
        return None, "❌ /itm scanner not initialized."

    tickers = list(_itm_scanner.ticker_freqs.keys()) if _itm_scanner.ticker_freqs else _load_tickers()
    if not tickers:
        return [], None

    hits = []
    errors = 0
    for t in tickers:
        try:
            raw = _itm_scanner.scan_ticker(t)
            # scan_ticker returns (results_list, debug_counter)
            if isinstance(raw, tuple):
                results = raw[0]
            else:
                results = raw
            if results:
                for item in results:
                    if isinstance(item, dict) and item.get("ticker"):
                        hits.append(item)
        except Exception as e:
            errors += 1
            logger.warning(f"_run_itm_scan: scan_ticker({t}) failed: {e}")

    hits.sort(key=lambda h: h.get("locked_apy", 0))
    logger.info(f"_run_itm_scan: {len(tickers)} tickers, {len(hits)} hits, {errors} errors")
    if hits:
        logger.info(f"_run_itm_scan: first hit FULL: {hits[0]}")
        logger.info(f"_run_itm_scan: first hit ticker={hits[0].get('ticker')} strike={hits[0].get('strike')} apy={hits[0].get('locked_apy')}")
    return hits, None

def _format_itm_hit(hit, idx, total):
    ticker = hit.get("ticker") or "?"
    spot = hit.get("spot") or 0
    strike = hit.get("strike")
    exp_date = hit.get("exp_date") or ""
    dte = hit.get("dte") or 0
    call_credit = hit.get("call_credit") or 0
    put_cost = hit.get("put_cost") or 0
    primary_debit = hit.get("primary_debit") or 0
    fallback_debit = hit.get("fallback_debit") or 0
    fallback_apy = hit.get("fallback_apy") or 0
    locked_total = hit.get("locked_total") or 0
    locked_apy = hit.get("locked_apy") or 0
    call_oi = hit.get("call_oi") or 0
    put_oi = hit.get("put_oi") or 0
    strike_str = f"{strike:g}" if strike is not None else "N/A"
    return (
        f"*{idx}/{total}  {ticker}*  spot ${spot}\n"
        f"  strike ${strike_str}  exp {exp_date} ({dte}d)\n"
        f"  call credit ${call_credit}  put cost ${put_cost}\n"
        f"  primary debit ${primary_debit}  fallback ${fallback_debit} @ {fallback_apy:.1f}%\n"
        f"  *locked ${locked_total:.2f}* @ *{locked_apy:.1f}% APY*\n"
        f"  OI call {call_oi} / put {put_oi}"
    )


async def cmd_itm(update, ctx):
    if not _is_whitelisted(update.effective_user.id):
        return
    uid = update.effective_user.id
    await update.message.reply_text("Running /itm scan…")

    hits, err = _run_itm_scan()
    if err:
        await update.message.reply_text(err)
        return
    if not hits:
        await update.message.reply_text("No /itm hits.")
        return

    await update.message.reply_text(
        f"📊 /itm found {len(hits)} hits (top {min(MAX_TRADE_BUTTONS, len(hits))} as buttons)."
    )
    pending_itm_trades.setdefault(uid, {})
    expires_at = time.time() + 600

    for idx, hit in enumerate(hits[-MAX_TRADE_BUTTONS:], start=1):
        trade_id = _gen_trade_id()
        pending_itm_trades[uid][trade_id] = (hit, 0, expires_at)
        try:
            text = _format_itm_hit(hit, idx, min(MAX_TRADE_BUTTONS, len(hits)))
        except Exception as e:
            logger.warning(f"_format_itm_hit failed for {hit.get('ticker')}: {e}")
            text = f"*{idx} {hit.get('ticker','?')}* spot ${hit.get('spot',0)} apy {hit.get('locked_apy',0):.1f}%"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"💼 Trade @ {hit.get('locked_apy', 0):.1f}% APY",
                callback_data=f"trade:{trade_id}"
            )
        ]])
        try:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"/itm button send failed for {hit.get('ticker')}: {e}")
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
    logger.info(f"cb_trade: ticker={hit.get('ticker')} walk_step={walk_step}")

    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
        next_pricing = orders.compute_legs_pricing(hit, walk_step=walk_step + 1)
        logger.info(f"cb_trade: pricing apy={pricing['apy']}")
    except Exception as e:
        logger.error(f"cb_trade: pricing failed: {e}", exc_info=True)
        await ctx.bot.send_message(uid, f"❌ pricing error: {e}")
        return

    user_pending[trade_id] = (hit, walk_step, time.time() + TRADE_CONFIRM_TIMEOUT)
    preview = orders.format_order_preview(hit, pricing, next_pricing)
    await ctx.bot.send_message(uid, preview, parse_mode="Markdown")
    logger.info("cb_trade: preview sent")


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
            InlineKeyboardButton(
                f"🅿️ Park @ {hit['locked_apy']:.1f}% APY",
                callback_data=f"rtrade:{trade_id}"
            )
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
    try:
        await q.answer()
        logger.info("cb_rtrade: q.answer() OK")
    except Exception as e:
        logger.error(f"cb_rtrade: q.answer() failed: {e}")

    trade_id = q.data.split(":", 1)[1]
    user_pending = pending_ritm_trades.get(uid, {})
    logger.info(f"cb_rtrade: lookup trade_id={trade_id} found={trade_id in user_pending} total={len(user_pending)}")

    if trade_id not in user_pending:
        try:
            await ctx.bot.send_message(uid, "⏱ Trade expired or unknown. Re-run /ritm.")
        except Exception as e:
            logger.error(f"cb_rtrade: expired-msg send failed: {e}")
        return

    hit, _ = user_pending[trade_id]
    logger.info(f"cb_rtrade: ticker={hit['ticker']} strike={hit['strike']} exp={hit['exp_date']}")

    try:
        pricing = orders.compute_ritm_pricing(hit)
        logger.info(f"cb_rtrade: pricing fair=${pricing['fair_net_credit_per_share']:.2f} parked=${pricing['parked_net_credit_per_share']:.2f}")
    except Exception as e:
        logger.error(f"cb_rtrade: pricing failed: {e}", exc_info=True)
        try:
            await ctx.bot.send_message(uid, f"❌ pricing error: {e}")
        except Exception:
            pass
        return

    user_pending[trade_id] = (hit, time.time() + TRADE_CONFIRM_TIMEOUT)

    try:
        preview = orders.format_ritm_preview(hit, pricing)
        logger.info(f"cb_rtrade: preview built {len(preview)} chars")
    except Exception as e:
        logger.error(f"cb_rtrade: preview build failed: {e}", exc_info=True)
        try:
            await ctx.bot.send_message(uid, f"❌ preview build error: {e}")
        except Exception:
            pass
        return

    try:
        sent = await ctx.bot.send_message(uid, preview, parse_mode="Markdown")
        logger.info(f"cb_rtrade: preview sent (markdown) message_id={sent.message_id}")
    except Exception as e:
        logger.warning(f"cb_rtrade: markdown failed: {e} — retrying plain")
        try:
            sent = await ctx.bot.send_message(uid, preview)
            logger.info(f"cb_rtrade: preview sent (plain) message_id={sent.message_id}")
        except Exception as e2:
            logger.error(f"cb_rtrade: plain send ALSO failed: {e2}", exc_info=True)
            try:
                await ctx.bot.send_message(uid, "❌ Could not send confirm preview.")
            except Exception:
                pass


# =========================================================================
# YES HANDLER
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
        client = _get_schwab()
        if client is None:
            raise RuntimeError("No Schwab client available — check token")
        order_id = client.place_order(payload)
        net_debit = pricing["stock_price"] - pricing["call_limit"] + pricing["put_limit"]
        logger.info(f"_submit_itm: order placed id={order_id} ticker={hit['ticker']} net_debit={net_debit:.2f}")
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
        client = _get_schwab()
        if client is None:
            raise RuntimeError("No Schwab client available — check token")
        order_id = client.place_order(payload)
        logger.info(f"_submit_ritm_parked: order placed id={order_id} ticker={hit['ticker']}")
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
    client = _get_schwab()
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
        "🔧 Improve: cancel on Schwab and re-run /itm.")


async def cb_cancel(update, ctx):
    q = update.callback_query
    await q.answer()
    order_id = q.data.split(":", 1)[1]
    try:
        client = _get_schwab()
        client.cancel_order(order_id)
        await ctx.bot.send_message(q.from_user.id, f"❌ Cancel sent #{order_id}")
    except Exception as e:
        await ctx.bot.send_message(q.from_user.id, f"❌ Cancel failed: {e}")


# =========================================================================
# APP BUILD
# =========================================================================

def build_app(*args, **kwargs):
    global _itm_scanner, _schwab_client

    token = None
    for k in ("token", "telegram_token", "bot_token", "tg_token"):
        if k in kwargs and kwargs[k]:
            token = kwargs[k]
            logger.info(f"build_app: token from kwarg '{k}'")
            break
    if not token:
        for a in args:
            if isinstance(a, str) and ":" in a and len(a) >= 40:
                parts = a.split(":", 1)
                if parts[0].isdigit() and len(parts[1]) >= 30:
                    token = a
                    logger.info("build_app: token found in positional args")
                    break
    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if token:
            logger.info("build_app: token from env TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("build_app: no token found")

    logger.info(f"build_app: received {len(args)} positional + {len(kwargs)} keyword args")
    for i, a in enumerate(args):
        atype = type(a).__name__
        aattrs = [x for x in ["scan_ticker", "ticker_freqs", "place_order", "cancel_order",
                               "get_order_status", "get_account_numbers", "get_account_hash",
                               "get_quote", "get_option_chain"] if hasattr(a, x)]
        logger.info(f"build_app: arg[{i}] type={atype} has={aattrs}")

    for a in args:
        if not isinstance(a, str):
            # ItmScanner: prefer larger ticker_freqs universe
            if hasattr(a, "scan_ticker") and hasattr(a, "ticker_freqs"):
                count = len(a.ticker_freqs) if a.ticker_freqs else 0
                current = len(_itm_scanner.ticker_freqs) if _itm_scanner and _itm_scanner.ticker_freqs else 0
                if _itm_scanner is None or count > current:
                    _itm_scanner = a
                    logger.info(f"build_app: injected ItmScanner tickers={count} type={type(a).__name__}")
            # Schwab client: has place_order AND (cancel_order OR get_order_status)
            if (_schwab_client is None and
                    hasattr(a, "place_order") and
                    (hasattr(a, "cancel_order") or hasattr(a, "get_order_status"))):
                _schwab_client = a
                logger.info(f"build_app: injected schwab_client type={type(a).__name__}")

    if _itm_scanner is None:
        logger.warning("build_app: no ItmScanner found — /itm will fail")
    if _schwab_client is None:
        logger.warning("build_app: no schwab_client found — order placement uses SchwabClient() fallback")

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
