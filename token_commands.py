"""
bot/token_commands.py

/refresh_token and /submit_token — the manual Schwab OAuth refresh flow
(Schwab auth codes expire in ~10 seconds, so this is a two-step dance
the user has to move through fast).
"""
import asyncio
import logging

import github_store

from .helpers import authorized_only, _get_schwab_for_user

logger = logging.getLogger(__name__)


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
