"""
bot/commands_fills.py

/fills — fill history & stats, broken down by scan source.
/stop — signal the running ITMT loop to stop after its current cycle.
/cancel_all — cancel every working order at the broker.
"""
import asyncio
from collections import defaultdict

import github_store

from .helpers import authorized_only, _get_schwab_for_user, _send_robust
from .state import _ITMT_STOP, _ITMT_RUNNING, _ACTIVE_ORDERS


@authorized_only
async def cmd_fills(update, context):
    """Show fill history, stats, and breakdown by scan source."""
    args = context.args or []
    days = int(args[0]) if args else 30
    fills = github_store.get_fills(days=days)
    if not fills:
        await update.message.reply_text(f"No fills in the last {days} days.")
        return
    total_cost = sum(f.get("cost", 0) for f in fills)
    weighted_apy = sum(f.get("apy", 0) * f.get("cost", 0) for f in fills)
    avg_apy = weighted_apy / total_cost if total_cost > 0 else 0

    lines = [f"📊 *{len(fills)} fills* last {days}d — ${total_cost:,.0f} deployed — wAPY {avg_apy:.1f}%"]

    # Breakdown by scan source
    by_source = defaultdict(lambda: {"count": 0, "cost": 0, "weighted_apy": 0})
    for fl in fills:
        src = fl.get("scan_source", "unknown")
        by_source[src]["count"] += 1
        by_source[src]["cost"] += fl.get("cost", 0)
        by_source[src]["weighted_apy"] += fl.get("apy", 0) * fl.get("cost", 0)

    if any(s != "unknown" for s in by_source):
        lines.append("")
        lines.append("*By scan source:*")
        for src, data in sorted(by_source.items(), key=lambda x: -x[1]["cost"]):
            src_apy = data["weighted_apy"] / data["cost"] if data["cost"] > 0 else 0
            lines.append(f"  {src}: {data['count']} fills · ${data['cost']:,.0f} · wAPY {src_apy:.1f}%")

    lines.append("")
    lines.append("*Recent:*")
    for fl in fills[-10:]:
        mode = "🤖" if fl.get("source") == "itmt" else "👆"
        scan = fl.get("scan_source", "")
        scan_tag = f" [{scan[:8]}]" if scan and scan != "unknown" else ""
        lines.append(f"{mode} {fl.get('ticker', '?'):>5} ${fl.get('strike', 0):g} "
                     f"{fl.get('exp', '?')} {fl.get('apy', 0):.1f}%{scan_tag}")
    await _send_robust(update.message.reply_text, "\n".join(lines))


@authorized_only
async def cmd_stop(update, context):
    user_id = update.effective_user.id
    _ITMT_STOP.add(user_id)
    _ITMT_RUNNING.discard(user_id)
    await update.message.reply_text("Stopping ITMT after current cycle.")


@authorized_only
async def cmd_cancel_all(update, context):
    user_id = update.effective_user.id
    schwab = _get_schwab_for_user(context, user_id)
    if not schwab:
        await update.message.reply_text("No Schwab client available.")
        return
    loop = asyncio.get_running_loop()
    try:
        working = await loop.run_in_executor(None, schwab.get_working_orders)
    except Exception as e:
        await update.message.reply_text(f"Failed to fetch orders: {e}")
        return
    if not working:
        await update.message.reply_text("No open orders to cancel.")
        return
    cancelled, failed = 0, 0
    for o in working:
        oid = str(o.get("orderId", ""))
        try:
            await loop.run_in_executor(None, schwab.cancel_order, oid)
            cancelled += 1
        except Exception:
            failed += 1
    for oid in [k for k, v in _ACTIVE_ORDERS.items() if v.get("user_id") == user_id]:
        _ACTIVE_ORDERS.pop(oid, None)
    msg = f"Cancelled {cancelled} order(s)."
    if failed:
        msg += f" {failed} failed."
    await update.message.reply_text(msg)
