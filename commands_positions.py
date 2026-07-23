"""
bot/commands_positions.py

/positions — projected P/L for option positions expiring this Friday.
"""
import asyncio
import logging

import github_store
from positions import compute_positions

from .helpers import authorized_only, _get_schwab_for_user, _edit_robust

logger = logging.getLogger(__name__)


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
