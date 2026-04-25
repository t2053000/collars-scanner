"""
bot.py
Telegram bot — command handlers + app factory.
"""

import asyncio
import logging
from collections import deque
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import github_store
from scanner import CollarScanner

logger = logging.getLogger(__name__)

SCAN_CONCURRENCY = 5

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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Collar Scanner Bot*\n\n"
        "Use `/scan` to find positive-edge collars across your watchlist.\n"
        "Send `/help` to see all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands*\n"
        "`/scan` – scan all tickers\n"
        "`/list` – show current tickers\n"
        "`/add  AAPL TSLA` – add tickers\n"
        "`/remove AAPL` – remove tickers\n"
        "`/logs` – show recent scan errors\n"
        "`/whoami` – show your Telegram ID",
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
        await update.message.reply_text("Usage: `/add AAPL TSLA ...`", parse_mode=ParseMode.MARKDOWN)
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


@authorized_only
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tickers = github_store.get_tickers()
    if not tickers:
        await update.message.reply_text("_Watchlist is empty – add some tickers first._",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    status_msg = await update.message.reply_text(
        f"🔎 Scanning {len(tickers)} tickers…", parse_mode=ParseMode.MARKDOWN
    )

    scanner: CollarScanner = context.application.bot_data["scanner"]
    loop = asyncio.get_running_loop()
    all_hits: list[dict] = []
    errors:   list[str]  = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    successful = 0

    async def scan_one(tk: str):
        nonlocal successful
        async with sem:
            try:
                hits = await loop.run_in_executor(None, scanner.scan_ticker, tk)
                all_hits.extend(hits)
                ok = True
            except Exception as e:
                logger.exception(f"scan error for {tk}")
                err_type = type(e).__name__
                short    = str(e)[:120].replace("\n", " ")
                full     = str(e)[:400].replace("\n", " ")
                errors.append(f"{tk}: {err_type} – {short}")
                _LAST_ERRORS.append(f"{tk}: {err_type} – {full}")
                ok = False

        if ok:
            successful += 1

    await asyncio.gather(*(scan_one(t) for t in tickers))

    messages = CollarScanner.format_summary(
        all_hits=all_hits,
        scanned=len(tickers),
        successful=successful,
        errors=errors,
    )
    await status_msg.edit_text(messages[0], parse_mode=ParseMode.MARKDOWN)
    for extra in messages[1:]:
        await update.message.reply_text(extra, parse_mode=ParseMode.MARKDOWN)


def build_app(telegram_token: str, scanner: CollarScanner) -> Application:
    app = Application.builder().token(telegram_token).build()
    app.bot_data["scanner"] = scanner

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("logs",   cmd_logs))
    return app
