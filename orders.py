"""
orders.py
Order construction + price walking for the trade-from-Telegram flow.

Currently supports ITM conversion only:
  Buy 100 shares + Sell 1 ITM call + Buy 1 same-strike put

Single combo order with all 3 legs, atomic execution (Schwab MULTI-LEG, complex order type).

Price walking:
  Initial: mid +/- 15% of bid-ask spread (favorable)
  Each "Improve" tap: walk 10% closer to unfavorable side
  Stop at 10% APY (after commissions) — no further improvement allowed

Commission assumption: $0.65 per option leg, $0 for stock = $1.30/contract on ITM conversion.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Commission per ITM conversion contract (2 option legs)
COMMISSION_PER_CONTRACT = 1.30

# Price walking parameters
INITIAL_MID_FRAC = 0.15      # start at mid +/- 15% of spread
IMPROVE_STEP_FRAC = 0.10     # each Improve tap walks 10% closer to unfavorable side
MIN_APY_FLOOR_PCT = 10.0     # stop allowing improvements below this APY


def build_option_symbol(ticker: str, exp_date: str, strike: float, opt_type: str) -> str:
    """
    Build the OSI (OCC) option symbol Schwab uses.
    Format: SYM  YYMMDDC00012500   (call), SYM  YYMMDDP00012500  (put)
    Note: ticker is left-padded to 6 chars, strike is integer × 1000, 8 digits.
    """
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    yymmdd = exp_dt.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"
    ticker_padded = ticker.upper().ljust(6, " ")
    return f"{ticker_padded}{yymmdd}{opt_type.upper()}{strike_str}"


def compute_legs_pricing(hit: dict, walk_step: int = 0):
    """
    Given a /itm scanner hit dict and a walk step number (0 = initial, 1+ = improvements),
    return dict with limit prices for each leg and a projected APY.

    walk_step controls how aggressive prices get:
      0 = initial mid +/- 15%
      1 = mid +/- 5%
      2 = mid + 5% (sells) / mid - 5% (buys) — past mid, paying spread
      ... and so on
    """
    # Need bid/ask of each option to compute walked prices.
    # Hit only stores mid-adjusted price already. Reconstruct from net_credit/call_credit/put_cost.
    # NOTE: scanner stores call_credit at MID-15% and put_cost at MID+15% (initial walk_step=0)
    # To re-derive bid/ask we'd need raw quotes; instead we store deltas.

    # Approach: derive "mid prices" from the initial walk_step=0 values.
    # call_credit_initial = call_mid - 0.15 * call_spread  ==>  call_m
