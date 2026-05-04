"""
bot.py
Telegram bot — collars, spreads, deep-ITM, DCA, CSP.
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
                f"❌ You are not authorized.\nYour Telegram ID: `{user.id if user else '?'}`\n"
                "Ask the admin to add you to whitelist.txt in the GitHub repo.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        return await func(update, context)
    return wrapper


def _truncate(text: str, limit: int = TG_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 30] + "\n\n_(truncated…)_"


async def _send_robust(send_callable, text: str):
    safe = _truncate(text)
    try:
        await send_callable(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest as e:
        logger.warning(f"Telegram BadRequest with markdown: {e} — retrying plain text")
    try:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        plain = _truncate(plain)
        await send_callable(plain)
    except BadRequest as e:
        logger.error(f"Telegram BadRequest even on plain text: {e}")


async def _edit_robust(message, text: str):
    safe = _truncate(text)
    try:
        await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN)
        return
    except BadRequest as e:
        logger.warning(f"Edit BadRequest with markdown: {e} — retrying plain text")
    try:
        plain = safe.replace("*", "").replace("_", "").replace("`", "")
        plain = _truncate(plain)
        await message.edit_text(plain)
    except BadRequest as e:
        logger.error(f"Edit BadRequest even on plain text: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Options Scanner Bot*\n\n"
        "`/scan` – positive-edge collars\n"
        "`/spreads` – cheap bull-call & bear-put spreads\n"
        "`/deepcall [N]` – deep-ITM buy-writes\n"
        "`/dca` – dividend collar arbitrage\n"
        "`/csp` – bull put credit spreads\n"
        "Send `/help` for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands*\n"
        "`/scan` – collar scan\n"
        "`/spreads [TICKER]` – cheap vertical debit spreads\n"
        "`/deepcall [N]` – deep-ITM buy-write\n"
        "`/dca` – dividend collar arbitrage\n"
        "`/csp` – bull put credit spreads (Δ 0.80-0.85)\n"
        "`/list` – show tickers\n"
        "`/add  AAPL TSLA` – add tickers\n"
        "`/remove AAPL` – remove tickers\n"
        "`/logs` – recent errors\n"
        "`/whoami` – your Telegram ID",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"👤 `{u.id}`  —  {u.full_name}", parse_mode=ParseMode.MARKDOWN
    )


@authorized_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tickers = github_store.get_tickers()
    if not tickers:
        await update.message.reply_text("_Watchlist is empty._", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        f"*Watchlist ({len(tickers)})*\n" + ", ".join(f"`{t}`" for t in tickers),
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/add AAPL TSLA`", parse_mode=ParseMode.MARKDOWN)
        return
    lines = [github_store.add_ticker(t)[1] for t in context.args]
    await update.message.reply_text("\n".join(lines))


@authorized_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/remove AAPL`", parse_mode=ParseMode.MARKDOWN)
        return
    lines = [github_store.remove_ticker(t)[1] for t in context.args]
    await update.message.reply_text("\n".join(lines))


@authorized_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "_Watchlist is empty – add some tickers first._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await update.message.reply_text(
        f"{label_emoji} Scanning {len(tickers)} tickers…",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_running_loop()
    all_hits: list[dict] = []
    errors:   list[str]  = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    successful = 0
    debug_totals: Counter = Counter()
    scan_kwargs = scan_kwargs or {}

    async def scan_one(tk: str):
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
                full  = str(e)[:400].replace("\n", " ")
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
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner: CollarScanner = context.application.bot_data["collar_scanner"]
    await _run_scan(update, context, scanner, "🔎", CollarScanner.format_summary)


@authorized_only
async def cmd_spreads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner: SpreadScanner = context.application.bot_data["spread_scanner"]
    if context.args:
        sym = context.args[0].upper().strip()
        if sym.isalpha() and 1 <= len(sym) <= 6:
            await _run_scan(update, context, scanner, "💸",
                            SpreadScanner.format_summary,
                            tickers_override=[sym])
            return
    await _run_scan(update, context, scanner, "💸", SpreadScanner.format_summary)


@authorized_only
async def cmd_deepcall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner: DeepCallScanner = context.application.bot_data["deepcall_scanner"]

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
                f"Usage: `/deepcall [N]` (cushion%, default {DEFAULT_CUSHION_PCT:g})",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    await _run_scan(
        update, context, scanner, "🛡️", DeepCallScanner.format_summary,
        scan_kwargs={"cushion_pct": cushion_pct},
        summary_kwargs={"cushion_pct": cushion_pct},
    )


@authorized_only
async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner: DcaScanner = context.application.bot_data["dca_scanner"]
    div_tickers = github_store.get_div_tickers()
    if not div_tickers:
        await update.message.reply_text(
            "_div_tickers.txt is empty in the data repo. Add entries like_ `MO:Q`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    scanner.ticker_freqs = div_tickers
    tickers = sorted(div_tickers.keys())
    await _run_scan(
        update, context, scanner, "💰", DcaScanner.format_summary,
        tickers_override=tickers,
    )


@authorized_only
async def cmd_csp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner: CspScanner = context.application.bot_data["csp_scanner"]
    div_tickers = github_store.get_div_tickers()
    if not div_tickers:
        await update.message.reply_text(
            "_div_tickers.txt is empty in the data repo._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    tickers = sorted(div_tickers.keys())
    await _run_scan(
        update, context, scanner, "💵", CspScanner.format_summary,
        tickers_override=tickers,
    )


def build_app(telegram_token: str,
              collar_scanner: CollarScanner,
              spread_scanner: SpreadScanner,
              deepcall_scanner: DeepCallScanner,
              dca_scanner: DcaScanner,
              csp_scanner: CspScanner) -> Application:
    app = Application.builder().token(telegram_token).build()
    app.bot_data["collar_scanner"]   = collar_scanner
    app.bot_data["spread_scanner"]   = spread_scanner
    app.bot_data["deepcall_scanner"] = deepcall_scanner
    app.bot_data["dca_scanner"]      = dca_scanner
    app.bot_data["csp_scanner"]      = csp_scanner

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("whoami",    cmd_whoami))
    app.add_handler(CommandHandler("list",      cmd_list))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("scan",      cmd_scan))
    app.add_handler(CommandHandler("spreads",   cmd_spreads))
    app.add_handler(CommandHandler("deepcall",  cmd_deepcall))
    app.add_handler(CommandHandler("deepcalls", cmd_deepcall))
    app.add_handler(CommandHandler("dca",       cmd_dca))
    app.add_handler(CommandHandler("csp",       cmd_csp))
    app.add_handler(CommandHandler("logs",      cmd_logs))
    return app
