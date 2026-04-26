"""
spreads.py
DEBUG MODE — looser filters, deeper strike coverage, debug counts.

For each ticker, for each of next 10 expirations:
  Bull call: BUY each OTM call strike, SELL next strike up (up to PAIRS_PER_DIR pairs)
  Bear put:  BUY each OTM put strike,  SELL next strike down (up to PAIRS_PER_DIR pairs)
"""

import logging
import math
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_EXPIRATIONS  = 10
PAIRS_PER_DIR    = 35
MAX_MID_DEBIT    = 0.10
MAX_WORST_DEBIT  = 0.20
MIN_OI           = 1
MIN_WIDTH        = 0.50
MAX_HITS_SHOWN   = 50


def _bid_ask(option):
    bid = option.get("bid") or 0.0
    ask = option.get("ask") or 0.0
    if bid <= 0 or ask <= 0:
        return None
    return bid, ask


def _oi(option):
    return int(option.get("openInterest") or 0)


def _find_key(d, target):
    for fmt in (str(target), f"{target:.1f}", f"{target:.2f}", f"{int(target)}"):
        if fmt in d:
            return fmt
    for k in d:
        try:
            if abs(float(k) - target) < 0.01:
                return k
        except ValueError:
            pass
    return None


class SpreadScanner:
    def __init__(self, schwab_client):
        self.schwab = schwab_client

    def scan_ticker(self, ticker):
        results = []
        debug = Counter()
        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] option chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            return results, debug

        call_map = chain.get("callExpDateMap", {})
        put_map  = chain.get("putExpDateMap",  {})

        sorted_exps = sorted(
            {k.split(":")[0] for k in call_map},
            key=lambda d: datetime.strptime(d, "%Y-%m-%d"),
        )[:MAX_EXPIRATIONS]

        for exp_date in sorted_exps:
            exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            dte = (exp_dt - datetime.now()).days
            if dte < 1:
                continue

            ck = next((k for k in call_map if k.startswith(exp_date + ":")), None)
            pk = next((k for k in put_map  if k.startswith(exp_date + ":")), None)
            if not ck or not pk:
                continue

            calls = call_map[ck]
            puts  = put_map[pk]

            # BULL CALL: each OTM call up, paired with next strike up
            calls_above = sorted(float(s) for s in calls if float(s) > spot)
            for i in range(min(PAIRS_PER_DIR, len(calls_above) - 1)):
                buy_K = calls_above[i]
                sell_K = calls_above[i + 1]
                spread = self._build_spread(
                    "bull_call", ticker, exp_date, dte, spot,
                    buy_K, sell_K, calls, debug,
                )
                if spread:
                    results.append(spread)

            # BEAR PUT: each OTM put down, paired with next strike down
            puts_below = sorted(
                (float(s) for s in puts if float(s) < spot),
                reverse=True,
            )
            for i in range(min(PAIRS_PER_DIR, len(puts_below) - 1)):
                buy_K = puts_below[i]
                sell_K = puts_below[i + 1]
                spread = self._build_spread(
                    "bear_put", ticker, exp_date, dte, spot,
                    buy_K, sell_K, puts, debug,
                )
                if spread:
                    results.append(spread)

        return results, debug

    def _build_spread(self, kind, ticker, exp_date, dte, spot,
                      buy_K, sell_K, leg_map, debug):
        debug["candidates"] += 1

        bk = _find_key(leg_map, buy_K)
        sk = _find_key(leg_map, sell_K)
        if not bk or not sk:
            debug["no_strike_key"] += 1
            return None
        buy_contracts = leg_map[bk]
        sell_contracts = leg_map[sk]
        if not buy_contracts or not sell_contracts:
            debug["empty_contracts"] += 1
            return None

        buy_opt = buy_contracts[0]
        sell_opt = sell_contracts[0]

        ba_buy = _bid_ask(buy_opt)
        ba_sell = _bid_ask(sell_opt)
        if ba_buy is None or ba_sell is None:
            debug["no_bid_ask"] += 1
            return None

        buy_oi = _oi(buy_opt)
        sell_oi = _oi(sell_opt)
        if buy_oi < MIN_OI or sell_oi < MIN_OI:
            debug["oi_too_low"] += 1
            return None

        buy_bid, buy_ask = ba_buy
        sell_bid, sell_ask = ba_sell
        buy_mid = (buy_bid + buy_ask) / 2.0
        sell_mid = (sell_bid + sell_ask) / 2.0

        mid_debit = buy_mid - sell_mid
        worst_debit = buy_ask - sell_bid

        if mid_debit <= 0:
            debug["non_positive_mid"] += 1
            return None
        if mid_debit > MAX_MID_DEBIT:
            debug["mid_too_high"] += 1
            return None
        if worst_debit <= 0:
            debug["non_positive_worst"] += 1
            return None
        if worst_debit > MAX_WORST_DEBIT:
            debug["worst_too_high"] += 1
            return None

        width = abs(sell_K - buy_K)
        if width < MIN_WIDTH:
            debug["width_too_small"] += 1
            return None

        max_profit = width - mid_debit
        if max_profit <= 0:
            debug["no_profit"] += 1
            return None
        ratio = max_profit / mid_debit

        distance = abs(sell_K - spot)
        if distance <= 0:
            debug["zero_distance"] += 1
            return None

        prob_score = (1.0 / (distance / spot)) * math.sqrt(dte / 365.0)
        score = ratio * prob_score
        pct_move = (distance / spot) * 100.0

        debug["passed"] += 1

        return dict(
            kind=kind, ticker=ticker, exp_date=exp_date, dte=dte,
            spot=round(spot, 2),
            buy_strike=buy_K, sell_strike=sell_K,
            buy_oi=buy_oi, sell_oi=sell_oi,
            buy_bid=round(buy_bid, 2), buy_ask=round(buy_ask, 2),
            sell_bid=round(sell_bid, 2), sell_ask=round(sell_ask, 2),
            mid_debit=round(mid_debit, 3),
            worst_debit=round(worst_debit, 3),
            max_profit=round(max_profit, 2),
            width=round(width, 2),
            ratio=round(ratio, 1),
            score=round(score, 3),
            pct_move=round(pct_move, 1),
        )

    @staticmethod
    def format_hit(r):
        bull = r["kind"] == "bull_call"
        emoji = "🐂" if bull else "🐻"
        kind = "Bull Call" if bull else "Bear Put"
        arrow = "↑" if bull else "↓"
        cost_mid = r["mid_debit"] * 100
        cost_worst = r["worst_debit"] * 100
        max_dollars = r["max_profit"] * 100
        leg = "C" if bull else "P"
        return (
            f"{emoji} *{r['ticker']}* {kind} ({r['dte']}d to {r['exp_date']})\n"
            f"Cost: ${cost_mid:.0f} mid / ${cost_worst:.0f} worst  ·  "
            f"Max profit: ${max_dollars:.0f} if {r['ticker']} {arrow} {r['pct_move']}%\n"
            f"Spot ${r['spot']} · Buy ${r['buy_strike']:g}{leg} · "
            f"Sell ${r['sell_strike']:g}{leg} · OI {r['buy_oi']}/{r['sell_oi']}\n"
            f"Bid/Ask: {r['buy_bid']}/{r['buy_ask']} · {r['sell_bid']}/{r['sell_ask']}"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors,
                       debug_totals=None):
        header = (
            f"💸 *Spread Scan Complete*\n"
            f"Tickers: {scanned} total · ✅ {successful} scanned · ⚠️ {len(errors)} errored\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug — candidates examined: {d.get('candidates', 0):,}*\n"
                f"  · no bid/ask:        {d.get('no_bid_ask', 0):,}\n"
                f"  · OI too low:        {d.get('oi_too_low', 0):,}\n"
                f"  · mid debit too high:{d.get('mid_too_high', 0):,}\n"
                f"  · worst debit too high:{d.get('worst_too_high', 0):,}\n"
                f"  · non-positive mid:  {d.get('non_positive_mid', 0):,}\n"
                f"  · non-positive worst:{d.get('non_positive_worst', 0):,}\n"
                f"  · width too small:   {d.get('width_too_small', 0):,}\n"
                f"  · no profit:         {d.get('no_profit', 0):,}\n"
                f"  · ✅ passed:         {d.get('passed', 0):,}\n"
            )
        header += f"\nCheap fillable spreads: *{len(all_hits)}*"
        if len(all_hits) > MAX_HITS_SHOWN:
            header += f" (showing top {MAX_HITS_SHOWN})"
        header += "\n"
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors[:20])
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No qualifying spreads found._"]

        all_hits.sort(key=lambda r: r["score"], reverse=True)
        all_hits = all_hits[:MAX_HITS_SHOWN]

        chunks, current = [], header
        for hit in all_hits:
            block = SpreadScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
