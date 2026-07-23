"""
bot/commands_basic.py

Simple, mostly stateless commands: onboarding, watchlist management,
and the recent-errors log.
"""
from telegram.constants import ParseMode

import github_store

from .helpers import authorized_only
from .state import _LAST_ERRORS


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
