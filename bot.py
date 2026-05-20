"""
bot.py
Telegram bot — collars, spreads, deep-ITM, DCA, CSP, ITM, RITM, token refresh.
"""

import asyncio
import logging
from collections import Counter, deque
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

import github_store
from scanner  import CollarScanner
from spreads  import SpreadScanner
from deepcall import DeepCallScanner, clamp_cushion, DEFAULT_CUSHION_PCT, MIN_CUSHION_PCT, MAX_CUSHION_PCT
from dca      import DcaScanner
from csp      import CspScanner
from itm      import ItmScanner
from ritm     import RitmScanner

logger = logging.getLogger(__name__)

SCAN_CONCURRENCY = 5
TG_MAX_LEN = 4000
_LAST_ERRORS: deque = deque(maxlen=30)


def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not github_store.is_authorized(user.id):
            await update.message.reply_text(
                f"❌ You are not authorized.\nYour Telegram ID: `{user.id if user else '?'}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        return await func(update, context)
    return wrapper


def _truncate(text, limit=TG_MAX_LEN):
    if len(text) <= limit:
        return text
    return text[:limit - 30] + "\n\n_(truncated…)_"


async def _send_robust(send_callable, text):
    safe = _truncate(text)
    try:
        await send_callable(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest as e:
        logger.warning(f"Telegram BadRequest with markdown: {e}")
    try:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        plain = _truncate(plain)
        await send_callable(plain)
    except BadRequest as e:
        logger.error(f"Telegram BadRequest even on plain text: {e}")


async def _edit_robust(message, text):
    safe = _truncate(text)
    try:
        await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest as e:
        logger.warning(f"Edit BadRequest with markdown: {e}")
    try:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        plain = _truncate(plain)
        await message.edit_text(plain)
    except BadRequest as e:
        logger.error(f"Edit BadRequest even on plain text: {e}")


async def cmd_start(update, context):
    await update.message.reply_text(
        "👋 *Options Scanner Bot*\n\n"
        "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm`\n"
        "`/refresh_token` to refresh Schwab.\n"
        "Send `/help` for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update, context):
    await update.message.reply_text(
        "*Commands*\n"
        "`/scan` – collar scan\n"
        "`/spreads [TICKER]` – cheap vertical debit spreads\n"
        "`/deepcall [N]` – deep-ITM buy-write\n"
        "`/dca` – dividend collar arbitrage\n"
        "`/csp` – bull put credit spreads (OTM)\n"
        "`/itm` – ITM conversion (long stock + sell call + buy put)\n"
        "`/ritm` – reverse ITM conversion (short stock + buy call + sell put)\n"
        "`/list` `/add` `/remove` `/logs` `/whoami`\n"
        "`/refresh_token` `/submit_token`",
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
        await update.message.reply_text("_Watchlist is empty._", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        f"*Watchlist ({len(tickers)})*\n" + ", ".join(f"`{t}`" for t in tickers),
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def cmd_add(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/add AAPL TSLA`", parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text("_No recent errors._", parse_mode=ParseMode.MARKDOWN)
        return
    body = "\n".join(_LAST_ERRORS)
    if len(body) > 3800:
        body = body[-3800:]
    await update.message.reply_text(
        "*Recent error details (most recent last):*\n```\n" + body + "\n```",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _run_scan(update, context, scanner, label_emoji, format_summary_fn,
                    tickers_override=None, scan_kwargs=None, summary_kwargs=None):
    tickers = tickers_override if tickers_override is not None else github_store.get_tickers()
    if not tickers:
        await update.message.reply_text(
            "_Watchlist is empty._", parse_mode=ParseMode.MARKDOWN,
        )
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

    kwargs = dict(
        all_hits=all_hits,
        scanned=len(tickers),
        successful=successful,
        errors=errors,
    )
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
                            SpreadScanner.format_summary,
                            tickers_override=[sym])
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
                    f"⚠️ Cushion `{requested}` outside range "
                    f"`{MIN_CUSHION_PCT:g}–{MAX_CUSHION_PCT:g}` — using *{clamped:g}%*",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except ValueError:
            await update.message.reply_text(
                f"Usage: `/deepcall [N]` (default {DEFAULT_CUSHION_PCT:g})",
                parse_mode=ParseMode.MARKDOWN,
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
        await update.message.reply_text("_div_tickers.txt empty._", parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text("_div_tickers.txt empty._", parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text("_No tickers to scan._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                    tickers_override=combined)


@authorized_only
async def cmd_ritm(update, context):
    scanner = context.application.bot_data["ritm_scanner"]
    div_tickers = github_store.get_div_tickers()
    watchlist = github_store.get_tickers()
    combined = sorted(set(watchlist) | set(div_tickers.keys()))
    if not combined:
        await update.message.reply_text("_No tickers to scan._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    await _run_scan(update, context, scanner, "🔄", RitmScanner.format_summary,
                    tickers_override=combined)


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
            "Most common cause: auth code already expired. Try /refresh_token again."
        )
        return
    await update.message.reply_text("✅ Token refreshed! Try /scan to confirm.")


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
    return app
