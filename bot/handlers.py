"""
bot/handlers.py

Legacy fallback: replying "YES TICKER" to a DCA card confirms it by
text instead of tapping the button.
"""
import asyncio
import logging
import time

import github_store
import orders

from .helpers import _get_schwab_for_user, _edit_robust
from .state import _PENDING_TRADES, _ACTIVE_ORDERS
from .order_monitor import monitor_order

logger = logging.getLogger(__name__)


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
