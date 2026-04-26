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

            call_exp_key = next((k for k in call_map i
