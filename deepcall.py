"""
deepcall.py
Deep-ITM buy-write scanner — configurable downside cushion.

Cushion% N means: strike ≤ (1 - N/100) × spot.
For each ticker × each expiration ≤ MAX_DTE:
  - Iterate calls in the deep-ITM band
  - Sell call at BID (realistic fill)
  - Skip if bid<=0, ask<=0, OI<MIN_OI, or no time value
  - effective_basis = spot - call_bid
  - time_value      = call_bid - (spot - call_strike)
  - return_pct      = time_value / effective_basis * 365/dte * 100
  - Keep if return_pct >= MIN_ANNUAL_RETURN_PCT
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_DTE                 = 60
MIN_ANNUAL_RETURN_PCT   = 4.2
MIN_OI                  = 1
DEFAULT_CUSHION_PCT     = 30.0
MIN_CUSHION_PCT         = 1.0
MAX_CUSHION_PCT         = 50.0
ABSOLUTE_LOWER_STRIKE_PCT = 0.50  # never go below 50% of spot


def clamp_cushion(value: float) -> tuple[float, bool]:
    """Returns (clamped_value, was_clamped)."""
    if value < MIN_CUSHION_PCT:
        return MIN_CUSHION_PCT, True
    if value > MAX_CUSHION_PCT:
        return MAX_CUSHION_PCT, True
    return value, False


def _has_market(option: dict) -> bool:
    bid = option.get("bid") or 0.0
    ask = option.get("ask") or 0.0
    return bid > 0 and ask > 0


def _bid(option: dict) -> float:
    return float(option.get("bid") or 0.0)


def _oi(option: dict) -> int:
    return int(option.get("openInterest") or 0)


class DeepCallScanner:
    def __init__(self, schwab_client):
        self.schwab = schwab_client

    def scan_ticker(self, ticker: str, cushion_pct: float = DEFAULT_CUSHION_PCT) -> list[dict]:
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
        if not call_map:
            return results

        # Strike must be at or below (1 - cushion/100) × spot
        # AND at or above ABSOLUTE_LOWER_STRIKE_PCT × spot
        strike_ceil  = spot * (1.0 - cushion_pct / 100.0)
        strike_floor = spot * ABSOLUTE_LOWER_STRIKE_PCT

        valid_exps = []
        for full_key in call_map.keys():
            try:
                exp_date = full_key.split(":")[0]
                exp_dt   = datetime.strptime(exp_date, "%Y-%m-%d")
                dte      = (exp_dt - datetime.now()).days
                if 1 <= dte <= MAX_DTE:
                    valid_exps.append((exp_date, full_key, dte))
            except Exception:
                continue

        for exp_date, full_key, dte in valid_exps:
            calls = call_map[full_key]

            for strike_str, contracts in calls.items():
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue

                if strike < strike_floor or strike > strike_ceil:
                    continue

                if not contracts:
                    continue
                opt = contracts[0]

                if not _has_market(opt):
                    continue
                if _oi(opt) < MIN_OI:
                    continue

                call_bid = _bid(opt)
                intrinsic = spot - strike
                if intrinsic <= 0:
                    continue

                time_value = call_bid - intrinsic
                if time_value <= 0:
                    continue

                effective_basis = spot - call_bid
                if effective_basis <= 0:
                    continue

                return_pct = time_value / effective_basis * 365.0 / dte * 100.0

                if return_pct < MIN_ANNUAL_RETURN_PCT:
                    continue

                actual_cushion = (spot - strike) / spot * 100.0

                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    strike=strike,
                    call_bid=round(call_bid, 2),
                    intrinsic=round(intrinsic, 2),
                    time_value=round(time_value, 2),
                    effective_basis=round(effective_basis, 2),
                    cushion_pct=round(actual_cushion, 1),
                    annual_return_pct=round(return_pct, 1),
                    oi=_oi(opt),
                ))

        return results

    @staticmethod
    def format_hit(r: dict) -> str:
        return (
            f"🛡️ *{r['ticker']}* @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 sell C ${r['strike']:g} @ ${r['call_bid']} (bid)\n"
            f"  💰 effective basis: *${r['effective_basis']}*\n"
            f"  🛡 cushion: *{r['cushion_pct']}%* drop tolerance\n"
            f"  📊 if called: +${r['time_value']}/sh (=${r['time_value']*100:.0f}/contract)\n"
            f"  🎯 annualized: *{r['annual_return_pct']}%*  ·  OI {r['oi']}"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, cushion_pct=DEFAULT_CUSHION_PCT):
        header = (
            f"🛡️ *Deep-ITM Buy-Write Scan*\n"
            f"Tickers: {scanned} total  ·  ✅ {successful} scanned  ·  ⚠️ {len(errors)} errored\n"
            f"Cushion ≥ *{cushion_pct:g}%*, ≤{MAX_DTE}d, ≥{MIN_ANNUAL_RETURN_PCT}% annualized\n"
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
            return [header + "_No deep-ITM opportunities found._"]

        all_hits.sort(key=lambda r: r["annual_return_pct"], reverse=True)

        chunks, current = [], header
        for hit in all_hits:
            block = DeepCallScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
