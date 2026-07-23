"""
bot.py
Telegram bot — scanners + ITM/DCA trade execution with improve/cancel flow.
Heavy logging on trade flow for debugging.
"""

import asyncio
import os
from pathlib import Path
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

SCAN_CONCURRENCY = 12
TICKER_BLACKLIST = {"VIVO", "GRRR"}
TG_MAX_LEN = 4000
_LAST_ERRORS: deque = deque(maxlen=30)

_PENDING_TRADES: dict = {}
PENDING_TIMEOUT_SEC = 60
_ACTIVE_ORDERS: dict = {}
_ITMT_STOP: set = set()
_ITMT_RUNNING: set = set()
_LAST_TOKEN_SAVE: float = 0.0
TOKEN_SAVE_INTERVAL = 3600
MAX_WALK_STEPS=8

MAX_TRADE_BUTTONS = 20


def _maybe_save_token(primary_uid: int):
    """Save primary token to GitHub if >1 hour since last save."""
    global _LAST_TOKEN_SAVE
    if time.time() - _LAST_TOKEN_SAVE < TOKEN_SAVE_INTERVAL:
        return
    try:
        token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))
        if token_path.exists():
            github_store.save_schwab_token(primary_uid, token_path.read_text())
            _LAST_TOKEN_SAVE = time.time()
            logger.info("Periodic token save to GitHub")
    except Exception as e:
        logger.debug(f"Token save skipped: {e}")


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
        raw   = await loop.run_in_executor(None, schwab.get_positions)
        fills = await loop.run_in_executor(None, github_store.get_fills, 90)
        text  = await loop.run_in_executor(None, compute_positions, raw, fills)
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
    tickers = [t for t in tickers if t not in TICKER_BLACKLIST]
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

    primary_uid = context.application.bot_data.get("primary_user_id", 0)
    _maybe_save_token(primary_uid)

    if hits_with_buttons and all_hits:
        if scanner_key == "itm":
            all_hits.sort(key=lambda r: r.get("locked_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_itm_trade_button(update, context, hit)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"trade button send failed for {hit.get('ticker')}: {e}")

        elif scanner_key == "itm_r":
            all_hits.sort(key=lambda r: r.get("locked_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_rtrade_button(update, context, hit)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"rtrade button send failed for {hit.get('ticker')}: {e}")

        elif scanner_key == "dca":
            all_hits.sort(key=lambda r: r.get("score_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_dca_trade_button(update, context, hit)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"dca button send failed for {hit.get('ticker')}: {e}")


# ---------------------------------------------------------------------------
# Trade buttons
# ---------------------------------------------------------------------------

def _format_itm_card_text(hit: dict, submitted: int = 0, filled: int = 0) -> str:
    apy = hit.get("locked_apy", 0)
    return (
        f"🔒 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
        f"Strike ${hit['strike']:g} · Net credit ${hit.get('net_credit', 0):.2f}/sh\n"
        f"💳 Pay ${hit.get('primary_debit', 0):.2f}/sh → *{apy:.1f}% APY*\n"
        f"OI {hit['call_oi']}/{hit['put_oi']} · Locked ${hit.get('locked_total', 0):.0f}"
    )


def _build_itm_card_keyboard(trade_id: str, hit: dict, submitted: int, filled: int) -> InlineKeyboardMarkup:
    buttons = []
    seen_apys = set()

    for ws in range(MAX_WALK_STEPS):
        try:
            p = orders.compute_legs_pricing(hit, walk_step=ws)
            apy = p["apy"]
            if apy < orders.MIN_APY_FLOOR_PCT:
                continue
            rounded = round(apy, 1)
            if rounded in seen_apys:
                continue
            seen_apys.add(rounded)

            emoji = "📈" if ws == 0 else "📊" if ws <= 3 else "📉"
            buttons.append(InlineKeyboardButton(
                f"{emoji} {apy:.0f}%",
                callback_data=f"confirm_trade:{trade_id}:{ws}"
            ))
        except Exception:
            pass

    if not buttons:
        apy = hit.get("locked_apy", 0)
        buttons.append(InlineKeyboardButton(
            f"✅ {apy:.0f}%", callback_data=f"confirm_trade:{trade_id}:0"
        ))

    status = f"📤{submitted} ✅{filled}"
    action_row = [
        InlineKeyboardButton(status, callback_data="noop"),
        InlineKeyboardButton("🔄", callback_data=f"refresh_itm:{trade_id}"),
    ]

    rows = []
    for i in range(0, len(buttons), 4):
        rows.append(buttons[i:i + 4])
    rows.append(action_row)
    return InlineKeyboardMarkup(rows)

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
        "submitted":  0,
        "filled":     0,
    }

    summary  = _format_itm_card_text(hit, 0, 0)
    keyboard = _build_itm_card_keyboard(trade_id, hit, 0, 0)

    try:
        msg = await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except BadRequest as e:
        logger.warning(f"itm trade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            msg = await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"itm trade button failed even plain: {e2}")
            return

    # store message identity so we can edit it later
    _PENDING_TRADES[(user_id, trade_id)]["message_id"] = msg.message_id
    _PENDING_TRADES[(user_id, trade_id)]["chat_id"]    = msg.chat_id


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
    hiv_tickers = github_store.get_latest_hiv_tickers()
    tickers     = hiv_tickers if hiv_tickers else github_store.get_tickers()
    source      = "Finviz"

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
    tickers = github_store.get_tickers()
        

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
@authorized_callback
async def cb_confirm_trade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    parts    = query.data.split(":")
    trade_id = parts[1]
    walk_step = int(parts[2]) if len(parts) > 2 else 0

    await query.answer()

    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await query.answer("Card expired. Re-run /itm.", show_alert=True)
        return

    hit = pending["hit"]
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_trade: pricing failed")
        await query.answer(f"Pricing failed: {e}", show_alert=True)
        return

    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_itm_conversion_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_trade: order placed id={order_id} ticker={hit['ticker']}")
    except Exception as e:
        logger.exception("cb_confirm_trade: place_order failed")
        await query.answer(f"Order rejected: {type(e).__name__}: {str(e)[:120]}", show_alert=True)
        return

    # update counters on the living card
    pending["submitted"] = pending.get("submitted", 0) + 1
    _PENDING_TRADES[(user_id, trade_id)] = pending

    new_text = _format_itm_card_text(hit, pending["submitted"], pending.get("filled", 0))
    new_kb   = _build_itm_card_keyboard(trade_id, hit, pending["submitted"], pending.get("filled", 0))
    await _edit_robust(query.message, new_text, reply_markup=new_kb)

    _ACTIVE_ORDERS[order_id] = {
        "order_id":   order_id,
        "user_id":    user_id,
        "hit":        hit,
        "pricing":    pricing,
        "walk_step":  walk_step,
        "trade_type": "itm",
        "trade_id":   trade_id,          # needed so monitor can update the card
    }
    # still start the short monitor (it will only bump the filled counter)
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
    _ACTIVE_ORDERS[order_id] = {
        "order_id": order_id, "user_id": user_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "dca",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {hit['ticker']} DCA\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY: *{pricing['apy']:.1f}%*\nMonitoring (8s)...")
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
    _ACTIVE_ORDERS[order_id] = {
        "order_id": order_id, "user_id": user_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "itm_r",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {ticker} options\n"
        f"SELL put + BUY call · NET CREDIT ${pricing['net_credit']:.2f}\n"
        f"Monitoring for fill (8s)...")
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
    while time.time() - start < 8:
        await asyncio.sleep(2)
        try:
            status     = await loop.run_in_executor(None, schwab.get_order_status, order_id)
            status_str = status.get("status", "UNKNOWN")
            logger.info(f"monitor_order: order={order_id} status={status_str}")
            if status_str == "FILLED":
                filled = True
                break
            if status_str in ("CANCELED", "REJECTED", "EXPIRED"):
                active = _ACTIVE_ORDERS.pop(order_id, None)
                tkr    = active["hit"]["ticker"] if active else "?"
                await _edit_robust(status_msg, f"Order {order_id} for {tkr} ended: {status_str}")
                return
        except Exception as e:
            logger.warning(f"order status poll failed: {e}")
            continue

    if filled:
        active = _ACTIVE_ORDERS.pop(order_id, None)
        if not active:
            return

        # bump filled counter on the original card if we still have it
        trade_id = active.get("trade_id")
        if trade_id:
            key = (user_id, trade_id)
            pending = _PENDING_TRADES.get(key)
            if pending:
                pending["filled"] = pending.get("filled", 0) + 1
                _PENDING_TRADES[key] = pending
                new_text = _format_itm_card_text(
                    pending["hit"],
                    pending.get("submitted", 0),
                    pending["filled"]
                )
                new_kb = _build_itm_card_keyboard(
                    trade_id,
                    pending["hit"],
                    pending.get("submitted", 0),
                    pending["filled"]
                )
                try:
                    await _edit_robust(status_msg, new_text, reply_markup=new_kb)
                except Exception as e:
                    logger.warning(f"could not update card on fill: {e}")

        # still save the fill record
        sources = github_store.get_ticker_sources()
        github_store.save_fill({
            "ticker": active["hit"]["ticker"],
            "strike": active["hit"].get("strike"),
            "exp": active["hit"].get("exp_date"),
            "dte": active["hit"].get("dte"),
            "apy": active["pricing"].get("apy"),
            "cost": active["hit"].get("spot", 0) * 100,
            "order_id": order_id,
            "source": "manual",
            "scan_source": sources.get(active["hit"]["ticker"], {}).get("scan_code", "unknown"),
        })
        return

    # not filled after 8s — offer improve / cancel
    active = _ACTIVE_ORDERS.get(order_id)
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
        f"Order {order_id} for *{hit['ticker']}* not filled after 8s.\n"
        f"Limit: ${active['pricing']['call_limit']:.2f} / ${active['pricing']['put_limit']:.2f}\n"
        f"APY: {active['pricing']['apy']:.1f}%{floor_note}",
        reply_markup=InlineKeyboardMarkup(buttons))


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
    active = _ACTIVE_ORDERS.get(order_id)
    if not active:
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
        _ACTIVE_ORDERS.pop(order_id, None)
        return
    try:
        payload      = orders.build_itm_conversion_order(hit, new_pricing)
        new_order_id = await loop.run_in_executor(None, schwab.place_order, payload)
    except Exception as e:
        logger.exception("improve resubmit failed")
        await query.message.reply_text(f"Resubmit failed: {type(e).__name__}: {e}")
        _ACTIVE_ORDERS.pop(order_id, None)
        return
    status_msg = await query.message.reply_text(
        f"Retry #{new_walk}: order *{new_order_id}* @ {new_pricing['apy']:.1f}% APY\nMonitoring (8s)...",
        parse_mode=ParseMode.MARKDOWN)
    _ACTIVE_ORDERS.pop(order_id, None)
    _ACTIVE_ORDERS[new_order_id] = {
        "order_id": new_order_id, "user_id": user_id, "hit": hit, "pricing": new_pricing,
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
    active = _ACTIVE_ORDERS.get(order_id)
    if not active:
        await query.message.reply_text("No active order.")
        return
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, schwab.cancel_order, order_id)
        await query.message.reply_text(f"Order {order_id} cancelled.")
    except Exception as e:
        await query.message.reply_text(f"Cancel failed: {e}")
    _ACTIVE_ORDERS.pop(order_id, None)


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



# ── ITMT configuration ────────────────────────────────────────────────
ITMT_TOP_N         = 3
ITMT_FILL_WAIT_SEC = 4
ITMT_POLL_SEC      = 1
ITMT_DEFAULT_APY   = 35.0
ITMT_DEFAULT_MIN   = 180     # minutes (3 hours)


@authorized_only
async def cmd_itmt(update, context):
    """
    /itmt 6000          — $6k budget, 35% APY, 1 hour
    /itmt 6000 40       — $6k budget, 40% APY min
    /itmt 6000 35 120   — $6k budget, 35% APY, 2 hours
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/itmt <budget> [min_apy] [timeout_min]`\n"
            "Example: `/itmt 6000` — $6k, 35% APY, 1 hour",
            parse_mode=ParseMode.MARKDOWN)
        return

    user_id = update.effective_user.id
    if user_id in _ITMT_RUNNING:
        _ITMT_STOP.add(user_id)
        await update.message.reply_text("⏹ Stopping previous ITMT... restarting.")
        await asyncio.sleep(15)
        _ITMT_STOP.discard(user_id)

    budget      = float(args[0])
    min_apy     = float(args[1]) if len(args) > 1 else ITMT_DEFAULT_APY
    timeout_min = float(args[2]) if len(args) > 2 else ITMT_DEFAULT_MIN
    schwab  = _get_schwab_for_user(context, user_id)
    scanner = context.application.bot_data["itm_scanner"]
    scanner.ticker_freqs = github_store.get_div_tickers()

    tickers = []
    ticker_sources = {}
    logger.info(f"ITMT: budget=${budget}, min_apy={min_apy}")

    status_msg = await update.message.reply_text(
        f"🤖 *ITMT started*\n"
        f"Budget: ${budget:,.0f} · APY ≥ {min_apy}% · Timeout: {timeout_min:.0f}min",
        parse_mode=ParseMode.MARKDOWN)

    loop      = asyncio.get_running_loop()
    _ITMT_STOP.discard(user_id)
    _ITMT_RUNNING.add(user_id)
    remaining = budget
    deadline  = time.time() + (timeout_min * 60)
    cycle     = 0
    fills     = []
    sem       = asyncio.Semaphore(SCAN_CONCURRENCY)

    try:
      while time.time() < deadline and remaining > 0 and user_id not in _ITMT_STOP:
        cycle += 1
        elapsed = time.time() - (deadline - timeout_min * 60)

        # Reload tickers every 10 cycles to pick up new ones
        if cycle == 1 or cycle % 10 == 0:
            try:
                hiv_tickers = await asyncio.wait_for(
                    loop.run_in_executor(None, github_store.get_latest_hiv_tickers),
                    timeout=30)
                new_list = hiv_tickers if hiv_tickers else await asyncio.wait_for(
                    loop.run_in_executor(None, github_store.get_tickers),
                    timeout=30)
                new_list = sorted(set(new_list) - TICKER_BLACKLIST)
                ticker_sources = await asyncio.wait_for(
                    loop.run_in_executor(None, github_store.get_ticker_sources),
                    timeout=30)
                new_list = sorted(new_list, key=lambda t: ticker_sources.get(t, {}).get("priority", 3))
                tickers = new_list
                p1 = sum(1 for t in tickers if ticker_sources.get(t, {}).get("priority") == 1)
                logger.info(f"ITMT: reloaded {len(tickers)} tickers ({p1} priority 1)")
            except Exception as e:
                logger.warning(f"ITMT: ticker reload failed ({e}) — keeping previous {len(tickers)} tickers")
                if not tickers:
                    await asyncio.sleep(10)
                    continue

        # ── scan ────────────────────────────────────────
        all_hits = []
        debug_totals = Counter()
        errors = 0

        async def scan_one(tk):
            nonlocal errors
            async with sem:
                try:
                    result = await loop.run_in_executor(
                        None, lambda t=tk: scanner.scan_ticker(t))
                    if isinstance(result, tuple):
                        hits, debug = result
                        debug_totals.update(debug)
                    else:
                        hits = result
                    all_hits.extend(hits)
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        logger.error(f"ITMT scan error {tk}: {e}")

        scan_start = time.time()
        await asyncio.gather(*(scan_one(t) for t in tickers))
        scan_dur = time.time() - scan_start
        logger.info(f"ITMT cycle {cycle}: scan done in {scan_dur:.1f}s — {len(all_hits)} hits, {errors} errors")

        # ── filter & rank ───────────────────────────────
        candidates = []
        for h in all_hits:
            if h["spot"] * 100 > remaining:
                continue
            p = orders.compute_legs_pricing(h, walk_step=0)
            if p["apy"] >= min_apy:
                candidates.append((h, p))
        candidates.sort(key=lambda x: x[1]["apy"], reverse=True)
        logger.info(f"ITMT cycle {cycle}: {len(candidates)} qualified (budget ${remaining:,.0f}, min_apy {min_apy}%)")
        if candidates:
            for i, (h, p) in enumerate(candidates[:5]):
                logger.info(f"  #{i+1} {h['ticker']} spot=${h['spot']:.2f} strike=${h['strike']:g} APY={p['apy']:.1f}% cost=${h['spot']*100:.0f}")

        await _edit_robust(status_msg,
            f"🤖 *ITMT cycle {cycle}*\n"
            f"Remaining: ${remaining:,.0f} · {elapsed:.0f}s elapsed\n"
            f"Hits: {len(all_hits)} · Qualified: {len(candidates)} · Errors: {errors}")

        if not candidates:
            if errors > len(tickers) * 0.5:
                wait = 30
                logger.info(f"ITMT cycle {cycle}: {errors} errors — backing off {wait}s")
            else:
                wait = 10
                logger.info(f"ITMT cycle {cycle}: no candidates — sleeping {wait}s")
            await asyncio.sleep(wait)
            continue

        top = candidates[:ITMT_TOP_N]

        # ── place all top-N in parallel ─────────────────
        placed = []
        for hit, pricing in top:
            try:
                payload  = orders.build_itm_conversion_order(hit, pricing)
                order_id = await loop.run_in_executor(
                    None, schwab.place_order, payload)
                placed.append((order_id, hit, pricing))
                logger.info(f"ITMT placed {order_id} · {hit['ticker']} "
                            f"APY={pricing['apy']:.1f}%")
            except Exception as e:
                logger.warning(f"ITMT place failed {hit['ticker']}: {e}")

        logger.info(f"ITMT cycle {cycle}: {len(placed)} orders placed")
        if not placed:
            logger.info(f"ITMT cycle {cycle}: all placements failed — sleeping 10s")
            await asyncio.sleep(10)
            continue

        summary = " / ".join(f"{h['ticker']} {p['apy']:.0f}%"
                             for _, h, p in placed)
        await _edit_robust(status_msg,
            f"🤖 *ITMT cycle {cycle}* — {len(placed)} orders live\n"
            f"{summary}\n"
            f"Polling {ITMT_FILL_WAIT_SEC}s for fill...")

        # ── poll for first fill ─────────────────────────
        winner = None
        live = list(placed)
        start = time.time()
        while time.time() - start < ITMT_FILL_WAIT_SEC and live:
            await asyncio.sleep(ITMT_POLL_SEC)
            still_live = []
            for oid, hit, pricing in live:
                try:
                    data   = await loop.run_in_executor(
                        None, schwab.get_order_status, oid)
                    status = data.get("status", "UNKNOWN")
                except Exception:
                    status = "UNKNOWN"
                if status == "FILLED":
                    winner = (oid, hit, pricing)
                    break
                if status not in ("CANCELED", "REJECTED", "EXPIRED"):
                    still_live.append((oid, hit, pricing))
            if winner:
                break
            live = still_live

        logger.info(f"ITMT cycle {cycle}: poll done — winner={'yes' if winner else 'no'}, {len(live)} still live")

        # ── cancel non-winners ──────────────────────────
        winner_oid = winner[0] if winner else None
        for oid, hit, pricing in placed:
            if oid != winner_oid:
                try:
                    await loop.run_in_executor(None, schwab.cancel_order, oid)
                except Exception:
                    pass

        # ── handle result ───────────────────────────────
        if winner:
            oid, hit, pricing = winner
            cost = hit["spot"] * 100
            remaining -= cost
            fills.append({
                "ticker": hit["ticker"], "strike": hit["strike"],
                "exp": hit["exp_date"], "apy": pricing["apy"],
                "cost": cost, "order_id": oid,
            })
            github_store.save_fill({
                "ticker": hit["ticker"], "strike": hit["strike"],
                "exp": hit["exp_date"], "dte": hit["dte"],
                "apy": pricing["apy"], "cost": cost,
                "order_id": oid, "source": "itmt",
                "scan_source": ticker_sources.get(hit["ticker"], {}).get("scan_code", "unknown"),
            })
            await _send_robust(update.message.reply_text,
                f"✅ *FILLED* — {hit['ticker']} · order {oid}\n"
                f"Strike ${hit['strike']:g} · {hit['exp_date']} "
                f"({hit['dte']}d)\n"
                f"APY: *{pricing['apy']:.1f}%* · Cost: ${cost:,.0f}\n"
                f"Remaining: ${remaining:,.0f}")

    finally:
        _ITMT_RUNNING.discard(user_id)
        global _LAST_TOKEN_SAVE
        _LAST_TOKEN_SAVE = 0  # force save, bypass 1hr throttle
        primary_uid = context.application.bot_data.get("primary_user_id", 0)
        _maybe_save_token(primary_uid)
        logger.info("ITMT exited — token saved")

    # ── final summary ───────────────────────────────────
    if fills:
        lines = [f"🤖 *ITMT COMPLETE* — {len(fills)} fill(s), "
                 f"${budget - remaining:,.0f} of ${budget:,.0f} deployed\n"]
        for f in fills:
            lines.append(f"✅ {f['ticker']} ${f['strike']:g} "
                         f"{f['exp']} APY={f['apy']:.1f}% "
                         f"${f['cost']:,.0f}")
        await _send_robust(update.message.reply_text, "\n".join(lines))
    else:
        await _edit_robust(status_msg,
            f"🤖 *ITMT COMPLETE* — no fills after {cycle} cycles.\n"
            f"Budget ${budget:,.0f} unallocated.")


@authorized_only
async def cmd_fills(update, context):
    """Show fill history, stats, and breakdown by scan source."""
    args = context.args or []
    days = int(args[0]) if args else 30
    fills = github_store.get_fills(days=days)
    if not fills:
        await update.message.reply_text(f"No fills in the last {days} days.")
        return
    total_cost = sum(f.get("cost", 0) for f in fills)
    weighted_apy = sum(f.get("apy", 0) * f.get("cost", 0) for f in fills)
    avg_apy = weighted_apy / total_cost if total_cost > 0 else 0

    lines = [f"📊 *{len(fills)} fills* last {days}d — ${total_cost:,.0f} deployed — wAPY {avg_apy:.1f}%"]

    # Breakdown by scan source
    from collections import defaultdict
    by_source = defaultdict(lambda: {"count": 0, "cost": 0, "weighted_apy": 0})
    for fl in fills:
        src = fl.get("scan_source", "unknown")
        by_source[src]["count"] += 1
        by_source[src]["cost"] += fl.get("cost", 0)
        by_source[src]["weighted_apy"] += fl.get("apy", 0) * fl.get("cost", 0)

    if any(s != "unknown" for s in by_source):
        lines.append("")
        lines.append("*By scan source:*")
        for src, data in sorted(by_source.items(), key=lambda x: -x[1]["cost"]):
            src_apy = data["weighted_apy"] / data["cost"] if data["cost"] > 0 else 0
            lines.append(f"  {src}: {data['count']} fills · ${data['cost']:,.0f} · wAPY {src_apy:.1f}%")

    lines.append("")
    lines.append("*Recent:*")
    for fl in fills[-10:]:
        mode = "🤖" if fl.get("source") == "itmt" else "👆"
        scan = fl.get("scan_source", "")
        scan_tag = f" [{scan[:8]}]" if scan and scan != "unknown" else ""
        lines.append(f"{mode} {fl.get('ticker', '?'):>5} ${fl.get('strike', 0):g} "
                     f"{fl.get('exp', '?')} {fl.get('apy', 0):.1f}%{scan_tag}")
    await _send_robust(update.message.reply_text, "\n".join(lines))


@authorized_only
async def cmd_stop(update, context):
    user_id = update.effective_user.id
    _ITMT_STOP.add(user_id)
    _ITMT_RUNNING.discard(user_id)
    await update.message.reply_text("Stopping ITMT after current cycle.")


@authorized_only
async def cmd_cancel_all(update, context):
    user_id = update.effective_user.id
    schwab = _get_schwab_for_user(context, user_id)
    if not schwab:
        await update.message.reply_text("No Schwab client available.")
        return
    loop = asyncio.get_running_loop()
    try:
        working = await loop.run_in_executor(None, schwab.get_working_orders)
    except Exception as e:
        await update.message.reply_text(f"Failed to fetch orders: {e}")
        return
    if not working:
        await update.message.reply_text("No open orders to cancel.")
        return
    cancelled, failed = 0, 0
    for o in working:
        oid = str(o.get("orderId", ""))
        try:
            await loop.run_in_executor(None, schwab.cancel_order, oid)
            cancelled += 1
        except Exception:
            failed += 1
    for oid in [k for k, v in _ACTIVE_ORDERS.items() if v.get("user_id") == user_id]:
        _ACTIVE_ORDERS.pop(oid, None)
    msg = f"Cancelled {cancelled} order(s)."
    if failed:
        msg += f" {failed} failed."
    await update.message.reply_text(msg)


def build_app(telegram_token, collar_scanner, spread_scanner, deepcall_scanner,
              dca_scanner, csp_scanner, itm_scanner, ritm_scanner,
              schwab_clients: dict, primary_user_id: int, itm_ibkr_scanner=None):
    app = Application.builder().token(telegram_token).concurrent_updates(True).build()
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

    app.add_handler(CommandHandler("i",              cmd_itm))
    app.add_handler(CommandHandler("c",              cmd_cancel_all))
    app.add_handler(CommandHandler("itmt",           cmd_itmt))
    app.add_handler(CommandHandler("itmm",           cmd_itmt))
    app.add_handler(CommandHandler("r",              cmd_refresh_token))
    app.add_handler(CommandHandler("s",              cmd_submit_token))
    app.add_handler(CommandHandler("stop",           cmd_stop))
    app.add_handler(CommandHandler("x",              cmd_stop))
    app.add_handler(CommandHandler("fills",          cmd_fills))
    app.add_handler(CommandHandler("f",              cmd_fills))

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
    _ACTIVE_ORDERS[order_id] = {
        "order_id": order_id, "user_id": user_id, "hit": hit, "pricing": pricing,
        "walk_step": pending["walk_step"], "trade_type": trade_type,
    }
    await _edit_robust(status_msg,
        f"Order *{order_id}* submitted · {ticker} {trade_type.upper()}\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY: *{pricing['apy']:.1f}%*\nMonitoring (8s)...")
    asyncio.create_task(monitor_order(context, user_id, order_id, status_msg))


