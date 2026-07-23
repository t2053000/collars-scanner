"""
bot/order_monitor.py

Background polling for orders after submission: waits briefly for a
fill, updates the originating trade card / status message, records
fills to GitHub, and offers "Improve" / "Cancel" if nothing filled in
time.
"""
import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import github_store
import orders

from .helpers import _get_schwab_for_user, _edit_robust, authorized_callback
from .state import _ACTIVE_ORDERS, _PENDING_TRADES
from .trade_cards import _format_itm_card_text, _build_itm_card_keyboard

logger = logging.getLogger(__name__)


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

    # not filled after 8s — expire (main card X handles cancel; no improve/cancel card)
    active = _ACTIVE_ORDERS.pop(order_id, None)
    if not active:
        return
    hit = active["hit"]
    await _edit_robust(status_msg,
        f"Order {order_id} for *{hit['ticker']}* not filled after 8s (expired).")


# ---------------------------------------------------------------------------
# Order monitoring — Reverse ITM
#
# NOTE: In the source bot.py, `cb_confirm_rtrade` schedules
# `monitor_rtrade_order(...)` as a background task, but that function
# was not defined anywhere in the file — it would raise a NameError the
# moment a reverse-ITM trade got confirmed. This stub restores the name
# so the module imports and the bot doesn't crash the event loop, and
# tells the user monitoring didn't happen rather than failing silently.
# It intentionally does NOT reimplement the missing fill-polling /
# auto-short-stock logic, since that's a real trading decision this
# refactor shouldn't invent on your behalf. See the summary in chat for
# details on restoring full behavior.
# ---------------------------------------------------------------------------

async def monitor_rtrade_order(context, user_id, order_id, status_msg, ticker):
    logger.error(
        f"monitor_rtrade_order: called for order={order_id} ticker={ticker}, "
        "but this function's body was missing from the source bot.py. "
        "No fill polling or follow-up short-stock order will happen."
    )
    await _edit_robust(
        status_msg,
        f"Order {order_id} for {ticker} submitted, but automatic fill "
        f"monitoring for reverse-ITM trades isn't implemented — check "
        f"Schwab directly for status."
    )


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