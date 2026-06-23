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
_ACTIVE_ORDERS: dict = {}

MAX_TRADE_BUTTONS = 20


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
        "· `/itm` — tap Confirm to place order instantly\n"
        "· `/itm r` — reverse ITM scan via Schwab\n"
        "· `/itmib` — reverse ITM scan via IBKR (better data)\n"
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


# ---------------------------------------------------------------------------
# Positions command
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_positions(update, context):
    """Show all positions expiring this Friday with projected P/L and APY."""
    user_id = update.effective_user.id
    schwab  = _get_schwab_for_user(context, user_id)
    msg     = await update.message.reply_text("📊 Fetching positions…")
    loop    = asyncio.get_running_loop()
    try:
        raw  = await loop.run_in_executor(None, schwab.get_positions)
        text = await loop.run_in_executor(None, compute_positions, raw)
        await _edit_robust(msg, text)
    except Exception as e:
        logger.exception("cmd_positions failed")
        await _edit_robust(msg, f"❌ Error fetching positions: {type(e).__name__}: {str(e)[:300]}")


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
# Trade buttons
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
                await update.message.reply_text(f"⚠️ Using *{clamped:g}%* cushion", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Usage: `/deepcall [N]`", parse_mode=ParseMode.MARKDOWN)
            return
    await _run_scan(update, context, scanner, "🛡️", DeepCallScanner.format_summary,
                    scan_kwargs={"cushion_pct": cushion_pct},
                    summary_kwargs={"cushion_pct": cushion_pct})


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
                    tickers_override=tickers, hits_with_buttons=True, scanner_key="dca")


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
        source = "Barchart"
    else:
        # === ONLY from tickers.txt (your requested behavior) ===
        tickers = github_store.get_tickers()
        source = "tickers.txt"

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


@authorized_only
async def cmd_ritm(update, context):
    scanner     = context.application.bot_data["ritm_scanner"]
    div_tickers = github_store.get_div_tickers()

    args    = [a.lower() for a in (context.args or [])]
    bc_mode = "bc" in args

    if bc_mode:
        tickers = github_store.get_latest_barchart_tickers()
        if not tickers:
            await update.message.reply_text("⚠️ No Barchart tickers available yet — try again during market hours.")
            return
    else:
        hiv_tickers = github_store.get_latest_hiv_tickers()
        tickers     = hiv_tickers if hiv_tickers else github_store.get_tickers()

    combined = sorted(set(tickers))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    await _run_scan(update, context, scanner, "🔄", RitmScanner.format_summary,
                    tickers_override=combined)


@authorized_only
async def cmd_itmib(update, context):
    scanner = context.application.bot_data.get("itm_ibkr_scanner")
    if scanner is None:
        await update.message.reply_text("IBKR scanner unavailable — check VPS/Tailscale connection.")
        return
    div_tickers = github_store.get_div_tickers()

    args    = [a.lower() for a in (context.args or [])]
    bc_mode = "bc" in args

    if bc_mode:
        tickers = github_store.get_latest_barchart_tickers()
        if not tickers:
            await update.message.reply_text("⚠️ No Barchart tickers available yet — try again during market hours.")
            return
    else:
        hiv_tickers = github_store.get_latest_hiv_tickers()
        tickers     = hiv_tickers if hiv_tickers else github_store.get_tickers()

    combined = sorted(set(tickers))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    original = scanner.scan_ticker
    scanner.scan_ticker = scanner.scan_ticker_reverse
    await _run_scan(update, context, scanner, "🔄", ItmIbkrScanner.format_summary,
                    tickers_override=combined, hits_with_buttons=True, scanner_key="itm_r")
    scanner.scan_ticker = original


# ---------------------------------------------------------------------------
# ITM confirm / cancel callbacks
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_trade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    logger.info(f"cb_confirm_trade FIRED user={user_id} trade_id={trade_id}")
    await query.answer()
    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await _edit_robust(query.message, "Trade expired. Re-run /itm.")
        return
    hit       = pending["hit"]
    walk_step = pending["walk_step"]
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_trade: pricing failed")
        await _edit_robust(query.message, f"Pricing failed: {type(e).__name__}: {e}")
        return
    await _edit_robust(query.message, f"Submitting {hit['ticker']} ITM @ {pricing['apy']:.1f}% APY...")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_itm_conversion_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_trade: order placed id={order_id} ticker={hit['ticker']}")
    except Exception as e:
        logger.exception("cb_confirm_trade: place_order failed")
        await _edit_robust(query.message, f"Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return
    del _PENDING_TRADES[(user_id, trade_id)]
    _ACTIVE_ORDERS[user_id] = {
        "order_id": order_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "itm",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {hit['ticker']} ITM\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY: *{pricing['apy']:.1f}%*\nMonitoring (30s)...")
    asyncio.create_task(monitor_order(context, user_id, order_id, query.message))


@authorized_callback
async def cb_cancel_trade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    await query.answer("Cancelled.")
    _PENDING_TRADES.pop((user_id, trade_id), None)
    await _edit_robust(query.message, "Trade cancelled.")


# ---------------------------------------------------------------------------
# DCA confirm / cancel callbacks
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_dca(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    logger.info(f"cb_confirm_dca FIRED user={user_id} trade_id={trade_id}")
    await query.answer()
    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await _edit_robust(query.message, "Trade expired. Re-run /dca.")
        return
    hit       = pending["hit"]
    walk_step = pending["walk_step"]
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_dca: pricing failed")
        await _edit_robust(query.message, f"Pricing failed: {type(e).__name__}: {e}")
        return
    await _edit_robust(query.message, f"Submitting {hit['ticker']} DCA @ {pricing['apy']:.1f}% APY...")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_itm_conversion_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_dca: order placed id={order_id} ticker={hit['ticker']}")
    except Exception as e:
        logger.exception("cb_confirm_dca: place_order failed")
        await _edit_robust(query.message, f"Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return
    del _PENDING_TRADES[(user_id, trade_id)]
    _ACTIVE_ORDERS[user_id] = {
        "order_id": order_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "dca",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {hit['ticker']} DCA\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY: *{pricing['apy']:.1f}%*\nMonitoring (30s)...")
    asyncio.create_task(monitor_order(context, user_id, order_id, query.message))


@authorized_callback
async def cb_cancel_dca(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    await query.answer("Cancelled.")
    _PENDING_TRADES.pop((user_id, trade_id), None)
    await _edit_robust(query.message, "Trade cancelled.")


# ---------------------------------------------------------------------------
# Reverse ITM confirm / cancel callbacks
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_rtrade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    logger.info(f"cb_confirm_rtrade FIRED user={user_id} trade_id={trade_id}")
    await query.answer()
    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await _edit_robust(query.message, "Trade expired. Re-run /itm r.")
        return
    hit       = pending["hit"]
    walk_step = pending["walk_step"]
    ticker    = hit["ticker"]
    try:
        pricing = orders.compute_reverse_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_rtrade: pricing failed")
        await _edit_robust(query.message, f"Pricing failed: {type(e).__name__}: {e}")
        return
    await _edit_robust(query.message, f"Submitting {ticker} reverse ITM options @ {pricing['apy']:.1f}% APY...")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_reverse_itm_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_rtrade: order placed id={order_id} ticker={ticker}")
    except Exception as e:
        logger.exception("cb_confirm_rtrade: place_order failed")
        await _edit_robust(query.message, f"Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return
    del _PENDING_TRADES[(user_id, trade_id)]
    _ACTIVE_ORDERS[user_id] = {
        "order_id": order_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "itm_r",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {ticker} options\n"
        f"SELL put + BUY call · NET CREDIT ${pricing['net_credit']:.2f}\n"
        f"Monitoring for fill (30s)...")
    asyncio.create_task(monitor_rtrade_order(context, user_id, order_id, query.message, ticker))


@authorized_callback
async def cb_cancel_rtrade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    await query.answer("Cancelled.")
    _PENDING_TRADES.pop((user_id, trade_id), None)
    await _edit_robust(query.message, "Trade cancelled.")


# ---------------------------------------------------------------------------
# Order monitoring — ITM / DCA
# ---------------------------------------------------------------------------

async def monitor_order(context, user_id, order_id, status_msg):
    logger.info(f"monitor_order START: user={user_id} order={order_id}")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    start  = time.time()
    filled = False
    while time.time() - start < 30:
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
    if filled:
        active = _ACTIVE_ORDERS.pop(user_id, None)
        tkr    = active["hit"]["ticker"] if active else "?"
        await _edit_robust(status_msg, f"FILLED — order {order_id} for {tkr}")
        return
    active = _ACTIVE_ORDERS.get(user_id)
    if not active:
        return
    hit          = active["hit"]
    walk_step    = active["walk_step"]
    next_pricing = orders.compute_legs_pricing(hit, walk_step=walk_step + 1)
    improve_ok   = orders.can_improve(next_pricing)
    buttons = []
    if improve_ok:
        buttons.append([InlineKeyboardButton(
            f"Improve -> {next_pricing['apy']:.1f}% APY", callback_data=f"improve:{order_id}")])
    buttons.append([InlineKeyboardButton("Cancel order", callback_data=f"cancel:{order_id}")])
    floor_note = f"\nCannot improve — below {orders.MIN_APY_FLOOR_PCT:g}% floor." if not improve_ok else ""
    await _edit_robust(status_msg,
        f"Order {order_id} for *{hit['ticker']}* not filled after 30s.\n"
        f"Limit: ${active['pricing']['call_limit']:.2f} / ${active['pricing']['put_limit']:.2f}\n"
        f"APY: {active['pricing']['apy']:.1f}%{floor_note}",
        reply_markup=InlineKeyboardMarkup(buttons))


# ---------------------------------------------------------------------------
# Order monitoring — Reverse ITM
# ---------------------------------------------------------------------------

async def monitor_rtrade_order(context, user_id, order_id, status_msg, ticker):
    logger.info(f"monitor_rtrade_order START: user={user_id} order={order_id}")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    start  = time.time()
    filled = False
    while time.time() - start < 30:
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
        try:
            short_payload  = orders.build_short_stock_order(ticker)
            short_order_id = await loop.run_in_executor(None, schwab.place_order, short_payload)
            logger.info(f"monitor_rtrade_order: short placed id={short_order_id} ticker={ticker}")
            await _edit_robust(status_msg,
                f"OPTIONS FILLED — order {order_id} · {ticker}\n"
                f"SHORT order *{short_order_id}* submitted · SELL SHORT 100 {ticker} @ MKT\n"
                f"Check Schwab to confirm both legs are working.")
        except Exception as e:
            logger.exception(f"monitor_rtrade_order: short stock order failed: {e}")
            await _edit_robust(status_msg,
                f"OPTIONS FILLED — order {order_id} · {ticker}\n"
                f"SHORT order FAILED: {type(e).__name__}: {str(e)[:200]}\n"
                f"SHORT {ticker} MANUALLY ON SCHWAB NOW.")
    else:
        try:
            await loop.run_in_executor(None, schwab.cancel_order, order_id)
            await _edit_robust(status_msg,
                f"Order {order_id} · {ticker} not filled after 30s — auto-cancelled.\n"
                f"Re-run /itm r to try again.")
            logger.info(f"monitor_rtrade_order: auto-cancelled {order_id} for {ticker}")
        except Exception as e:
            logger.warning(f"monitor_rtrade_order: auto-cancel failed: {e}")
            await _edit_robust(status_msg,
                f"Order {order_id} · {ticker} not filled after 30s.\n"
                f"Auto-cancel failed — cancel manually on Schwab.\n"
                f"Do NOT short {ticker} manually.")


# ---------------------------------------------------------------------------
# Improve / Cancel
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_improve(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    order_id = query.data.split(":", 1)[1]
    logger.info(f"cb_improve FIRED user={user_id} order={order_id}")
    await query.answer()
    active = _ACTIVE_ORDERS.get(user_id)
    if not active or active["order_id"] != order_id:
        await query.message.reply_text("Order session expired.")
        return
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    hit    = active["hit"]
    try:
        await loop.run_in_executor(None, schwab.cancel_order, order_id)
    except Exception as e:
        logger.warning(f"cancel during improve failed: {e}")
    new_walk    = active["walk_step"] + 1
    new_pricing = orders.compute_legs_pricing(hit, walk_step=new_walk)
    if not orders.can_improve(new_pricing):
        await query.message.reply_text(f"Improvement would drop below {orders.MIN_APY_FLOOR_PCT:g}% floor. Aborted.")
        _ACTIVE_ORDERS.pop(user_id, None)
        return
    try:
        payload      = orders.build_itm_conversion_order(hit, new_pricing)
        new_order_id = await loop.run_in_executor(None, schwab.place_order, payload)
    except Exception as e:
        logger.exception("improve resubmit failed")
        await query.message.reply_text(f"Resubmit failed: {type(e).__name__}: {e}")
        _ACTIVE_ORDERS.pop(user_id, None)
        return
    status_msg = await query.message.reply_text(
        f"Retry #{new_walk}: order *{new_order_id}* @ {new_pricing['apy']:.1f}% APY\nMonitoring (30s)...",
        parse_mode=ParseMode.MARKDOWN)
    _ACTIVE_ORDERS[user_id] = {
        "order_id": new_order_id, "hit": hit, "pricing": new_pricing,
        "walk_step": new_walk, "trade_type": active.get("trade_type", "itm"),
    }
    asyncio.create_task(monitor_order(context, user_id, new_order_id, status_msg))


@authorized_callback
async def cb_cancel(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    order_id = query.data.split(":", 1)[1]
    logger.info(f"cb_cancel FIRED user={user_id} order={order_id}")
    await query.answer()
    active = _ACTIVE_ORDERS.get(user_id)
    if not active or active["order_id"] != order_id:
        await query.message.reply_text("No active order.")
        return
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, schwab.cancel_order, order_id)
        await query.message.reply_text(f"Order {order_id} cancelled.")
    except Exception as e:
        await query.message.reply_text(f"Cancel failed: {e}")
    _ACTIVE_ORDERS.pop(user_id, None)


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_refresh_token(update, context):
    user_id       = update.effective_user.id
    schwab_client = _get_schwab_for_user(context, user_id)
    auth_url      = schwab_client.build_authorize_url()
    msg = (
        "Schwab Token Refresh\n\n"
        "Auth codes expire in ~10 sec — be ready!\n\n"
        "1. type /submit_token (with space) — DO NOT SEND YET\n"
        "2. Tap URL below:\n"
        f"{auth_url}\n"
        "3. Log in, tap Allow\n"
        "4. Browser shows broken https://127.0.0.1/?code=... page\n"
        "5. Long-press address bar, Copy URL\n"
        "6. Switch to Telegram, paste, Send IMMEDIATELY"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)


@authorized_only
async def cmd_submit_token(update, context):
    user_id       = update.effective_user.id
    schwab_client = _get_schwab_for_user(context, user_id)
    text  = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /submit_token <URL or code>")
        return
    payload = parts[1].strip()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, schwab_client.exchange_code_for_token, payload)
        token_json = schwab_client.get_token_json()
        await loop.run_in_executor(None, github_store.save_schwab_token, user_id, token_json)
        logger.info(f"Saved refreshed token to GitHub for user {user_id}")
    except Exception as e:
        logger.exception("token exchange failed")
        await update.message.reply_text(
            f"Token exchange failed: {type(e).__name__}: {str(e)[:300]}\n\n"
            "Most common: auth code expired. Try /refresh_token again.")
        return
    await update.message.reply_text("Token refreshed!")


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
    app.bot_data["itm_ibkr_scanner"]  = itm_ibkr_scanner
    primary_schwab = schwab_clients.get(primary_user_id)
    app.bot_data["schwab_client"]     = primary_schwab

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
    app.add_handler(CommandHandler("itmib",         cmd_itmib))
    app.add_handler(CommandHandler("positions",     cmd_positions))
    app.add_handler(CommandHandler("logs",          cmd_logs))
    app.add_handler(CommandHandler("refresh_token", cmd_refresh_token))
    app.add_handler(CommandHandler("submit_token",  cmd_submit_token))

    app.add_handler(CallbackQueryHandler(cb_confirm_trade,  pattern=r"^confirm_trade:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_trade,   pattern=r"^cancel_trade:"))
    app.add_handler(CallbackQueryHandler(cb_confirm_dca,    pattern=r"^confirm_dca:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_dca,     pattern=r"^cancel_dca:"))
    app.add_handler(CallbackQueryHandler(cb_confirm_rtrade, pattern=r"^confirm_rtrade:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_rtrade,  pattern=r"^cancel_rtrade:"))
    app.add_handler(CallbackQueryHandler(cb_improve, pattern=r"^improve:"))
    app.add_handler(CallbackQueryHandler(cb_cancel,  pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_yes_reply))

    return app


# ---------------------------------------------------------------------------
# Text reply handler (legacy YES TICKER fallback)
# ---------------------------------------------------------------------------

async def handle_yes_reply(update, context):
    user = update.effective_user
    if not user or not github_store.is_authorized(user.id):
        return
    text  = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        return
    if parts[0].upper() != "YES":
        return
    ticker  = parts[1].upper()
    user_id = user.id
    logger.info(f"handle_yes_reply: ticker={ticker} user={user_id}")
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
            continue
        if pending.get("reverse", False):
            continue
        matching = (uid, tid, pending)
        break
    if not matching:
        return
    uid, tid, pending = matching
    hit        = pending["hit"]
    pricing    = pending["pricing"]
    trade_type = pending.get("trade_type", "dca")
    logger.info(f"handle_yes_reply: matched pending {tid} type={trade_type}")
    schwab = _get_schwab_for_user(context, user_id)
    try:
        order_payload = orders.build_itm_conversion_order(hit, pricing)
    except Exception as e:
        logger.exception("order build failed")
        await update.message.reply_text(f"Order build failed: {type(e).__name__}: {e}")
        return
    status_msg = await update.message.reply_text(
        f"Submitting {ticker} DCA collar at {pricing['apy']:.1f}% APY...")
    loop = asyncio.get_running_loop()
    try:
        order_id = await loop.run_in_executor(None, schwab.place_order, order_payload)
        logger.info(f"handle_yes_reply: order placed id={order_id}")
    except Exception as e:
        logger.exception("place_order failed")
        await _edit_robust(status_msg, f"Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return
    del _PENDING_TRADES[(uid, tid)]
    _ACTIVE_ORDERS[user_id] = {
        "order_id": order_id, "hit": hit, "pricing": pricing,
        "walk_step": pending["walk_step"], "trade_type": trade_type,
    }
    await _edit_robust(status_msg,
        f"Order *{order_id}* submitted · {ticker} {trade_type.upper()}\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY: *{pricing['apy']:.1f}%*\nMonitoring (30s)...")
    asyncio.create_task(monitor_order(context, user_id, order_id, status_msg))
