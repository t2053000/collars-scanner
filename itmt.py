"""
bot/itmt.py

/itmt — the ITM auto-trader. Repeatedly scans, ranks candidates by APY,
fires the top N as parallel orders, keeps the first fill and cancels
the rest, and loops until the budget or timeout runs out.
"""
import asyncio
import logging
import time
from collections import Counter

from telegram.constants import ParseMode

import github_store
import orders

from . import state
from .helpers import authorized_only, _get_schwab_for_user, _edit_robust, _send_robust, _maybe_save_token
from .state import SCAN_CONCURRENCY, TICKER_BLACKLIST, _ITMT_STOP, _ITMT_RUNNING

logger = logging.getLogger(__name__)

# ── ITMT configuration ────────────────────────────────────────────────
ITMT_TOP_N         = 3
ITMT_FILL_WAIT_SEC = 4
ITMT_POLL_SEC      = 1
ITMT_DEFAULT_APY   = 35.0
ITMT_DEFAULT_MIN   = 180     # minutes (3 hours)


@authorized_only
async def cmd_itmt(update, context):
    """
    /itmt 6000          — $6k budget, 35% APY, 1 hour
    /itmt 6000 40       — $6k budget, 40% APY min
    /itmt 6000 35 120   — $6k budget, 35% APY, 2 hours
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/itmt <budget> [min_apy] [timeout_min]`\n"
            "Example: `/itmt 6000` — $6k, 35% APY, 1 hour",
            parse_mode=ParseMode.MARKDOWN)
        return

    user_id = update.effective_user.id
    if user_id in _ITMT_RUNNING:
        _ITMT_STOP.add(user_id)
        await update.message.reply_text("⏹ Stopping previous ITMT... restarting.")
        await asyncio.sleep(15)
        _ITMT_STOP.discard(user_id)

    budget      = float(args[0])
    min_apy     = float(args[1]) if len(args) > 1 else ITMT_DEFAULT_APY
    timeout_min = float(args[2]) if len(args) > 2 else ITMT_DEFAULT_MIN
    schwab  = _get_schwab_for_user(context, user_id)
    scanner = context.application.bot_data["itm_scanner"]
    scanner.ticker_freqs = github_store.get_div_tickers()

    tickers = []
    ticker_sources = {}
    logger.info(f"ITMT: budget=${budget}, min_apy={min_apy}")

    status_msg = await update.message.reply_text(
        f"🤖 *ITMT started*\n"
        f"Budget: ${budget:,.0f} · APY ≥ {min_apy}% · Timeout: {timeout_min:.0f}min",
        parse_mode=ParseMode.MARKDOWN)

    loop      = asyncio.get_running_loop()
    _ITMT_STOP.discard(user_id)
    _ITMT_RUNNING.add(user_id)
    remaining = budget
    deadline  = time.time() + (timeout_min * 60)
    cycle     = 0
    fills     = []
    sem       = asyncio.Semaphore(SCAN_CONCURRENCY)

    try:
      while time.time() < deadline and remaining > 0 and user_id not in _ITMT_STOP:
        cycle += 1
        elapsed = time.time() - (deadline - timeout_min * 60)

        # Reload tickers every 10 cycles to pick up new ones
        if cycle == 1 or cycle % 10 == 0:
            try:
                hiv_tickers = await asyncio.wait_for(
                    loop.run_in_executor(None, github_store.get_latest_hiv_tickers),
                    timeout=30)
                new_list = hiv_tickers if hiv_tickers else await asyncio.wait_for(
                    loop.run_in_executor(None, github_store.get_tickers),
                    timeout=30)
                new_list = sorted(set(new_list) - TICKER_BLACKLIST)
                ticker_sources = await asyncio.wait_for(
                    loop.run_in_executor(None, github_store.get_ticker_sources),
                    timeout=30)
                new_list = sorted(new_list, key=lambda t: ticker_sources.get(t, {}).get("priority", 3))
                tickers = new_list
                p1 = sum(1 for t in tickers if ticker_sources.get(t, {}).get("priority") == 1)
                logger.info(f"ITMT: reloaded {len(tickers)} tickers ({p1} priority 1)")
            except Exception as e:
                logger.warning(f"ITMT: ticker reload failed ({e}) — keeping previous {len(tickers)} tickers")
                if not tickers:
                    await asyncio.sleep(10)
                    continue

        # ── scan ────────────────────────────────────────
        all_hits = []
        debug_totals = Counter()
        errors = 0

        async def scan_one(tk):
            nonlocal errors
            async with sem:
                try:
                    result = await loop.run_in_executor(
                        None, lambda t=tk: scanner.scan_ticker(t))
                    if isinstance(result, tuple):
                        hits, debug = result
                        debug_totals.update(debug)
                    else:
                        hits = result
                    all_hits.extend(hits)
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        logger.error(f"ITMT scan error {tk}: {e}")

        scan_start = time.time()
        await asyncio.gather(*(scan_one(t) for t in tickers))
        scan_dur = time.time() - scan_start
        logger.info(f"ITMT cycle {cycle}: scan done in {scan_dur:.1f}s — {len(all_hits)} hits, {errors} errors")

        # ── filter & rank ───────────────────────────────
        candidates = []
        for h in all_hits:
            if h["spot"] * 100 > remaining:
                continue
            p = orders.compute_legs_pricing(h, walk_step=0)
            if p["apy"] >= min_apy:
                candidates.append((h, p))
        candidates.sort(key=lambda x: x[1]["apy"], reverse=True)
        logger.info(f"ITMT cycle {cycle}: {len(candidates)} qualified (budget ${remaining:,.0f}, min_apy {min_apy}%)")
        if candidates:
            for i, (h, p) in enumerate(candidates[:5]):
                logger.info(f"  #{i+1} {h['ticker']} spot=${h['spot']:.2f} strike=${h['strike']:g} APY={p['apy']:.1f}% cost=${h['spot']*100:.0f}")

        await _edit_robust(status_msg,
            f"🤖 *ITMT cycle {cycle}*\n"
            f"Remaining: ${remaining:,.0f} · {elapsed:.0f}s elapsed\n"
            f"Hits: {len(all_hits)} · Qualified: {len(candidates)} · Errors: {errors}")

        if not candidates:
            if errors > len(tickers) * 0.5:
                wait = 30
                logger.info(f"ITMT cycle {cycle}: {errors} errors — backing off {wait}s")
            else:
                wait = 10
                logger.info(f"ITMT cycle {cycle}: no candidates — sleeping {wait}s")
            await asyncio.sleep(wait)
            continue

        top = candidates[:ITMT_TOP_N]

        # ── place all top-N in parallel ─────────────────
        placed = []
        for hit, pricing in top:
            try:
                payload  = orders.build_itm_conversion_order(hit, pricing)
                order_id = await loop.run_in_executor(
                    None, schwab.place_order, payload)
                placed.append((order_id, hit, pricing))
                logger.info(f"ITMT placed {order_id} · {hit['ticker']} "
                            f"APY={pricing['apy']:.1f}%")
            except Exception as e:
                logger.warning(f"ITMT place failed {hit['ticker']}: {e}")

        logger.info(f"ITMT cycle {cycle}: {len(placed)} orders placed")
        if not placed:
            logger.info(f"ITMT cycle {cycle}: all placements failed — sleeping 10s")
            await asyncio.sleep(10)
            continue

        summary = " / ".join(f"{h['ticker']} {p['apy']:.0f}%"
                             for _, h, p in placed)
        await _edit_robust(status_msg,
            f"🤖 *ITMT cycle {cycle}* — {len(placed)} orders live\n"
            f"{summary}\n"
            f"Polling {ITMT_FILL_WAIT_SEC}s for fill...")

        # ── poll for first fill ─────────────────────────
        winner = None
        live = list(placed)
        start = time.time()
        while time.time() - start < ITMT_FILL_WAIT_SEC and live:
            await asyncio.sleep(ITMT_POLL_SEC)
            still_live = []
            for oid, hit, pricing in live:
                try:
                    data   = await loop.run_in_executor(
                        None, schwab.get_order_status, oid)
                    status = data.get("status", "UNKNOWN")
                except Exception:
                    status = "UNKNOWN"
                if status == "FILLED":
                    winner = (oid, hit, pricing)
                    break
                if status not in ("CANCELED", "REJECTED", "EXPIRED"):
                    still_live.append((oid, hit, pricing))
            if winner:
                break
            live = still_live

        logger.info(f"ITMT cycle {cycle}: poll done — winner={'yes' if winner else 'no'}, {len(live)} still live")

        # ── cancel non-winners ──────────────────────────
        winner_oid = winner[0] if winner else None
        for oid, hit, pricing in placed:
            if oid != winner_oid:
                try:
                    await loop.run_in_executor(None, schwab.cancel_order, oid)
                except Exception:
                    pass

        # ── handle result ───────────────────────────────
        if winner:
            oid, hit, pricing = winner
            cost = hit["spot"] * 100
            remaining -= cost
            fills.append({
                "ticker": hit["ticker"], "strike": hit["strike"],
                "exp": hit["exp_date"], "apy": pricing["apy"],
                "cost": cost, "order_id": oid,
            })
            github_store.save_fill({
                "ticker": hit["ticker"], "strike": hit["strike"],
                "exp": hit["exp_date"], "dte": hit["dte"],
                "apy": pricing["apy"], "cost": cost,
                "order_id": oid, "source": "itmt",
                "scan_source": ticker_sources.get(hit["ticker"], {}).get("scan_code", "unknown"),
            })
            await _send_robust(update.message.reply_text,
                f"✅ *FILLED* — {hit['ticker']} · order {oid}\n"
                f"Strike ${hit['strike']:g} · {hit['exp_date']} "
                f"({hit['dte']}d)\n"
                f"APY: *{pricing['apy']:.1f}%* · Cost: ${cost:,.0f}\n"
                f"Remaining: ${remaining:,.0f}")

    finally:
        _ITMT_RUNNING.discard(user_id)
        state._LAST_TOKEN_SAVE = 0  # force save, bypass 1hr throttle
        primary_uid = context.application.bot_data.get("primary_user_id", 0)
        _maybe_save_token(primary_uid)
        logger.info("ITMT exited — token saved")

    # ── final summary ───────────────────────────────────
    if fills:
        lines = [f"🤖 *ITMT COMPLETE* — {len(fills)} fill(s), "
                 f"${budget - remaining:,.0f} of ${budget:,.0f} deployed\n"]
        for f in fills:
            lines.append(f"✅ {f['ticker']} ${f['strike']:g} "
                         f"{f['exp']} APY={f['apy']:.1f}% "
                         f"${f['cost']:,.0f}")
        await _send_robust(update.message.reply_text, "\n".join(lines))
    else:
        await _edit_robust(status_msg,
            f"🤖 *ITMT COMPLETE* — no fills after {cycle} cycles.\n"
            f"Budget ${budget:,.0f} unallocated.")
