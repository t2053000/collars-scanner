"""
bot/helpers.py

Low-level building blocks shared across every command and callback:
- authorization decorators
- per-user Schwab client lookup
- periodic Schwab token persistence to GitHub
- Telegram send/edit wrappers that fall back to plain text if Markdown
  parsing fails
"""
import logging
import os
import time
from functools import wraps
from pathlib import Path

from telegram.constants import ParseMode
from telegram.error import BadRequest

import github_store

from . import state
from .state import TG_MAX_LEN, TOKEN_SAVE_INTERVAL

logger = logging.getLogger(__name__)


def _maybe_save_token(primary_uid: int):
    """Save primary token to GitHub if >1 hour since last save."""
    if time.time() - state._LAST_TOKEN_SAVE < TOKEN_SAVE_INTERVAL:
        return
    try:
        token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))
        if token_path.exists():
            github_store.save_schwab_token(primary_uid, token_path.read_text())
            state._LAST_TOKEN_SAVE = time.time()
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
