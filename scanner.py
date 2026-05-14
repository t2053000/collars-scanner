"""
scanner.py
Collar scanner — uses MID-ADJUSTED fills (15% of spread away from mid)
with liquidity filters: minimum open interest and maximum bid-ask spread.
Now with debug counters to see what's filtering hits.
"""

import logging
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_EXPIRATIONS     = 10
MIN_NEG_YEARLY_PCT  = 6.0
MID_ADJUST_FRAC     = 0.15
MIN_OI              = 10
MAX_SPREAD_PCT      = 0.75


def _has_market(option):
    bid = option.get("bid") or 0.0
    ask = option.get("ask") or 0.0
    return bid > 0 and ask > 0


def _bid(option):
    return float(option.get("bid") or 0.0)


def _ask(option):
    return float(option.get("ask") or 0.0)


def _oi(option):
    return int(option.get("openInterest") or 0)


def _spread_pct(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 99.0
    return (ask - bid) / mid


def _sell_price_adjusted(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    spread = ask - bid
    return mid - MID_ADJUST_FRAC * spread


def _buy_price_adjusted(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    spread = ask - bid
    return mid + MID_ADJUST_FRAC * spread


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


class CollarScanner:
    def __init__(self, schwab_client):
        self.schwab = schwab_client

    def scan_ticker(self, ticker: str):
        results = []
        debug = Counter()
        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] option chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            debug["no_spot"] += 1
            return results, debug

        call_map = chain.get("callExpDateMap", {})
        put_map  = chain.get("putExpDateMap",  {})

        sorted_exps = sorted(
            {k.split(":")[0] for k in call_map},
            key=lambda d: datetime.strptime(d, "%Y-%m-%d"),
        )[:MAX_EXPIRATIONS]

        for exp_date in sorted_exps:
            debug["expirations"] += 1
            call_exp_key = next((k for k in call_map if k.startswith(exp_date + ":")), None)
            put_exp_key  = next((k for k in put_map  if k.startswith(exp_date + ":")), None)
            if not call_exp_key or not put_exp_key:
                continue

            calls = call_map[call_exp_key]
            puts  = put_map[put_exp_key]

            call_strikes = sorted(float(s) for s in calls if float(s) > spot)
            put_strikes  = sorted((float(s) for s in puts if float(s) < spot), reverse=True)
            if not call_strikes or not put_strikes:
                debug["no_otm_strikes"] += 1
                continue

            call_strike = call_strikes[0]
            put_strike  = put_strikes[0]

            ck = _find_key(calls, call_strike)
            pk = _find_key(puts,  put_strike)
            if not ck or not pk:
                continue

            call_contracts = calls[ck]
            put_contracts  = puts[pk]
            if not call_contracts or not put_contracts:
                continue

            call_opt = call_contracts[0]
            put_opt  = put_contracts[0]

            debug["candidates"] += 1

            if not _has_market(call_opt) or not _has_market(put_opt):
                debug["no_market"] += 1
                continue

            if _oi(call_opt) < MIN_OI:
                debug["call_oi_low"] += 1
                continue
            if _oi(put_opt) < MIN_OI:
                debug["put_oi_low"] += 1
                continue

            if _spread_pct(call_opt) > MAX_SPREAD_PCT:
                debug["call_spread_wide"] += 1
                continue
            if _spread_pct(put_opt) > MAX_SPREAD_PCT:
                debug["put_spread_wide"] += 1
                continue

            call_credit = _sell_price_adjusted(call_opt)
            put_cost    = _buy_price_adjusted(put_opt)

            exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            dte    = (exp_dt - datetime.now()).days
            if dte < 1:
                continue

            net_premium = call_credit - put_cost
            ann_factor  = 365.0 / dte

            pos_yearly = ((call_strike - spot) + net_premium) / spot * ann_factor * 100.0
            neu_yearly = net_premium                          / spot * ann_factor * 100.0
            neg_yearly = ((put_strike  - spot) + net_premium) / spot * ann_factor * 100.0

            if neg_yearly <= MIN_NEG_YEARLY_PCT:
                debug["below_neg_threshold"] += 1
                continue

            debug["passed"] += 1
            results.append(dict(
                ticker=ticker,
                exp_date=exp_date,
                dte=dte,
                spot=round(spot, 2),
                call_strike=call_strike,
                call_credit=round(call_credit, 2),
                put_strike=put_strike,
                put_cost=round(put_cost, 2),
                net_premium=round(net_premium, 2),
                pos_yearly=round(pos_yearly, 1),
                neu_yearly=round(neu_yearly, 1),
                neg_yearly=round(neg_yearly, 1),
                call_oi=_oi(call_opt),
                put_oi=_oi(put_opt),
            ))

        return results, debug

    @staticmethod
    def format_hit(r):
        return (
            f"*{r['ticker']}*  @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 sell C ${r['call_strike']} @ ${r['call_credit']} (mid-15%)\n"
            f"  🛡️ buy  P ${r['put_strike']} @ ${r['put_cost']} (mid+15%)\n"
            f"  💰 net premium: *${r['net_premium']}*\n"
            f"  📊 OI call/put: {r['call_oi']}/{r['put_oi']}\n"
            f"  📈 POS/NEU/NEG yearly:  *{r['pos_yearly']}% / {r['neu_yearly']}% / {r['neg_yearly']}%*"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"🔎 *Collar Scan Complete*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"OI ≥ {MIN_OI} both legs · spread ≤ {int(MAX_SPREAD_PCT*100)}% of mid · NEG ≥ {MIN_NEG_YEARLY_PCT:g}%\n"
            f"Fillable opportunities: *{len(all_hits)}*\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug — candidates: {d.get('candidates', 0):,}*\n"
                f"  · no market:          {d.get('no_market', 0):,}\n"
                f"  · call OI < {MIN_OI}:        {d.get('call_oi_low', 0):,}\n"
                f"  · put OI < {MIN_OI}:         {d.get('put_oi_low', 0):,}\n"
                f"  · call spread wide:   {d.get('call_spread_wide', 0):,}\n"
                f"  · put spread wide:    {d.get('put_spread_wide', 0):,}\n"
                f"  · NEG below threshold:{d.get('below_neg_threshold', 0):,}\n"
                f"  · ✅ passed:          {d.get('passed', 0):,}\n"
                f"  · no spot price:      {d.get('no_spot', 0):,} tickers\n"
                f"  · no OTM strikes:     {d.get('no_otm_strikes', 0):,} expirations\n"
            )
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors)
            if len(err_block) > 1500:
                tickers_only = ", ".join(e.split(":")[0] for e in errors)
                err_block = f"  {tickers_only}\n_(use_ `/logs` _for details)_"
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No fillable collars found._"]

        all_hits.sort(key=lambda r: r["neg_yearly"], reverse=True)

        chunks, current = [], header
        for hit in all_hits:
            block = CollarScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
