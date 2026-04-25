"""
bot.py
Telegram bot — collars + token refresh.
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
        "`/scan` – find positive-edge collars\n"
        "`/refresh_token` – refresh the Schwab token\n"
        "Send `/help` for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands*\n"
        "`/scan` – scan watchlist for collars\n"
        "`/list` – show tickers\n"
        "`/add  AAPL TSLA` – add tickers\n"
        "`/remove AAPL` – remove tickers\n"
        "`/logs` – recent scan errors\n"
        "`/refresh_token` – start Schwab token refresh\n"
        "`/submit_token URL` – complete refresh\n"
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


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

@authorized_only
async def cmd_refresh_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schwab_client = context.application.bot_data["schwab_client"]
    auth_url = schwab_client.build_authorize_url()
    await update.message.reply_text(
        "🔐 *Schwab Token Refresh*\n\n"
        "1️⃣ Tap this URL to open Schwab login:\n"
        f"{auth_url}\n\n"
        "2️⃣ Log in with your Schwab brokerage account → click *Allow*\n\n"
        "3️⃣ Browser redirects to `https://127.0.0.1/?code=...` "
        "(page won't load — that's fine!)\n\n"
        "4️⃣ Copy the *entire* redirected URL from your browser address bar\n\n"
        "5️⃣ Send it back to me as:\n"
        "`/submit_token <paste URL>`",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


@authorized_only
async def cmd_submit_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schwab_client = context.application.bot_data["schwab_client"]
    text = update.message.text or ""
    # Strip the command itself; everything after the first whitespace is the URL
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: `/submit_token https://127.0.0.1/?code=...`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    redirect_url = parts[1].strip()

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, schwab_client.exchange_code_for_token, redirect_url
        )
    except Exception as e:
        logger.exception("token exchange failed")
        await update.message.reply_text(
            f"❌ Token exchange failed:\n`{type(e).__name__}: {str(e)[:300]}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "✅ *Token refreshed successfully!*\n"
        "Try `/scan` to confirm everything works.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
def build_app(telegram_token: str, scanner: CollarScanner, schwab_client) -> Application:
    app = Application.builder().token(telegram_token).build()
    app.bot_data["scanner"]       = scanner
    app.bot_data["schwab_client"] = schwab_client

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("whoami",        cmd_whoami))
    app.add_handler(CommandHandler("list",          cmd_list))
    app.add_handler(CommandHandler("add",           cmd_add))
    app.add_handler(CommandHandler("remove",        cmd_remove))
    app.add_handler(CommandHandler("scan",          cmd_scan))
    app.add_handler(CommandHandler("logs",          cmd_logs))
    app.add_handler(CommandHandler("refresh_token", cmd_refresh_token))
    app.add_handler(CommandHandler("submit_token",  cmd_submit_token))
    return app
