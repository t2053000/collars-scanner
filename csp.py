"""
csp.py
Bull Put Credit Spread scanner — OTM short puts for stock acquisition at discount.
"""

import logging
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

DTE_MIN              = 1
DTE_MAX              = 21
DELTA_MIN            = 0.20
DELTA_MAX            = 0.30
WIDTH                = 2.50
MID_ADJUST_FRAC      = 0.15
MIN_RETURN_PCT       = 5.0
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


def _compute_annual_div(fundamentals, spot):
    if not fundamentals:
        return 0.0
    annual_div = float(fundamentals.get("dividendAmount") or 0.0)
    if annual_div > 0:
        return annual_div
    div_yield = float(fundamentals.get("dividendYield") or 0.0)
    if div_yield <= 0:
        return 0.0
    if div_yield > 1:
        return (div_yield / 100.0) * spot
    return div_yield * spot


class CspScanner:
    def __init__(self, schwab_client, ticker_freqs=None):
        self.schwab = schwab_client
        self.ticker_freqs = ticker_freqs or {}

    def scan_ticker(self, ticker):
        results = []
        debug = Counter()
        ticker = ticker.upper()
        freq = self.ticker_freqs.get(ticker, "?")

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

        annual_div = 0.0
        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
            annual_div = _compute_annual_div(fundamentals, spot)
        except Exception:
            pass

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
                strikes_sorted = sorted(float(s) for s in puts.keys())
            except ValueError:
                continue

            for short_strike in reversed(strikes_sorted):
                if short_strike >= spot:
                    continue

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
                for s in reversed(strikes_sorted):
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
                if max_risk <= 0:
                    debug["broken_quote_negative_risk"] += 1
                    continue

                return_on_risk = (net_credit / max_risk) * 100.0
                if return_on_risk < MIN_RETURN_PCT:
                    debug["below_min_return"] += 1
                    continue

                effective_buy_price = short_strike - net_credit
                discount_pct = (spot - effective_buy_price) / spot * 100.0

                div_yield_pct = (annual_div / spot) * 100.0 if spot > 0 else 0.0

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
                    return_on_risk=round(return_on_risk, 1),
                    effective_buy_price=round(effective_buy_price, 2),
                    discount_pct=round(discount_pct, 1),
                    short_delta=round(d, 2),
                    short_oi=_oi(short_opt),
                    long_oi=_oi(long_opt),
                    freq=freq,
                    annual_div=round(annual_div, 2),
                    div_yield_pct=round(div_yield_pct, 2),
                ))
                break

        return results, debug

    @staticmethod
    def format_hit(r):
        if r['discount_pct'] >= 0:
            assignment_str = f"discount {r['discount_pct']}% vs spot"
        else:
            assignment_str = f"premium {-r['discount_pct']}% vs spot"
        freq_label = {
            "M": "monthly", "Q": "quarterly", "S": "semi-annual",
            "A": "annual", "W": "weekly", "?": "unknown",
        }.get(r.get("freq", "?"), r.get("freq", "?"))
        return (
            f"💵 *{r['ticker']}* @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 Sell P ${r['short_strike']:g} @ ${r['short_credit']} (Δ {r['short_delta']})\n"
            f"  🛡️ Buy  P ${r['long_strike']:g} @ ${r['long_cost']}\n"
            f"  💰 Net credit: ${r['net_credit']}/sh · Width: ${r['width']}\n"
            f"  ⚠️ Max risk: ${r['max_risk']}/sh\n"
            f"  🎁 If assigned: own stock @ ${r['effective_buy_price']} ({assignment_str})\n"
            f"  💸 Dividend: ${r['annual_div']}/yr ({r['div_yield_pct']}% yield) · paid {freq_label}\n"
            f"  📊 OI short/long: {r['short_oi']}/{r['long_oi']}\n"
            f"  🎯 Return on risk: *{r['return_on_risk']}%* per contract"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"💵 *Bull Put Credit Spread Scan (OTM)*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Δ {DELTA_MIN}-{DELTA_MAX} (OTM) · width ${WIDTH:g} · {DTE_MIN}-{DTE_MAX}d · return ≥ {MIN_RETURN_PCT:g}%\n"
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
                f"  · below min return: {d.get('below_min_return', 0):,}\n"
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

        all_hits.sort(key=lambda r: r["return_on_risk"])

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
