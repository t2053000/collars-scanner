"""
bot.py
Telegram bot — scanners + ITM trade execution with improve/cancel flow.
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
import orders

logger = logging.getLogger(__name__)

SCAN_CONCURRENCY = 5
TG_MAX_LEN = 4000
_LAST_ERRORS: deque = deque(maxlen=30)

_PENDING_TRADES: dict = {}
PENDING_TIMEOUT_SEC = 60
_ACTIVE_ORDERS: dict = {}

MAX_TRADE_BUTTONS = 20


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
# Basic commands
# ---------------------------------------------------------------------------

async def cmd_start(update, context):
    await update.message.reply_text(
        "👋 *Options Scanner Bot*\n\n"
        "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm`\n"
        "Send `/help` for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update, context):
    await update.message.reply_text(
        "*Commands*\n"
        "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm`\n"
        "`/list` `/add` `/remove` `/logs` `/whoami`\n"
        "`/refresh_token` `/submit_token`\n\n"
        "*Trading:* `/itm` hits have Trade buttons. Reply `YES TICKER` to confirm.\n"
        "*Reverse scan:* `/itm r` — strikes above spot, put>call. "
        "Tap R Trade button, reply `R TICKER` to submit 2-leg options order. "
        "Short stock manually on Schwab.",
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


# ---------------------------------------------------------------------------
# Generic scan runner
# ---------------------------------------------------------------------------

async def _run_scan(update, context, scanner, label_emoji, format_summary_fn,
                    tickers_override=None, scan_kwargs=None, summary_kwargs=None,
                    hits_with_buttons=False, scanner_key=None):
    tickers = tickers_override if tickers_override is not None else github_store.get_tickers()
    if not tickers:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return

    status_msg = await update.message.reply_text(
        f"{label_emoji} Scanning {len(tickers)} tickers…",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_running_loop()
    all_hits = []
    errors = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    successful = 0
    debug_totals = Counter()
    scan_kwargs = scan_kwargs or {}

    async def scan_one(tk):
        nonlocal successful
        async with sem:
            try:
                result = await loop.run_in_executor(
                    None, lambda: scanner.scan_ticker(tk, **scan_kwargs)
                )
                if isinstance(result, tuple):
                    hits, debug = result
                    debug_totals.update(debug)
                else:
                    hits = result
                all_hits.extend(hits)
                ok = True
            except Exception as e:
                logger.exception(f"scan error for {tk}")
                err_type = type(e).__name__
                short = str(e)[:120].replace("\n", " ")
                full = str(e)[:400].replace("\n", " ")
                errors.append(f"{tk}: {err_type} – {short}")
                _LAST_ERRORS.append(f"{tk}: {err_type} – {full}")
                ok = False
        if ok:
            successful += 1

    await asyncio.gather(*(scan_one(t) for t in tickers))

    kwargs = dict(all_hits=all_hits, scanned=len(tickers),
                  successful=successful, errors=errors)
    if summary_kwargs:
        kwargs.update(summary_kwargs)
    if debug_totals:
        kwargs["debug_totals"] = dict(debug_totals)
    try:
        messages = format_summary_fn(**kwargs)
    except TypeError:
        kwargs.pop("debug_totals", None)
        try:
            messages = format_summary_fn(**kwargs)
        except TypeError:
            for k in list(summary_kwargs or {}):
                kwargs.pop(k, None)
            messages = format_summary_fn(**kwargs)

    await _edit_robust(status_msg, messages[0])
    for extra in messages[1:]:
        await _send_robust(update.message.reply_text, extra)

    if hits_with_buttons and all_hits:
        all_hits.sort(key=lambda r: r["locked_apy"])
        top_hits = all_hits[-MAX_TRADE_BUTTONS:]
        is_reverse = scanner_key == "itm_r"
        logger.info(f"_run_scan: sending {len(top_hits)} trade buttons (reverse={is_reverse})")
        for hit in top_hits:
            try:
                if is_reverse:
                    await _send_rtrade_button(update, context, hit)
                else:
                    await _send_trade_button(update, context, hit)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"trade button send failed for {hit.get('ticker')}: {e}")
                continue


async def _send_trade_button(update, context, hit):
    """Standard ITM conversion trade button — reply YES TICKER to confirm."""
    trade_id = uuid.uuid4().hex[:8]
    user_id = update.effective_user.id
    logger.info(f"_send_trade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={hit.get('locked_apy')}")

    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit": hit,
        "walk_step": 0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse": False,
    }

    summary = (
        f"🔒 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
        f"Strike ${hit['strike']:g} · Net credit ${hit['net_credit']:.2f}/sh\n"
        f"💳 Pay ${hit['primary_debit']:.2f}/sh → *{hit['locked_apy']:.1f}% APY*\n"
        f"🔄 Fallback ${hit['fallback_debit']:.2f}/sh → {hit['fallback_apy']:.1f}% APY\n"
        f"OI {hit['call_oi']}/{hit['put_oi']} · Locked ${hit['locked_total']:.0f}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"💼 Trade @ {hit['locked_apy']:.1f}% APY",
            callback_data=f"trade:{trade_id}",
        ),
    ]])
    try:
        await update.message.reply_text(
            summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    except BadRequest as e:
        logger.warning(f"trade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"trade button send failed even plain: {e2}")


async def _send_rtrade_button(update, context, hit):
    """Reverse ITM trade button (2-leg options only) — reply R TICKER to confirm."""
    trade_id = uuid.uuid4().hex[:8]
    user_id = update.effective_user.id
    logger.info(f"_send_rtrade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={hit.get('locked_apy')}")

    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit": hit,
        "walk_step": 0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse": True,
    }

    htb_flag = " ⚠️HTB?" if hit.get("htb") else ""
    ex_div = hit.get("next_ex_div_date", "")
    ex_div_str = f" · ex-div {ex_div}" if ex_div else ""
    summary = (
        f"🔄 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d){htb_flag}\n"
        f"Strike ${hit['strike']:g} · Net credit ${hit['net_credit']:.2f}/sh{ex_div_str}\n"
        f"💰 Options credit ${hit['primary_debit']:.2f}/sh → *{hit['locked_apy']:.1f}% APY*\n"
        f"🔄 Fallback ${hit['fallback_debit']:.2f}/sh → {hit['fallback_apy']:.1f}% APY\n"
        f"OI {hit['call_oi']}/{hit['put_oi']} · Locked ${hit['locked_total']:.0f}\n"
        f"⚠️ Short stock manually on Schwab"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🔄 R Trade @ {hit['locked_apy']:.1f}% APY",
            callback_data=f"trade:{trade_id}",
        ),
    ]])
    try:
        await update.message.reply_text(
            summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    except BadRequest as e:
        logger.warning(f"rtrade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"rtrade button send failed even plain: {e2}")


# ---------------------------------------------------------------------------
# Scanner commands
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_scan(update, context):
    scanner = context.application.bot_data["collar_scanner"]
    await _run_scan(update, context, scanner, "🔎", CollarScanner.format_summary)


@authorized_only
async def cmd_spreads(update, context):
    scanner = context.application.bot_data["spread_scanner"]
    if context.args:
        sym = context.args[0].upper().strip()
        if sym.isalpha() and 1 <= len(sym) <= 6:
            await _run_scan(update, context, scanner, "💸",
                            SpreadScanner.format_summary, tickers_override=[sym])
            return
    await _run_scan(update, context, scanner, "💸", SpreadScanner.format_summary)


@authorized_only
async def cmd_deepcall(update, context):
    scanner = context.application.bot_data["deepcall_scanner"]
    cushion_pct = DEFAULT_CUSHION_PCT
    if context.args:
        try:
            requested = float(context.args[0])
            clamped, was_clamped = clamp_cushion(requested)
            cushion_pct = clamped
            if was_clamped:
                await update.message.reply_text(
                    f"⚠️ Using *{clamped:g}%* cushion",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except ValueError:
            await update.message.reply_text(
                f"Usage: `/deepcall [N]`", parse_mode=ParseMode.MARKDOWN
            )
            return
    await _run_scan(
        update, context, scanner, "🛡️", DeepCallScanner.format_summary,
        scan_kwargs={"cushion_pct": cushion_pct},
        summary_kwargs={"cushion_pct": cushion_pct},
    )


@authorized_only
async def cmd_dca(update, context):
    scanner = context.application.bot_data["dca_scanner"]
    div_tickers = github_store.get_div_tickers()
    if not div_tickers:
        await update.message.reply_text("_Empty._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    tickers = sorted(div_tickers.keys())
    await _run_scan(update, context, scanner, "💰", DcaScanner.format_summary,
                    tickers_override=tickers)


@authorized_only
async def cmd_csp(update, context):
    scanner = context.application.bot_data["csp_scanner"]
    div_tickers = github_store.get_div_tickers()
    if not div_tickers:
        await update.message.reply_text("_Empty._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    tickers = sorted(div_tickers.keys())
    await _run_scan(update, context, scanner, "💵", CspScanner.format_summary,
                    tickers_override=tickers)


@authorized_only
async def cmd_itm(update, context):
    scanner = context.application.bot_data["itm_scanner"]
    div_tickers = github_store.get_div_tickers()
    watchlist = github_store.get_tickers()
    combined = sorted(set(watchlist) | set(div_tickers.keys()))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers

    reverse_mode = bool(context.args and context.args[0].lower() == "r")

    if reverse_mode:
        original_scan_ticker = scanner.scan_ticker
        scanner.scan_ticker = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=combined,
                        hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original_scan_ticker
    else:
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=combined,
                        hits_with_buttons=True, scanner_key="itm")


@authorized_only
async def cmd_ritm(update, context):
    scanner = context.application.bot_data["ritm_scanner"]
    div_tickers = github_store.get_div_tickers()
    watchlist = github_store.get_tickers()
    combined = sorted(set(watchlist) | set(div_tickers.keys()))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    await _run_scan(update, context, scanner, "🔄", RitmScanner.format_summary,
                    tickers_override=combined)


# ---------------------------------------------------------------------------
# Trade flow — with verbose logging
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_trade(update, context):
    query = update.callback_query
    logger.info(f"cb_trade FIRED user={update.effective_user.id} data={query.data}")
    try:
        await query.answer()
    except Exception as e:
        logger.exception(f"cb_trade: query.answer failed: {e}")

    user_id = update.effective_user.id
    try:
        trade_id = query.data.split(":", 1)[1]
    except Exception as e:
        logger.exception(f"cb_trade: failed to parse trade_id: {e}")
        await query.message.reply_text(f"❌ Bad callback data: {query.data}")
        return

    pending = _PENDING_TRADES.get((user_id, trade_id))
    logger.info(f"cb_trade: pending lookup ({user_id},{trade_id}) found={pending is not None}, total pending={len(_PENDING_TRADES)}")
    if not pending:
        await query.message.reply_text(
            f"⏱ Trade expired (no entry for {trade_id}). Re-run /itm."
        )
        return

    hit = pending["hit"]
    walk_step = pending["walk_step"]
    is_reverse = pending.get("reverse", False)
    logger.info(f"cb_trade: ticker={hit.get('ticker')} walk_step={walk_step} reverse={is_reverse}")

    try:
        if is_reverse:
            pricing = orders.compute_reverse_pricing(hit, walk_step=walk_step)
            next_pricing = orders.compute_reverse_pricing(hit, walk_step=walk_step + 1)
        else:
            pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
            next_pricing = orders.compute_legs_pricing(hit, walk_step=walk_step + 1)
        logger.info(f"cb_trade: pricing computed, apy={pricing.get('apy')}")
    except Exception as e:
        logger.exception(f"cb_trade: pricing failed: {e}")
        await query.message.reply_text(f"❌ Pricing calc failed: {type(e).__name__}: {e}")
        return

    pending["expires_at"] = time.time() + PENDING_TIMEOUT_SEC
    pending["pricing"] = pricing
    pending["chat_id"] = query.message.chat_id
    pending["trade_id"] = trade_id

    try:
        if is_reverse:
            preview = orders.format_reverse_order_preview(hit, pricing, next_pricing)
        else:
            preview = orders.format_order_preview(hit, pricing, next_pricing)
        logger.info(f"cb_trade: preview built, {len(preview)} chars")
    except Exception as e:
        logger.exception(f"cb_trade: format preview failed: {e}")
        await query.message.reply_text(f"❌ Preview format failed: {type(e).__name__}: {e}")
        return

    try:
        await query.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
        logger.info("cb_trade: preview sent (markdown)")
    except BadRequest as e:
        logger.warning(f"cb_trade: markdown failed: {e}, retrying plain")
        plain = preview.replace("*", "").replace("_", "").replace("`", "")
        try:
            await query.message.reply_text(plain)
            logger.info("cb_trade: preview sent (plain)")
        except Exception as e2:
            logger.exception(f"cb_trade: plain preview failed: {e2}")
            await query.message.reply_text(f"❌ Send failed: {type(e2).__name__}")
    except Exception as e:
        logger.exception(f"cb_trade: unexpected send error: {e}")
        await query.message.reply_text(f"❌ Send error: {type(e).__name__}: {e}")


async def handle_yes_reply(update, context):
    """Handles both YES TICKER (ITM) and R TICKER (reverse ITM) confirmations."""
    user = update.effective_user
    if not user or not github_store.is_authorized(user.id):
        return

    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        return

    keyword = parts[0].upper()
    if keyword not in ("YES", "R"):
        return

    is_reverse = keyword == "R"
    ticker = parts[1].upper()
    user_id = user.id

    logger.info(f"handle_yes_reply: keyword={keyword} ticker={ticker} user={user_id} reverse={is_reverse}")

    now = time.time()
    matching = None
    for (uid, tid), pending in list(_PENDING_TRADES.items()):
        if uid != user_id:
            continue
        if pending.get("hit", {}).get("ticker", "").upper() != ticker:
            continue
        if pending.get("expires_at", 0) < now:
            logger.info(f"handle_yes_reply: removed expired {tid}")
            del _PENDING_TRADES[(uid, tid)]
            continue
        if "pricing" not in pending:
            logger.info(f"handle_yes_reply: skipping {tid}, no pricing (button not tapped)")
            continue
        # Match YES to non-reverse, R to reverse
        if pending.get("reverse", False) != is_reverse:
            continue
        matching = (uid, tid, pending)
        break

    if not matching:
        logger.info(f"handle_yes_reply: no matching pending for {ticker}")
        if is_reverse:
            await update.message.reply_text(
                f"❌ No pending reverse {ticker} trade. Tap R Trade button first, then reply R {ticker} within 60s."
            )
        else:
            await update.message.reply_text(
                f"❌ No pending {ticker} trade. Tap Trade button first, then reply YES {ticker} within 60s."
            )
        return

    uid, tid, pending = matching
    hit = pending["hit"]
    pricing = pending["pricing"]
    logger.info(f"handle_yes_reply: matched pending {tid} reverse={is_reverse}, building order")

    schwab = context.application.bot_data["schwab_client"]

    try:
        if is_reverse:
            order_payload = orders.build_reverse_itm_order(hit, pricing)
        else:
            order_payload = orders.build_itm_conversion_order(hit, pricing)
        logger.info(f"handle_yes_reply: order payload built")
    except Exception as e:
        logger.exception("order build failed")
        await update.message.reply_text(f"❌ Order build failed: {type(e).__name__}: {e}")
        return

    action_str = f"{ticker} reverse ITM (options only)" if is_reverse else f"{ticker} ITM conversion"
    status_msg = await update.message.reply_text(
        f"📤 Submitting {action_str} at {pricing['apy']:.1f}% APY…"
    )

    loop = asyncio.get_running_loop()
    try:
        order_id = await loop.run_in_executor(None, schwab.place_order, order_payload)
        logger.info(f"handle_yes_reply: order placed, id={order_id}")
    except Exception as e:
        logger.exception("place_order failed")
        await _edit_robust(status_msg, f"❌ Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return

    del _PENDING_TRADES[(uid, tid)]

    if is_reverse:
        # Reverse orders: no monitoring (NET_CREDIT, user manages stock leg manually)
        await _edit_robust(
            status_msg,
            f"✅ Order *{order_id}* submitted for {ticker} (options legs)\n"
            f"SELL put + BUY call at NET_CREDIT ${pricing['net_credit']:.2f}\n"
            f"⚠️ *Remember to short {ticker} stock on Schwab manually.*"
        )
        return

    # Standard ITM — monitor fill
    _ACTIVE_ORDERS[user_id] = {
        "order_id": order_id,
        "hit": hit,
        "pricing": pricing,
        "walk_step": pending["walk_step"],
        "chat_id": status_msg.chat_id,
        "message_id": status_msg.message_id,
    }

    await _edit_robust(
        status_msg,
        f"📤 Order *{order_id}* submitted for {ticker}\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY if filled: *{pricing['apy']:.1f}%*\n"
        f"⏳ Monitoring for fill (30s)…"
    )

    asyncio.create_task(monitor_order(context, user_id, order_id, status_msg))


async def monitor_order(context, user_id, order_id, status_msg):
    logger.info(f"monitor_order START: user={user_id} order={order_id}")
    schwab = context.application.bot_data["schwab_client"]
    loop = asyncio.get_running_loop()
    start = time.time()
    filled = False

    while time.time() - start < 30:
        await asyncio.sleep(5)
        try:
            status = await loop.run_in_executor(None, schwab.get_order_status, order_id)
            status_str = status.get("status", "UNKNOWN")
            logger.info(f"monitor_order: order={order_id} status={status_str}")
            if status_str == "FILLED":
                filled = True
                break
            if status_str in ("CANCELED", "REJECTED", "EXPIRED"):
                active = _ACTIVE_ORDERS.pop(user_id, None)
                hit = active["hit"] if active else None
                tkr = hit["ticker"] if hit else "?"
                await _edit_robust(
                    status_msg,
                    f"⚠️ Order {order_id} for {tkr} ended: {status_str}",
                )
                return
        except Exception as e:
            logger.warning(f"order status poll failed: {e}")
            continue

    if filled:
        active = _ACTIVE_ORDERS.pop(user_id, None)
        hit = active["hit"] if active else None
        tkr = hit["ticker"] if hit else "?"
        await _edit_robust(
            status_msg,
            f"✅ *FILLED* — order {order_id} for {tkr}",
        )
        return

    active = _ACTIVE_ORDERS.get(user_id)
    if not active:
        return

    hit = active["hit"]
    walk_step = active["walk_step"]
    next_pricing = orders.compute_legs_pricing(hit, walk_step=walk_step + 1)
    improve_enabled = orders.can_improve(next_pricing)

    buttons = []
    if improve_enabled:
        buttons.append([InlineKeyboardButton(
            f"🔧 Improve → {next_pricing['apy']:.1f}% APY",
            callback_data=f"improve:{order_id}",
        )])
    buttons.append([InlineKeyboardButton(
        "❌ Cancel order", callback_data=f"cancel:{order_id}",
    )])
    keyboard = InlineKeyboardMarkup(buttons)

    floor_note = ""
    if not improve_enabled:
        floor_note = f"\n⚠️ Can't improve — next would drop below {orders.MIN_APY_FLOOR_PCT:g}% floor."

    await _edit_robust(
        status_msg,
        f"⏳ Order {order_id} for *{hit['ticker']}* not filled after 30s.\n"
        f"Current limit: ${active['pricing']['call_limit']:.2f} / ${active['pricing']['put_limit']:.2f}\n"
        f"Current APY: {active['pricing']['apy']:.1f}%{floor_note}",
        reply_markup=keyboard,
    )


@authorized_callback
async def cb_improve(update, context):
    query = update.callback_query
    logger.info(f"cb_improve FIRED user={update.effective_user.id} data={query.data}")
    await query.answer()
    user_id = update.effective_user.id
    order_id = query.data.split(":", 1)[1]

    active = _ACTIVE_ORDERS.get(user_id)
    if not active or active["order_id"] != order_id:
        await query.message.reply_text("⏱ Order session expired.")
        return

    schwab = context.application.bot_data["schwab_client"]
    loop = asyncio.get_running_loop()
    hit = active["hit"]

    try:
        await loop.run_in_executor(None, schwab.cancel_order, order_id)
    except Exception as e:
        logger.warning(f"cancel during improve failed: {e}")

    new_walk = active["walk_step"] + 1
    new_pricing = orders.compute_legs_pricing(hit, walk_step=new_walk)
    if not orders.can_improve(new_pricing):
        await query.message.reply_text(
            f"⚠️ Improvement would drop below {orders.MIN_APY_FLOOR_PCT:g}% floor. Aborted."
        )
        _ACTIVE_ORDERS.pop(user_id, None)
        return

    try:
        order_payload = orders.build_itm_conversion_order(hit, new_pricing)
        new_order_id = await loop.run_in_executor(None, schwab.place_order, order_payload)
    except Exception as e:
        logger.exception("improve resubmit failed")
        await query.message.reply_text(f"❌ Resubmit failed: {type(e).__name__}: {e}")
        _ACTIVE_ORDERS.pop(user_id, None)
        return

    status_msg = await query.message.reply_text(
        f"🔁 Retry #{new_walk}: order *{new_order_id}* @ {new_pricing['apy']:.1f}% APY\n"
        f"⏳ Monitoring (30s)…",
        parse_mode=ParseMode.MARKDOWN,
    )

    _ACTIVE_ORDERS[user_id] = {
        "order_id": new_order_id,
        "hit": hit,
        "pricing": new_pricing,
        "walk_step": new_walk,
        "chat_id": status_msg.chat_id,
        "message_id": status_msg.message_id,
    }
    asyncio.create_task(monitor_order(context, user_id, new_order_id, status_msg))


@authorized_callback
async def cb_cancel(update, context):
    query = update.callback_query
    logger.info(f"cb_cancel FIRED user={update.effective_user.id} data={query.data}")
    await query.answer()
    user_id = update.effective_user.id
    order_id = query.data.split(":", 1)[1]

    active = _ACTIVE_ORDERS.get(user_id)
    if not active or active["order_id"] != order_id:
        await query.message.reply_text("⏱ No active order.")
        return

    schwab = context.application.bot_data["schwab_client"]
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, schwab.cancel_order, order_id)
        await query.message.reply_text(f"❌ Order {order_id} cancelled.")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Cancel failed: {e}")
    _ACTIVE_ORDERS.pop(user_id, None)


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_refresh_token(update, context):
    schwab_client = context.application.bot_data["schwab_client"]
    auth_url = schwab_client.build_authorize_url()
    msg = (
        "🔐 Schwab Token Refresh\n\n"
        "⚡ Auth codes expire in ~10 sec — be ready!\n\n"
        "1️⃣ type /submit_token (with space) — DO NOT SEND YET\n"
        "2️⃣ Tap URL below:\n"
        f"{auth_url}\n"
        "3️⃣ Log in → tap Allow\n"
        "4️⃣ Browser shows broken https://127.0.0.1/?code=... page\n"
        "5️⃣ Long-press address bar → Copy URL\n"
        "6️⃣ Switch to Telegram → paste → Send IMMEDIATELY"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)


@authorized_only
async def cmd_submit_token(update, context):
    schwab_client = context.application.bot_data["schwab_client"]
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /submit_token <URL or code>")
        return
    payload = parts[1].strip()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, schwab_client.exchange_code_for_token, payload
        )
    except Exception as e:
        logger.exception("token exchange failed")
        await update.message.reply_text(
            f"❌ Token exchange failed:\n{type(e).__name__}: {str(e)[:300]}\n\n"
            "Most common: auth code expired. Try /refresh_token again."
        )
        return
    await update.message.reply_text("✅ Token refreshed!")


# ---------------------------------------------------------------------------
# Wire everything up
# ---------------------------------------------------------------------------

def build_app(telegram_token,
              collar_scanner,
              spread_scanner,
              deepcall_scanner,
              dca_scanner,
              csp_scanner,
              itm_scanner,
              ritm_scanner,
              schwab_client):
    app = Application.builder().token(telegram_token).build()
    app.bot_data["collar_scanner"]   = collar_scanner
    app.bot_data["spread_scanner"]   = spread_scanner
    app.bot_data["deepcall_scanner"] = deepcall_scanner
    app.bot_data["dca_scanner"]      = dca_scanner
    app.bot_data["csp_scanner"]      = csp_scanner
    app.bot_data["itm_scanner"]      = itm_scanner
    app.bot_data["ritm_scanner"]     = ritm_scanner
    app.bot_data["schwab_client"]    = schwab_client

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("whoami",        cmd_whoami))
    app.add_handler(CommandHandler("list",          cmd_list))
    app.add_handler(CommandHandler("add",           cmd_add))
    app.add_handler(CommandHandler("remove",        cmd_remove))
    app.add_handler(CommandHandler("scan",          cmd_scan))
    app.add_handler(CommandHandler("spreads",       cmd_spreads))
    app.add_handler(CommandHandler("deepcall",      cmd_deepcall))
    app.add_handler(CommandHandler("deepcalls",     cmd_deepcall))
    app.add_handler(CommandHandler("dca",           cmd_dca))
    app.add_handler(CommandHandler("csp",           cmd_csp))
    app.add_handler(CommandHandler("itm",           cmd_itm))
    app.add_handler(CommandHandler("ritm",          cmd_ritm))
    app.add_handler(CommandHandler("logs",          cmd_logs))
    app.add_handler(CommandHandler("refresh_token", cmd_refresh_token))
    app.add_handler(CommandHandler("submit_token",  cmd_submit_token))

    app.add_handler(CallbackQueryHandler(cb_trade,   pattern=r"^trade:"))
    app.add_handler(CallbackQueryHandler(cb_improve, pattern=r"^improve:"))
    app.add_handler(CallbackQueryHandler(cb_cancel,  pattern=r"^cancel:"))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_yes_reply
    ))

    return app