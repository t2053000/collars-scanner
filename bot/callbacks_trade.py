"""
bot/callbacks_trade.py

Inline-button callbacks for the three trade-card flavors: ITM, DCA, and
reverse ITM. Each "confirm" handler prices the trade, places the order,
and kicks off background monitoring; each "cancel" just drops the
pending trade.
"""
import asyncio
import logging
import time

import orders

from .helpers import _get_schwab_for_user, _edit_robust, authorized_callback
from .state import _PENDING_TRADES, _ACTIVE_ORDERS
from .trade_cards import _format_itm_card_text, _build_itm_card_keyboard
from .order_monitor import monitor_order, monitor_rtrade_order

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ITM confirm / cancel / refresh callbacks
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_trade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    parts    = query.data.split(":")
    trade_id = parts[1]
    walk_step = int(parts[2]) if len(parts) > 2 else 0

    await query.answer()

    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await query.answer("Card expired. Re-run /itm.", show_alert=True)
        return

    hit = pending["hit"]
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_trade: pricing failed")
        await query.answer(f"Pricing failed: {e}", show_alert=True)
        return

    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_itm_conversion_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_trade: order placed id={order_id} ticker={hit['ticker']}")
    except Exception as e:
        logger.exception("cb_confirm_trade: place_order failed")
        await query.answer(f"Order rejected: {type(e).__name__}: {str(e)[:120]}", show_alert=True)
        return

    # update counters on the living card
    pending["submitted"] = pending.get("submitted", 0) + 1
    _PENDING_TRADES[(user_id, trade_id)] = pending

    new_text = _format_itm_card_text(hit, pending["submitted"], pending.get("filled", 0))
    new_kb   = _build_itm_card_keyboard(trade_id, hit, pending["submitted"], pending.get("filled", 0))
    await _edit_robust(query.message, new_text, reply_markup=new_kb)

    _ACTIVE_ORDERS[order_id] = {
        "order_id":   order_id,
        "user_id":    user_id,
        "hit":        hit,
        "pricing":    pricing,
        "walk_step":  walk_step,
        "trade_type": "itm",
        "trade_id":   trade_id,          # needed so monitor can update the card
    }
    # still start the short monitor (it will only bump the filled counter)
    asyncio.create_task(monitor_order(context, user_id, order_id, query.message))


@authorized_callback
async def cb_cancel_trade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    await query.answer("Cancelled.")
    _PENDING_TRADES.pop((user_id, trade_id), None)
    await _edit_robust(query.message, "Trade cancelled.")


@authorized_callback
async def cb_refresh_itm(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":")[1]

    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await query.answer("Card expired. Re-run /itm.", show_alert=True)
        return

    await query.answer("Refreshing…")

    schwab  = _get_schwab_for_user(context, user_id)
    scanner = context.application.bot_data["itm_scanner"]
    loop    = asyncio.get_running_loop()
    ticker  = pending["hit"]["ticker"]

    try:
        result = await loop.run_in_executor(None, scanner.scan_ticker, ticker)
        hits = result[0] if isinstance(result, tuple) else result

        target_exp    = pending["hit"]["exp_date"]
        target_strike = pending["hit"]["strike"]
        fresh = next(
            (h for h in hits
             if h["exp_date"] == target_exp and abs(h["strike"] - target_strike) < 0.01),
            None
        )
        if not fresh:
            await query.answer("No longer available at that strike/exp", show_alert=True)
            return

        # keep the counters
        submitted = pending.get("submitted", 0)
        filled    = pending.get("filled", 0)
        pending["hit"] = fresh
        _PENDING_TRADES[(user_id, trade_id)] = pending

        new_text = _format_itm_card_text(fresh, submitted, filled)
        new_kb   = _build_itm_card_keyboard(trade_id, fresh, submitted, filled)
        await _edit_robust(query.message, new_text, reply_markup=new_kb)

    except Exception as e:
        logger.exception("cb_refresh_itm failed")
        await query.answer(f"Refresh failed: {type(e).__name__}", show_alert=True)


# ---------------------------------------------------------------------------
# DCA confirm / cancel callbacks
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_dca(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    logger.info(f"cb_confirm_dca FIRED user={user_id} trade_id={trade_id}")
    await query.answer()
    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await _edit_robust(query.message, "Trade expired. Re-run /dca.")
        return
    hit       = pending["hit"]
    walk_step = pending["walk_step"]
    try:
        pricing = orders.compute_legs_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_dca: pricing failed")
        await _edit_robust(query.message, f"Pricing failed: {type(e).__name__}: {e}")
        return
    await _edit_robust(query.message, f"Submitting {hit['ticker']} DCA @ {pricing['apy']:.1f}% APY...")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_itm_conversion_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_dca: order placed id={order_id} ticker={hit['ticker']}")
    except Exception as e:
        logger.exception("cb_confirm_dca: place_order failed")
        await _edit_robust(query.message, f"Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return
    del _PENDING_TRADES[(user_id, trade_id)]
    _ACTIVE_ORDERS[order_id] = {
        "order_id": order_id, "user_id": user_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "dca",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {hit['ticker']} DCA\n"
        f"Limit: ${pricing['call_limit']:.2f} sell / ${pricing['put_limit']:.2f} buy\n"
        f"APY: *{pricing['apy']:.1f}%*\nMonitoring (8s)...")
    asyncio.create_task(monitor_order(context, user_id, order_id, query.message))


@authorized_callback
async def cb_cancel_dca(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    await query.answer("Cancelled.")
    _PENDING_TRADES.pop((user_id, trade_id), None)
    await _edit_robust(query.message, "Trade cancelled.")


# ---------------------------------------------------------------------------
# Reverse ITM confirm / cancel callbacks
# ---------------------------------------------------------------------------

@authorized_callback
async def cb_confirm_rtrade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    logger.info(f"cb_confirm_rtrade FIRED user={user_id} trade_id={trade_id}")
    await query.answer()
    pending = _PENDING_TRADES.get((user_id, trade_id))
    if not pending or time.time() > pending.get("expires_at", 0):
        await _edit_robust(query.message, "Trade expired. Re-run /itm r.")
        return
    hit       = pending["hit"]
    walk_step = pending["walk_step"]
    ticker    = hit["ticker"]
    try:
        pricing = orders.compute_reverse_pricing(hit, walk_step=walk_step)
    except Exception as e:
        logger.exception("cb_confirm_rtrade: pricing failed")
        await _edit_robust(query.message, f"Pricing failed: {type(e).__name__}: {e}")
        return
    await _edit_robust(query.message, f"Submitting {ticker} reverse ITM options @ {pricing['apy']:.1f}% APY...")
    schwab = _get_schwab_for_user(context, user_id)
    loop   = asyncio.get_running_loop()
    try:
        payload  = orders.build_reverse_itm_order(hit, pricing)
        order_id = await loop.run_in_executor(None, schwab.place_order, payload)
        logger.info(f"cb_confirm_rtrade: order placed id={order_id} ticker={ticker}")
    except Exception as e:
        logger.exception("cb_confirm_rtrade: place_order failed")
        await _edit_robust(query.message, f"Order rejected: {type(e).__name__}: {str(e)[:300]}")
        return
    del _PENDING_TRADES[(user_id, trade_id)]
    _ACTIVE_ORDERS[order_id] = {
        "order_id": order_id, "user_id": user_id, "hit": hit, "pricing": pricing,
        "walk_step": walk_step, "trade_type": "itm_r",
    }
    await _edit_robust(query.message,
        f"Order *{order_id}* submitted · {ticker} options\n"
        f"SELL put + BUY call · NET CREDIT ${pricing['net_credit']:.2f}\n"
        f"Monitoring for fill (8s)...")
    asyncio.create_task(monitor_rtrade_order(context, user_id, order_id, query.message, ticker))


@authorized_callback
async def cb_cancel_rtrade(update, context):
    query    = update.callback_query
    user_id  = update.effective_user.id
    trade_id = query.data.split(":", 1)[1]
    await query.answer("Cancelled.")
    _PENDING_TRADES.pop((user_id, trade_id), None)
    await _edit_robust(query.message, "Trade cancelled.")
