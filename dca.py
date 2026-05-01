"""
dca.py
Dividend Collar Arbitrage scanner.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DTE_MIN              = 90
DTE_MAX              = 730
MIN_STRIKE_PCT_SPOT  = 0.80
MAX_STRIKE_PCT_SPOT  = 1.00
MID_ADJUST_FRAC      = 0.15
MIN_OI               = 1


_FREQ_DAYS = {
    "M": 30,
    "Q": 91,
    "S": 182,
    "A": 365,
    "W": 7,
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
    if not last_ex_div:
        return 0
    interval = _FREQ_DAYS.get(freq, 91)
    today = datetime.utcnow()
    next_div = last_ex_div + timedelta(days=interval)
    while next_div < today:
        next_div += timedelta(days=interval)
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
        ticker = ticker.upper()
        freq = self.ticker_freqs.get(ticker, "Q")

        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] option chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            return results

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
            return results

        last_ex_div_str = fundamentals.get("exDividendDate")
        last_ex_div = None
        if last_ex_div_str:
            try:
                last_ex_div = datetime.strptime(last_ex_div_str[:10], "%Y-%m-%d")
            except ValueError:
                pass

        call_map = chain.get("callExpDateMap", {})
        put_map = chain.get("putExpDateMap", {})
        if not call_map or not put_map:
            return results

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
                strike = float(strike_str)

                call_contracts = calls.get(strike_str, [])
                put_contracts = puts.get(strike_str, [])
                if not call_contracts or not put_contracts:
                    continue

                call_opt = call_contracts[0]
                put_opt = put_contracts[0]

                if not _has_market(call_opt) or not _has_market(put_opt):
                    continue
                if _oi(call_opt) < MIN_OI or _oi(put_opt) < MIN_OI:
                    continue

                call_credit = _sell_price(call_opt)
                put_cost = _buy_price(put_opt)

                net_premium = call_credit - put_cost
                if net_premium <= 0:
                    continue

                intrinsic = max(spot - strike, 0)
                call_time_value = call_credit - intrinsic
                if call_time_value <= 0.01:
                    call_time_value = 0.01

                num_ex_divs = _project_ex_div_dates(last_ex_div, freq, exp_dt)
                if num_ex_divs == 0:
                    continue

                score = (annual_div / call_time_value) * num_ex_divs

                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    strike=strike,
                    call_credit=round(call_credit, 2),
                    put_cost=round(put_cost, 2),
                    net_premium=round(net_premium, 2),
                    call_time_value=round(call_time_value, 2),
                    annual_div=round(annual_div, 2),
                    num_ex_divs=num_ex_divs,
                    freq=freq,
                    score=round(score, 2),
                ))

        return results

    @staticmethod
    def format_hit(r):
        return (
            f"💰 *{r['ticker']}* @ ${r['spot']}  ·  freq *{r['freq']}*\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 Sell C ${r['strike']:g} @ ${r['call_credit']}\n"
            f"  🛡️ Buy  P ${r['strike']:g} @ ${r['put_cost']}\n"
            f"  💵 Net premium credit: *${r['net_premium']}*/sh\n"
            f"  ⏳ Call time value: ${r['call_time_value']}\n"
            f"  💸 Annual div: ${r['annual_div']} · {r['num_ex_divs']} ex-divs in option life\n"
            f"  🎯 Score: *{r['score']}*"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors):
        header = (
            f"💰 *Dividend Collar Arbitrage Scan*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Same-strike collars (80-100% of spot, {DTE_MIN}-{DTE_MAX}d, net credit > 0)\n"
            f"Opportunities: *{len(all_hits)}*\n"
        )
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors[:20])
            if len(err_block) > 1500:
                tickers_only = ", ".join(e.split(":")[0] for e in errors)
                err_block = f"  {tickers_only}\n_(use_ `/logs` _for details)_"
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No qualifying same-strike collars found._"]

        all_hits.sort(key=lambda r: r["score"], reverse=True)

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
