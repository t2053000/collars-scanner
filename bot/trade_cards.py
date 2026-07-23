"""
bot/trade_cards.py

Builds and sends the inline trade-confirmation cards (ITM, DCA, reverse
ITM) that appear under scan hits. The ITM card is "live" — it keeps a
row of APY buttons (one per walk step) and a submitted/filled counter
that gets updated in place as orders are placed and fill.
"""
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

import orders

from .state import _PENDING_TRADES, PENDING_TIMEOUT_SEC, MAX_WALK_STEPS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ITM card (supports repeated confirms at different walk steps)
# ---------------------------------------------------------------------------

def _format_itm_card_text(hit: dict, submitted: int = 0, filled: int = 0) -> str:
    apy = hit.get("locked_apy", 0)
    return (
        f"🔒 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
        f"Strike ${hit['strike']:g} · Net credit ${hit.get('net_credit', 0):.2f}/sh\n"
        f"💳 Pay ${hit.get('primary_debit', 0):.2f}/sh → *{apy:.1f}% APY*\n"
        f"OI {hit['call_oi']}/{hit['put_oi']} · Locked ${hit.get('locked_total', 0):.0f}"
    )


def _build_itm_card_keyboard(trade_id: str, hit: dict, submitted: int, filled: int) -> InlineKeyboardMarkup:
    buttons = []
    seen_apys = set()

    for ws in range(MAX_WALK_STEPS):
        try:
            p = orders.compute_legs_pricing(hit, walk_step=ws)
            apy = p["apy"]
            if apy < orders.MIN_APY_FLOOR_PCT:
                continue
            rounded = round(apy, 1)
            if rounded in seen_apys:
                continue
            seen_apys.add(rounded)

            emoji = "📈" if ws == 0 else "📊" if ws <= 3 else "📉"
            buttons.append(InlineKeyboardButton(
                f"{emoji} {apy:.0f}%",
                callback_data=f"confirm_trade:{trade_id}:{ws}"
            ))
        except Exception:
            pass

    if not buttons:
        apy = hit.get("locked_apy", 0)
        buttons.append(InlineKeyboardButton(
            f"✅ {apy:.0f}%", callback_data=f"confirm_trade:{trade_id}:0"
        ))

    status = f"📤{submitted} ✅{filled}"
    action_row = [
        InlineKeyboardButton(status, callback_data="noop"),
        InlineKeyboardButton("🔄", callback_data=f"refresh_itm:{trade_id}"),
        InlineKeyboardButton("❌", callback_data=f"cancel_ticker:{trade_id}"),
    ]

    rows = []
    for i in range(0, len(buttons), 4):
        rows.append(buttons[i:i + 4])
    rows.append(action_row)
    return InlineKeyboardMarkup(rows)


async def _send_itm_trade_button(update, context, hit):
    trade_id = uuid.uuid4().hex[:8]
    user_id  = update.effective_user.id
    apy      = hit.get("locked_apy", 0)
    logger.info(f"_send_itm_trade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={apy}")

    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit":        hit,
        "walk_step":  0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse":    False,
        "trade_type": "itm",
        "submitted":  0,
        "filled":     0,
    }

    summary  = _format_itm_card_text(hit, 0, 0)
    keyboard = _build_itm_card_keyboard(trade_id, hit, 0, 0)

    try:
        msg = await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except BadRequest as e:
        logger.warning(f"itm trade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            msg = await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"itm trade button failed even plain: {e2}")
            return

    # store message identity so we can edit it later
    _PENDING_TRADES[(user_id, trade_id)]["message_id"] = msg.message_id
    _PENDING_TRADES[(user_id, trade_id)]["chat_id"]    = msg.chat_id


# ---------------------------------------------------------------------------
# DCA card
# ---------------------------------------------------------------------------

async def _send_dca_trade_button(update, context, hit):
    trade_id   = uuid.uuid4().hex[:8]
    user_id    = update.effective_user.id
    apy        = hit.get("score_apy", 0)
    score_sign = "+" if hit.get("score_dollars", 0) >= 0 else ""
    logger.info(f"_send_dca_trade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={apy}")
    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit":        hit,
        "walk_step":  0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse":    False,
        "trade_type": "dca",
    }
    summary = (
        f"💰 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
        f"Strike ${hit['strike']:g} · Net premium ${hit.get('net_premium', 0):.2f}/sh\n"
        f"🛡️ Safety ${hit.get('safety_dollars', 0):.2f}/sh · "
        f"💸 Div +${hit.get('expected_div_dollars', 0):.2f}/sh\n"
        f"🎯 Score: *{score_sign}${hit.get('score_dollars', 0):.2f}/sh · {apy:.1f}% APY*\n"
        f"OI {hit['call_oi']}/{hit['put_oi']}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Confirm @ {apy:.1f}% APY", callback_data=f"confirm_dca:{trade_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_dca:{trade_id}"),
    ]])
    try:
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except BadRequest as e:
        logger.warning(f"dca trade button markdown failed: {e}")
        plain = summary.replace("*", "").replace("_", "").replace("`", "")
        try:
            await update.message.reply_text(plain, reply_markup=keyboard)
        except BadRequest as e2:
            logger.error(f"dca trade button failed even plain: {e2}")


# ---------------------------------------------------------------------------
# Reverse ITM card
# ---------------------------------------------------------------------------

async def _send_rtrade_button(update, context, hit):
    trade_id = uuid.uuid4().hex[:8]
    user_id  = update.effective_user.id
    apy      = hit.get("locked_apy", 0)
    logger.info(f"_send_rtrade_button: user={user_id} trade_id={trade_id} ticker={hit.get('ticker')} apy={apy}")
    _PENDING_TRADES[(user_id, trade_id)] = {
        "hit":        hit,
        "walk_step":  0,
        "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
        "reverse":    True,
        "trade_type": "itm_r",
    }
    htb_flag    = " ⚠️HTB?" if hit.get("htb") else ""
    ex_div_warn = f"\n🚨 EX-DIV {hit.get('next_ex_div_date', '')} BEFORE EXPIRY" if hit.get("ex_div_in_window") else ""
    ex_div_str  = f" · ex-div {hit['next_ex_div_date']}" if hit.get("next_ex_div_date") and not hit.get("ex_div_in_window") else ""
    borrow_str  = f" · borrow -{hit['borrow_cost']:.2f}" if hit.get("borrow_cost", 0) > 0 else ""
    fallback_line = f"🔄 Fallback -> {hit['fallback_apy']:.1f}% APY\n" if hit.get("fallback