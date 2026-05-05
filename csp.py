"""
csp.py
Bull Put Credit Spread scanner.

Designed for high-delta short puts (0.80-0.85) — high probability of being
assigned the stock at a discount. APY shown is "if NOT assigned" (max profit
scenario). If assigned (more likely), you own the stock at effective_buy_price.
"""

import logging
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

DTE_MIN              = 30
DTE_MAX              = 90
DELTA_MIN            = 0.80
DELTA_MAX            = 0.85
WIDTH                = 2.50
MID_ADJUST_FRAC      = 0.15
MIN_APY_PCT          = 25.0
MAX_PRICE            = 43.0


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


def _delta(option):
    d = option.get("delta")
    if d is None:
        return None
    try:
        return abs(float(d))
    except (TypeError, ValueError):
        return None


def _sell_price(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    return mid - MID_ADJUST_FRAC * (ask - bid)


def _buy_price(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    return mid + MID_ADJUST_FRAC * (ask - bid)


class CspScanner:
    def __init__(self, schwab_client):
        self.schwab = schwab_client

    def scan_ticker(self, ticker):
        results = []
        debug = Counter()
        ticker = ticker.upper()

        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] option chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            debug["no_spot"] += 1
            return results, debug

        if spot > MAX_PRICE:
            debug["price_above_max"] += 1
            return results, debug

        put_map = chain.get("putExpDateMap", {})
        if not put_map:
            debug["empty_chain"] += 1
            return results, debug

        for full_key in put_map.keys():
            try:
                exp_date = full_key.split(":")[0]
                exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            except (ValueError, IndexError):
                continue
            dte = (exp_dt - datetime.utcnow()).days
            if dte < DTE_MIN or dte > DTE_MAX:
                debug["dte_out_of_range"] += 1
                continue

            puts = put_map[full_key]
            if not puts:
                continue

            try:
                strikes_sorted = sorted(
                    (float(s) for s in puts.keys()), reverse=True
                )
            except ValueError:
                continue

            for short_strike in strikes_sorted:
                short_str = next(
                    (s for s in puts.keys() if abs(float(s) - short_strike) < 0.001),
                    None,
                )
                if not short_str:
                    continue
                short_contracts = puts.get(short_str, [])
                if not short_contracts:
                    continue
                short_opt = short_contracts[0]
                if not _has_market(short_opt):
                    continue

                d = _delta(short_opt)
                if d is None:
                    debug["short_no_delta"] += 1
                    continue
                if d < DELTA_MIN or d > DELTA_MAX:
                    continue

                debug["short_candidates"] += 1

                target_long = short_strike - WIDTH
                long_str = None
                long_strike = None
                for s in strikes_sorted:
                    if s <= target_long + 0.001:
                        long_strike = s
                        long_str = next(
                            (k for k in puts.keys() if abs(float(k) - s) < 0.001),
                            None,
                        )
                        break
                if not long_str:
                    debug["no_long_leg"] += 1
                    continue

                long_contracts = puts.get(long_str, [])
                if not long_contracts:
                    continue
                long_opt = long_contracts[0]
                if not _has_market(long_opt):
                    debug["long_no_market"] += 1
                    continue

                short_credit = _sell_price(short_opt)
                long_cost = _buy_price(long_opt)
                net_credit = short_credit - long_cost
                if net_credit <= 0:
                    debug["non_positive_credit"] += 1
                    continue

                actual_width = short_strike - long_strike
                if actual_width <= 0:
                    continue

                max_risk = actual_width - net_credit
                # Strictly require positive max_risk (filter broken quotes
                # where credit > width — that's not free money, that's bad data)
                if max_risk <= 0:
                    debug["broken_quote_negative_risk"] += 1
                    continue

                apy_if_not_assigned = (net_credit / max_risk) * (365.0 / dte) * 100.0
                if apy_if_not_assigned < MIN_APY_PCT:
                    debug["below_min_apy"] += 1
                    continue

                # If assigned: you'd be obligated to buy stock at short strike
                # but the credit you collected reduces effective basis.
                # Long put offsets if stock falls below long strike.
                effective_buy_price = short_strike - net_credit
                discount_pct = (spot - effective_buy_price) / spot * 100.0

                debug["passed"] += 1
                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    short_strike=short_strike,
                    long_strike=long_strike,
                    width=round(actual_width, 2),
                    short_credit=round(short_credit, 2),
                    long_cost=round(long_cost, 2),
                    net_credit=round(net_credit, 2),
                    max_risk=round(max_risk, 2),
                    apy_if_not_assigned=round(apy_if_not_assigned, 1),
                    effective_buy_price=round(effective_buy_price, 2),
                    discount_pct=round(discount_pct, 1),
                    short_delta=round(d, 2),
                    short_oi=_oi(short_opt),
                    long_oi=_oi(long_opt),
                ))
                break

        return results, debug

    @staticmethod
    def format_hit(r):
        discount_str = f"premium {-r['discount_pct']}%" if r['discount_pct'] < 0 else f"discount {r['discount_pct']}%"
        return (
            f"💵 *{r['ticker']}* @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 Sell P ${r['short_strike']:g} @ ${r['short_credit']} (Δ {r['short_delta']})\n"
            f"  🛡️ Buy  P ${r['long_strike']:g} @ ${r['long_cost']}\n"
            f"  💰 Net credit: ${r['net_credit']}/sh · Width: ${r['width']}\n"
            f"  ⚠️ Max risk: ${r['max_risk']}/sh\n"
            f"  🎁 If assigned: buy @ ${r['effective_buy_price']} ({discount_str} vs spot)\n"
            f"  📊 OI short/long: {r['short_oi']}/{r['long_oi']}\n"
            f"  🎯 APY if not assigned: *{r['apy_if_not_assigned']}%*"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"💵 *Bull Put Credit Spread Scan*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Δ {DELTA_MIN}-{DELTA_MAX} · width ${WIDTH:g} · {DTE_MIN}-{DTE_MAX}d · APY ≥ {MIN_APY_PCT:g}%\n"
            f"Max price ${MAX_PRICE:g}\n"
            f"_Best deals at the BOTTOM_\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug*\n"
                f"  · short candidates: {d.get('short_candidates', 0):,}\n"
                f"  · short no delta:   {d.get('short_no_delta', 0):,}\n"
                f"  · no long leg:      {d.get('no_long_leg', 0):,}\n"
                f"  · long no market:   {d.get('long_no_market', 0):,}\n"
                f"  · non-positive credit:{d.get('non_positive_credit', 0):,}\n"
                f"  · broken quote (risk≤0):{d.get('broken_quote_negative_risk', 0):,}\n"
                f"  · below min APY:    {d.get('below_min_apy', 0):,}\n"
                f"  · ✅ passed:        {d.get('passed', 0):,}\n"
                f"  · price > ${MAX_PRICE:g}:  {d.get('price_above_max', 0):,} tickers\n"
            )
        header += f"\nOpportunities: *{len(all_hits)}*\n"
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors[:20])
            if len(err_block) > 1500:
                tickers_only = ", ".join(e.split(":")[0] for e in errors)
                err_block = f"  {tickers_only}\n_(use_ `/logs` _for details)_"
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No qualifying spreads found._"]

        all_hits.sort(key=lambda r: r["apy_if_not_assigned"])

        chunks, current = [], header
        for hit in all_hits:
            block = CspScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
