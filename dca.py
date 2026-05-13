"""
dca.py
Dividend Collar Arbitrage scanner.

Score formula (Option C - early-assignment aware):
  gap            = max(spot - strike, 0)
  safety_dollars = net_premium - gap

  if safety_dollars < 0:
      # Likely called away at first ex-div → only 1 dividend collected
      expected_div = annual_div / cycles_per_year
  else:
      # Held to expiry → collect all ex-divs in option life
      expected_div = annual_div * (dte / 365)

  score_dollars = safety_dollars + expected_div
  score_apy     = (score_dollars / spot) * (365/dte) * 100
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DTE_MIN              = 90
DTE_MAX              = 730
MIN_STRIKE_PCT_SPOT  = 0.80
MAX_STRIKE_PCT_SPOT  = 1.00
MID_ADJUST_FRAC      = 0.15
MAX_PRICE            = 43.0


_FREQ_DAYS = {
    "M": 30,
    "Q": 91,
    "S": 182,
    "A": 365,
    "W": 7,
}

_CYCLES_PER_YEAR = {
    "M": 12,
    "Q": 4,
    "S": 2,
    "A": 1,
    "W": 52,
}


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


def _project_ex_div_dates(last_ex_div, freq, until):
    interval = _FREQ_DAYS.get(freq, 91)
    today = datetime.utcnow()

    if last_ex_div:
        next_div = last_ex_div + timedelta(days=interval)
        while next_div < today:
            next_div += timedelta(days=interval)
    else:
        next_div = today + timedelta(days=interval // 2)

    count = 0
    while next_div <= until:
        count += 1
        next_div += timedelta(days=interval)
    return count


class DcaScanner:
    def __init__(self, schwab_client, ticker_freqs):
        self.schwab = schwab_client
        self.ticker_freqs = ticker_freqs

    def scan_ticker(self, ticker):
        results = []
        debug = Counter()
        ticker = ticker.upper()
        freq = self.ticker_freqs.get(ticker, "Q")

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

        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
        except Exception as e:
            logger.warning(f"[{ticker}] fundamentals fetch failed: {e}")
            fundamentals = {}

        annual_div = float(fundamentals.get("dividendAmount") or 0.0)
        if annual_div <= 0:
            div_yield = float(fundamentals.get("dividendYield") or 0.0)
            annual_div = (div_yield / 100.0) * spot if div_yield > 1 else div_yield * spot

        if annual_div <= 0:
            debug["no_dividend_data"] += 1
            return results, debug

        last_ex_div_str = fundamentals.get("exDividendDate") or fundamentals.get("dividendDate")
        last_ex_div = None
        if last_ex_div_str:
            try:
                last_ex_div = datetime.strptime(last_ex_div_str[:10], "%Y-%m-%d")
            except ValueError:
                pass

        if last_ex_div is None:
            debug["ex_div_estimated"] += 1

        call_map = chain.get("callExpDateMap", {})
        put_map = chain.get("putExpDateMap", {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        strike_floor = spot * MIN_STRIKE_PCT_SPOT
        strike_ceil = spot * MAX_STRIKE_PCT_SPOT

        all_exp_dates = set(k.split(":")[0] for k in call_map) & \
                        set(k.split(":")[0] for k in put_map)

        for exp_date in sorted(all_exp_dates):
            try:
                exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            except ValueError:
                continue
            dte = (exp_dt - datetime.utcnow()).days
            if dte < DTE_MIN or dte > DTE_MAX:
                debug["dte_out_of_range"] += 1
                continue

            ck = next((k for k in call_map if k.startswith(exp_date + ":")), None)
            pk = next((k for k in put_map if k.startswith(exp_date + ":")), None)
            if not ck or not pk:
                continue

            calls = call_map[ck]
            puts = put_map[pk]

            common_strikes = set()
            for s in calls.keys():
                try:
                    fs = float(s)
                    if strike_floor <= fs <= strike_ceil and s in puts:
                        common_strikes.add(s)
                except ValueError:
                    pass

            for strike_str in common_strikes:
                debug["candidates"] += 1
                strike = float(strike_str)

                call_contracts = calls.get(strike_str, [])
                put_contracts = puts.get(strike_str, [])
                if not call_contracts or not put_contracts:
                    debug["empty_contracts"] += 1
                    continue

                call_opt = call_contracts[0]
                put_opt = put_contracts[0]

                if not _has_market(call_opt):
                    debug["call_no_market"] += 1
                    continue
                if not _has_market(put_opt):
                    debug["put_no_market"] += 1
                    continue

                call_credit = _sell_price(call_opt)
                put_cost = _buy_price(put_opt)

                net_premium = call_credit - put_cost
                if net_premium <= 0:
                    debug["non_positive_credit"] += 1
                    continue

                gap = max(spot - strike, 0)
                safety_dollars = net_premium - gap

                num_ex_divs = _project_ex_div_dates(last_ex_div, freq, exp_dt)
                cycles_per_year = _CYCLES_PER_YEAR.get(freq, 4)

                # OPTION C: if negative safety, assume early assignment at first ex-div
                if safety_dollars < 0:
                    expected_div_dollars = annual_div / cycles_per_year
                    expected_ex_divs_collected = 1
                else:
                    expected_div_dollars = annual_div * (dte / 365.0)
                    expected_ex_divs_collected = num_ex_divs

                score_dollars = safety_dollars + expected_div_dollars
                score_apy = (score_dollars / spot) * (365.0 / dte) * 100.0

                # Diagnostic-only legacy APY breakdowns
                safety_apy = (safety_dollars / spot) * (365.0 / dte) * 100.0
                div_yield_apy = (annual_div / spot) * 100.0

                debug["passed"] += 1
                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    strike=strike,
                    call_credit=round(call_credit, 2),
                    put_cost=round(put_cost, 2),
                    net_premium=round(net_premium, 2),
                    gap=round(gap, 2),
                    safety_dollars=round(safety_dollars, 2),
                    safety_apy=round(safety_apy, 1),
                    annual_div=round(annual_div, 2),
                    div_yield_apy=round(div_yield_apy, 1),
                    num_ex_divs=num_ex_divs,
                    expected_ex_divs_collected=expected_ex_divs_collected,
                    expected_div_dollars=round(expected_div_dollars, 2),
                    score_dollars=round(score_dollars, 2),
                    score_apy=round(score_apy, 1),
                    freq=freq,
                    call_oi=_oi(call_opt),
                    put_oi=_oi(put_opt),
                    ex_div_estimated=last_ex_div is None,
                ))

        return results, debug

    @staticmethod
    def format_hit(r):
        est_marker = " ~" if r.get("ex_div_estimated") else ""
        safety_sign = "+" if r["safety_dollars"] >= 0 else ""
        score_sign = "+" if r["score_dollars"] >= 0 else ""
        if r["safety_dollars"] < 0:
            assignment_note = "early assignment likely (1 div assumed)"
        else:
            assignment_note = f"held to expiry ({r['num_ex_divs']}{est_marker} ex-divs)"
        return (
            f"💰 *{r['ticker']}* @ ${r['spot']}  ·  freq *{r['freq']}*\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 Sell C ${r['strike']:g} @ ${r['call_credit']}\n"
            f"  🛡️ Buy  P ${r['strike']:g} @ ${r['put_cost']}\n"
            f"  💵 Net premium: ${r['net_premium']}/sh · Gap: ${r['gap']}/sh\n"
            f"  🛡 Safety: *{safety_sign}${r['safety_dollars']}/sh* ({r['safety_apy']}% APY)\n"
            f"  💸 Annual div: ${r['annual_div']} ({r['div_yield_apy']}% APY)\n"
            f"  📊 Expected divs collected: *${r['expected_div_dollars']}/sh* ({assignment_note})\n"
            f"  📊 OI call/put: {r['call_oi']}/{r['put_oi']}\n"
            f"  🎯 Score: *{score_sign}${r['score_dollars']}/sh*  ·  *{r['score_apy']}% APY*"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"💰 *Dividend Collar Arbitrage Scan*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Same-strike collars (80-100% of spot, {DTE_MIN}-{DTE_MAX}d, net credit > 0)\n"
            f"Score = safety$ + expected_div$ · Negative-safety = early-assignment penalty\n"
            f"Max price ${MAX_PRICE:g}\n"
            f"_Best deals at the BOTTOM_\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug — candidates: {d.get('candidates', 0):,}*\n"
                f"  · empty contracts:    {d.get('empty_contracts', 0):,}\n"
                f"  · call no market:     {d.get('call_no_market', 0):,}\n"
                f"  · put no market:      {d.get('put_no_market', 0):,}\n"
                f"  · non-positive net:   {d.get('non_positive_credit', 0):,}\n"
                f"  · ✅ passed:          {d.get('passed', 0):,}\n"
                f"  · price > ${MAX_PRICE:g}:    {d.get('price_above_max', 0):,} tickers\n"
                f"  · ex-div estimated:   {d.get('ex_div_estimated', 0):,} tickers\n"
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
            return [header + "_No qualifying same-strike collars found._"]

        # Reverse sort: worst at top, best at bottom (sort by score_apy)
        all_hits.sort(key=lambda r: r["score_apy"])

        chunks, current = [], header
        for hit in all_hits:
            block = DcaScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
