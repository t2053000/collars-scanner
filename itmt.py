#!/usr/bin/env python3
"""
itmt.py — Autonomous ITM Terminal
Scans tickers, auto-places TOP-N orders IN PARALLEL, first fill wins,
cancels the rest, then rescans. Runs for 1 hour or until budget allocated.

Usage:
    python itmt.py 6000          # $6,000 budget, 35% APY min, 1 hour
    python itmt.py 6000 40       # $6,000 budget, 40% APY min
    python itmt.py 6000 35 120   # $6,000 budget, 35% APY min, 2 hours
"""

import os
import sys
import time
import logging
import asyncio
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# ── project imports ────────────────────────────────────────────────────
from schwab_client import SchwabClient
from itm import ItmScanner
import orders
import github_store

# ── configuration ──────────────────────────────────────────────────────
SCAN_CONCURRENCY = 12
TOP_N            = 3           # try top-N per scan cycle
FILL_WAIT_SEC    = 4           # wait per order attempt
POLL_INTERVAL    = 1           # check fill every N sec
DEFAULT_MIN_APY  = 35.0
DEFAULT_TIMEOUT  = 60          # minutes

# ── logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("itmt")


# ── helpers ────────────────────────────────────────────────────────────

def _bootstrap_schwab() -> SchwabClient:
    """Create and return an authenticated SchwabClient."""
    token_json = os.getenv("SCHWAB_TOKEN_JSON")
    token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "token.json"))
    if token_json and not token_path.exists():
        token_path.write_text(token_json)
        log.info(f"Wrote Schwab token to {token_path}")
    client = SchwabClient(token_path=str(token_path))
    client.initialize()
    log.info("Schwab client initialised")
    return client


async def _scan_all(scanner: ItmScanner, tickers: list[str]) -> list[dict]:
    """Run the ITM scan across all tickers with concurrency."""
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    all_hits = []
    errors = 0
    debug_totals = Counter()

    async def scan_one(tk):
        nonlocal errors
        async with sem:
            try:
                result = await loop.run_in_executor(
                    None, lambda t=tk: scanner.scan_ticker(t)
                )
                if isinstance(result, tuple):
                    hits, debug = result
                    debug_totals.update(debug)
                else:
                    hits = result
                all_hits.extend(hits)
            except Exception as e:
                errors += 1
                log.debug(f"  scan error {tk}: {e}")

    await asyncio.gather(*(scan_one(t) for t in tickers))
    log.info(f"  scan complete: {len(all_hits)} hits, {errors} errors | "
             f"debug: {dict(debug_totals)}")
    return all_hits


def _filter_hits(hits: list[dict], budget: float, min_apy: float) -> list[dict]:
    """Filter by budget and APY, return sorted by APY descending."""
    qualified = []
    for h in hits:
        cost = h["spot"] * 100
        if cost > budget:
            continue
        pricing = orders.compute_legs_pricing(h, walk_step=0)
        if pricing["apy"] < min_apy:
            continue
        qualified.append((h, pricing))

    qualified.sort(key=lambda x: x[1]["apy"], reverse=True)
    return qualified


def _try_place(schwab: SchwabClient, hit: dict, pricing: dict) -> str | None:
    """Build and place an ITM conversion order. Returns order_id or None."""
    try:
        payload = orders.build_itm_conversion_order(hit, pricing)
        order_id = schwab.place_order(payload)
        log.info(f"  ORDER PLACED: {order_id} · {hit['ticker']} "
                 f"strike=${hit['strike']:g} exp={hit['exp_date']} "
                 f"APY={pricing['apy']:.1f}%")
        return order_id
    except Exception as e:
        log.warning(f"  place_order failed for {hit['ticker']}: {e}")
        return None


def _check_fill(schwab: SchwabClient, order_id: str) -> str:
    """Return order status string."""
    try:
        data = schwab.get_order_status(order_id)
        return data.get("status", "UNKNOWN")
    except Exception as e:
        log.debug(f"  status check error: {e}")
        return "UNKNOWN"


def _cancel(schwab: SchwabClient, order_id: str):
    """Cancel an order, swallow errors."""
    try:
        schwab.cancel_order(order_id)
        log.info(f"  CANCELLED: {order_id}")
    except Exception as e:
        log.warning(f"  cancel failed for {order_id}: {e}")


# ── main loop ──────────────────────────────────────────────────────────

async def run(budget: float, min_apy: float, timeout_min: float):
    schwab = _bootstrap_schwab()
    div_tickers = github_store.get_div_tickers()
    scanner = ItmScanner(schwab, div_tickers)

    hiv_tickers = github_store.get_latest_hiv_tickers()
    tickers = hiv_tickers if hiv_tickers else github_store.get_tickers()
    tickers = sorted(set(tickers))

    remaining = budget
    deadline = time.time() + (timeout_min * 60)
    cycle = 0
    fills = []

    log.info("=" * 60)
    log.info(f"ITMT started — budget=${budget:,.0f}  min_apy={min_apy}%  "
             f"timeout={timeout_min}min  tickers={len(tickers)}")
    log.info("=" * 60)

    while time.time() < deadline and remaining > 0:
        cycle += 1
        elapsed = time.time() - (deadline - timeout_min * 60)
        log.info(f"\n{'─' * 50}")
        log.info(f"CYCLE {cycle} | remaining=${remaining:,.0f} | "
                 f"elapsed={elapsed:.0f}s / {timeout_min * 60:.0f}s")
        log.info(f"{'─' * 50}")

        # ── scan ────────────────────────────────────────────
        hits = await _scan_all(scanner, tickers)
        candidates = _filter_hits(hits, remaining, min_apy)

        if not candidates:
            log.info("  no qualifying hits — rescanning in 5s...")
            await asyncio.sleep(5)
            continue

        top = candidates[:TOP_N]
        log.info(f"  top {len(top)} candidates:")
        for i, (h, p) in enumerate(top, 1):
            log.info(f"    #{i}  {h['ticker']:>5}  strike=${h['strike']:>6g}  "
                     f"exp={h['exp_date']}  spot=${h['spot']:.2f}  "
                     f"APY={p['apy']:.1f}%  net_credit=${p['net_credit']:.2f}")

        # ── place all top-N in parallel ─────────────────────
        placed = []   # (order_id, hit, pricing)
        for rank, (hit, pricing) in enumerate(top, 1):
            order_id = _try_place(schwab, hit, pricing)
            if order_id:
                placed.append((order_id, hit, pricing))

        if not placed:
            log.info("  all placements failed — rescanning in 3s...")
            await asyncio.sleep(3)
            continue

        log.info(f"  {len(placed)} orders live — polling for first fill "
                 f"({FILL_WAIT_SEC}s window)...")

        # ── poll all orders, first fill wins ────────────────
        winner = None
        live = list(placed)     # copy — we'll remove dead orders
        start = time.time()
        while time.time() - start < FILL_WAIT_SEC and live:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed_poll = time.time() - start
            still_live = []
            for oid, hit, pricing in live:
                status = _check_fill(schwab, oid)
                log.info(f"    {oid} {hit['ticker']:>5} {status} "
                         f"({elapsed_poll:.1f}s)")
                if status == "FILLED":
                    winner = (oid, hit, pricing)
                    break
                if status in ("CANCELED", "REJECTED", "EXPIRED"):
                    continue   # drop from live list
                still_live.append((oid, hit, pricing))
            if winner:
                break
            live = still_live

        # ── cancel all non-winners ──────────────────────────
        winner_oid = winner[0] if winner else None
        for oid, hit, pricing in placed:
            if oid != winner_oid:
                _cancel(schwab, oid)

        if winner:
            oid, hit, pricing = winner
            cost = hit["spot"] * 100
            remaining -= cost
            fills.append({
                "ticker": hit["ticker"],
                "strike": hit["strike"],
                "exp": hit["exp_date"],
                "apy": pricing["apy"],
                "cost": cost,
                "order_id": oid,
            })
            log.info(f"  ✅ FILLED: {hit['ticker']} | cost=${cost:,.0f} | "
                     f"remaining=${remaining:,.0f}")
        else:
            log.info("  no fills this cycle — rescanning...")

    # ── summary ────────────────────────────────────────────────────
    log.info(f"\n{'=' * 60}")
    log.info(f"ITMT COMPLETE — {len(fills)} fill(s), "
             f"${budget - remaining:,.0f} allocated of ${budget:,.0f}")
    log.info(f"{'=' * 60}")
    for f in fills:
        log.info(f"  ✅ {f['ticker']:>5}  strike=${f['strike']:g}  "
                 f"exp={f['exp']}  APY={f['apy']:.1f}%  "
                 f"cost=${f['cost']:,.0f}  order={f['order_id']}")
    if not fills:
        log.info("  no orders filled")
    log.info("=" * 60)


# ── entry point ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python itmt.py <budget> [min_apy] [timeout_min]")
        print("  e.g. python itmt.py 6000          # $6k, 35% APY, 1 hour")
        print("       python itmt.py 6000 40 120    # $6k, 40% APY, 2 hours")
        sys.exit(1)

    budget      = float(sys.argv[1])
    min_apy     = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MIN_APY
    timeout_min = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_TIMEOUT

    asyncio.run(run(budget, min_apy, timeout_min))


if __name__ == "__main__":
    main()