"""
scanner.py
Scans a ticker for positive-expectancy collar opportunities.

For each ticker, for each of the next 10 expirations (same expiry both legs):
  - Sell nearest call strike ABOVE spot
  - Buy  nearest put  strike BELOW spot
  - Skip legs with no market (bid<=0 or ask<=0)

Three yearly-yield scenarios are computed:
  net_premium = call_mid - put_mid
  POS = if stock >= call_strike at expiry
        ((call_strike - spot) + net_premium) / spot * 365/dte * 100
  NEU = if stock unchanged at expiry
        net_premium / spot * 365/dte * 100
  NEG = if stock <= put_strike at expiry
        ((put_strike - spot) + net_premium) / spot * 365/dte * 100

Filter: keep hits where NEG yearly > MIN_NEG_YEARLY_PCT (= 12%, ~1%/mo).
Sort:   by NEG yearly descending.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_EXPIRATIONS     = 10
MIN_NEG_YEARLY_PCT  = 12.0   # roughly 1% per month


def _mid_or_none(option: dict) -> float | None:
    bid = option.get("bid") or 0.0
    ask = option.get("ask") or 0.0
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


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


class CollarScanner:
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
            call_exp_key = next((k for k in call_map if k.startswith(exp_date + ":")), None)
            put_exp_key  = next((k for k in put_map  if k.startswith(exp_date + ":")), None)
            if not call_exp_key or not put_exp_key:
                continue

            calls = call_map[call_exp_key]
            puts  = put_map[put_exp_key]

            call_strikes = sorted(float(s) for s in calls if float(s) > spot)
            put_strikes  = sorted((float(s) for s in puts if float(s) < spot), reverse=True)
            if not call_strikes or not put_strikes:
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

            call_mid = _mid_or_none(call_contracts[0])
            put_mid  = _mid_or_none(put_contracts[0])
            if call_mid is None or put_mid is None:
                continue

            exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            dte    = (exp_dt - datetime.now()).days
            if dte < 1:
                continue

            net_premium = call_mid - put_mid
            ann_factor  = 365.0 / dte

            pos_yearly = ((call_strike - spot) + net_premium) / spot * ann_factor * 100.0
            neu_yearly = net_premium                          / spot * ann_factor * 100.0
            neg_yearly = ((put_strike  - spot) + net_premium) / spot * ann_factor * 100.0

            if neg_yearly > MIN_NEG_YEARLY_PCT:
                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    call_strike=call_strike,
                    call_mid=round(call_mid, 2),
                    put_strike=put_strike,
                    put_mid=round(put_mid, 2),
                    net_premium=round(net_premium, 2),
                    pos_yearly=round(pos_yearly, 1),
                    neu_yearly=round(neu_yearly, 1),
                    neg_yearly=round(neg_yearly, 1),
                ))

        return results

    @staticmethod
    def format_hit(r: dict) -> str:
        return (
            f"*{r['ticker']}*  @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 sell C ${r['call_strike']} @ ${r['call_mid']}\n"
            f"  🛡️ buy  P ${r['put_strike']} @ ${r['put_mid']}\n"
            f"  📈 POS/NEU/NEG yearly:  *{r['pos_yearly']}% / {r['neu_yearly']}% / {r['neg_yearly']}%*"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors):
        header = (
            f"🔎 *Collar Scan Complete*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Opportunities (NEG yearly > {MIN_NEG_YEARLY_PCT:g}%): *{len(all_hits)}*\n"
        )
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors)
            if len(err_block) > 1500:
                tickers_only = ", ".join(e.split(":")[0] for e in errors)
                err_block = f"  {tickers_only}\n_(use_ `/logs` _for details)_"
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No positive-edge collars found._"]

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
