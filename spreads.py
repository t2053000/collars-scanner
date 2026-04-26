"""
spreads.py
Scans for cheap, fillable vertical debit spreads.
"""

import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_EXPIRATIONS  = 10
MAX_MID_DEBIT    = 0.02
MAX_WORST_DEBIT  = 0.05
MIN_OI           = 5
MIN_WIDTH        = 1.0


def _bid_ask(option: dict) -> tuple[float, float] | None:
    bid = option.get("bid") or 0.0
    ask = option.get("ask") or 0.0
    if bid <= 0 or ask <= 0:
        return None
    return bid, ask


def _oi(option: dict) -> int:
    return int(option.get("openInterest") or 0)


def _find_key(d: dict, target: float) -> str | None:
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

    def scan_ticker(self, ticker: str) -> list[dict]:
        results = []
        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] option chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            return results

        call_map: dict = chain.get("callExpDateMap", {})
        put_map:  dict = chain.get("putExpDateMap",  {})

        sorted_exps = sorted(
            {k.split(":")[0] for k in call_map},
            key=lambda d: datetime.strptime(d, "%Y-%m-%d"),
        )[:MAX_EXPIRATIONS]

        for exp_date in sorted_exps:
            exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            dte    = (exp_dt - datetime.now()).days
            if dte < 1:
                continue

            call_exp_key = next((k for k in call_map if k.startswith(exp_date + ":")), None)
            put_exp_key  = next((k for k in put_map  if k.startswith(exp_date + ":")), None)
            if not call_exp_key or not put_exp_key:
                continue

            calls = call_map[call_exp_key]
            puts  = put_map[put_exp_key]

            call_strikes_above = sorted(float(s) for s in calls if float(s) > spot)
            if len(call_strikes_above) >= 2:
                buy_K, sell_K = call_strikes_above[0], call_strikes_above[1]
                spread = self._build_spread("bull_call", ticker, exp_date, dte, spot,
                                            buy_K, sell_K, calls)
                if spread:
                    results.append(spread)

            put_strikes_below = sorted(
                (float(s) for s in puts if float(s) < spot),
                reverse=True,
            )
            if len(put_strikes_below) >= 2:
                buy_K, sell_K = put_strikes_below[0], put_strikes_below[1]
                spread = self._build_spread("bear_put", ticker, exp_date, dte, spot,
                                            buy_K, sell_K, puts)
                if spread:
                    results.append(spread)

        return results

    def _build_spread(self, kind, ticker, exp_date, dte, spot,
                      buy_K, sell_K, leg_map):
        bk = _find_key(leg_map, buy_K)
        sk = _find_key(leg_map, sell_K)
        if not bk or not sk:
            return None
        buy_contracts  = leg_map[bk]
        sell_contracts = leg_map[sk]
        if not buy_contracts or not sell_contracts:
            return None

        buy_opt  = buy_contracts[0]
        sell_opt = sell_contracts[0]

        ba_buy  = _bid_ask(buy_opt)
        ba_sell = _bid_ask(sell_opt)
        if ba_buy is None or ba_sell is None:
            return None

        buy_oi  = _oi(buy_opt)
        sell_oi = _oi(sell_opt)
        if buy_oi < MIN_OI or sell_oi < MIN_OI:
            return None

        buy_bid, buy_ask   = ba_buy
        sell_bid, sell_ask = ba_sell
        buy_mid  = (buy_bid + buy_ask) / 2.0
        sell_mid = (sell_bid + sell_ask) / 2.0

        mid_debit   = buy_mid - sell_mid
        worst_debit = buy_ask - sell_bid

        if mid_debit <= 0 or mid_debit > MAX_MID_DEBIT:
            return None
        if worst_debit <= 0 or worst_debit > MAX_WORST_DEBIT:
            return None

        width = abs(sell_K - buy_K)
        if width < MIN_WIDTH:
            return None

        max_profit = width - mid_debit
        if max_profit <= 0:
            return None
        ratio = max_profit / mid_debit

        distance = abs(sell_K - spot)
        if distance <= 0:
            return None

        prob_score = (1.0 / (distance / spot)) * math.sqrt(dte / 365.0)
        score      = ratio * prob_score
        pct_move   = (distance / spot) * 100.0

        return dict(
            kind=kind, ticker=ticker, exp_date=exp_date, dte=dte,
            spot=round(spot, 2),
            buy_strike=buy_K, sell_strike=sell_K,
            buy_oi=buy_oi, sell_oi=sell_oi,
            mid_debit=round(mid_debit, 3),
            worst_debit=round(worst_debit, 3),
            max_profit=round(max_profit, 2),
            width=round(width, 2),
            ratio=round(ratio, 1),
            score=round(score, 3),
            pct_move=round(pct_move, 1),
        )

    @staticmethod
    def format_hit(r: dict) -> str:
        bull = r["kind"] == "bull_call"
        emoji = "🐂" if bull else "🐻"
        kind  = "Bull Call" if bull else "Bear Put"
        arrow = "↑" if bull else "↓"
        cost_mid    = r["mid_debit"]   * 100
        cost_worst  = r["worst_debit"] * 100
        max_dollars = r["max_profit"]  * 100
        leg_letter  = "C" if bull else "P"
        return (
            f"{emoji} *{r['ticker']}* {kind} ({r['dte']}d to {r['exp_date']})\n"
            f"Cost: ${cost_mid:.0f} mid / ${cost_worst:.0f} worst  ·  "
            f"Max profit: ${max_dollars:.0f} if {r['ticker']} {arrow} {r['pct_move']}%\n"
            f"Spot ${r['spot']} · Buy ${r['buy_strike']:g}{leg_letter} · "
            f"Sell ${r['sell_strike']:g}{leg_letter} · OI {r['buy_oi']}/{r['sell_oi']}"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors):
        header = (
            f"💸 *Spread Scan Complete*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Cheap fillable spreads: *{len(all_hits)}*\n"
        )
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors)
            if len(err_block) > 1500:
                tickers_only = ", ".join(e.split(":")[0] for e in errors)
                err_block = f"  {tickers_only}\n_(use_ `/logs` _for details)_"
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No qualifying spreads found._"]

        all_hits.sort(key=lambda r: r["score"], reverse=True)

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
