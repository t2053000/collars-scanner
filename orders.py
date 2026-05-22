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
    # call_credit_initial = call_mid - 0.15 * call_spread  ==>  call_mid = ?
    # Without spread info, we can't perfectly walk. So: hit dict must include extra fields:
    #   call_bid, call_ask, put_bid, put_ask
    # If they're not present, fall back to no-walk pricing.

    call_bid = hit.get("call_bid")
    call_ask = hit.get("call_ask")
    put_bid = hit.get("put_bid")
    put_ask = hit.get("put_ask")

    if None in (call_bid, call_ask, put_bid, put_ask):
        # Fallback: can't walk, use stored prices and shrink the credit on each improvement
        call_credit = hit["call_credit"]
        put_cost = hit["put_cost"]
        # crude: shrink credit by $0.02 per walk_step
        credit_shrink = 0.02 * walk_step
        call_credit = max(0.01, call_credit - credit_shrink / 2)
        put_cost = put_cost + credit_shrink / 2
    else:
        call_mid = (call_bid + call_ask) / 2.0
        call_spread = call_ask - call_bid
        put_mid = (put_bid + put_ask) / 2.0
        put_spread = put_ask - put_bid

        # Selling call: start mid-15%, walk toward bid (lower)
        # walk_step 0: mid - 15% of spread
        # walk_step 1: mid - 5% of spread (improve by 10%)
        # walk_step 2: mid + 5% of spread (we're now BELOW mid — accepting worse)
        sell_offset_frac = INITIAL_MID_FRAC - (IMPROVE_STEP_FRAC * walk_step)
        # offset_frac controls how much above mid we sell (negative = below mid = closer to bid)
        # walk_step=0: +0.15 (mid - 15% of spread from mid downward... wait, "sell" means we want HIGH price)
        # Let me redo: for SELL we want HIGH price. Best (most edge) = ask. Worst = bid.
        # Initial position: mid - 15% of spread (closer to bid by 15%). This is what scanner does.
        # We want to start at this conservative-edge price and walk TOWARD bid as we improve.
        # So actual SELL limit = mid - sell_offset_frac * spread (where offset starts at 0.15 and grows)
        sell_offset_frac_actual = INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step)
        # Cap at full spread (i.e., at the bid)
        sell_offset_frac_actual = min(sell_offset_frac_actual, 0.5)
        call_credit = call_mid - sell_offset_frac_actual * call_spread

        # For BUY (put), we want LOW price. Best = bid. Worst = ask.
        # Start at mid + 15% spread, walk toward ask.
        buy_offset_frac_actual = INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step)
        buy_offset_frac_actual = min(buy_offset_frac_actual, 0.5)
        put_cost = put_mid + buy_offset_frac_actual * put_spread

    call_credit = round(call_credit, 2)
    put_cost = round(put_cost, 2)
    net_credit = round(call_credit - put_cost, 2)

    spot = hit["spot"]
    strike = hit["strike"]
    dte = hit["dte"]
    gap = spot - strike

    # Locked profit per share (before commissions)
    locked_per_share = net_credit - gap

    # Commissions per share (1 contract = 100 shares)
    commission_per_share = COMMISSION_PER_CONTRACT / 100.0
    locked_per_share_after_comm = locked_per_share - commission_per_share
    locked_total = locked_per_share_after_comm * 100.0

    # APY based on capital deployed = strike * 100 (what you get back) ... or cost_basis = spot - net_credit per share
    cost_basis_per_share = spot - net_credit
    if cost_basis_per_share <= 0 or dte <= 0:
        apy = 0.0
    else:
        apy = (locked_per_share_after_comm / cost_basis_per_share) * (365.0 / dte) * 100.0

    return {
        "call_limit": call_credit,
        "put_limit": put_cost,
        "stock_price": spot,
        "net_credit": net_credit,
        "locked_per_share": round(locked_per_share, 2),
        "locked_per_share_after_comm": round(locked_per_share_after_comm, 4),
        "locked_total": round(locked_total, 2),
        "apy": round(apy, 1),
        "walk_step": walk_step,
        "cost_basis_per_share": round(cost_basis_per_share, 2),
    }


def can_improve(pricing_next: dict) -> bool:
    """Return True if the next walk step would still yield APY >= floor."""
    return pricing_next["apy"] >= MIN_APY_FLOOR_PCT


def build_itm_conversion_order(hit: dict, pricing: dict) -> dict:
    """
    Build a Schwab MULTI-LEG NET_DEBIT order combining all 3 legs.

    Note: Schwab supports complex orders with stock + option legs via orderStrategyType=SINGLE
    with multiple orderLegCollection entries when complexOrderStrategyType = "CUSTOM".

    For an ITM conversion:
      - BUY stock (limit at current ask or slightly above for fill)
      - SELL_TO_OPEN call (limit at call_credit)
      - BUY_TO_OPEN put (limit at put_cost)

    Net debit per share = stock_price - call_credit + put_cost
    Total order debit (per contract) = (stock_price - call_credit + put_cost) * 100
    """
    ticker = hit["ticker"].upper()
    exp_date = hit["exp_date"]
    strike = hit["strike"]

    call_symbol = build_option_symbol(ticker, exp_date, strike, "C")
    put_symbol = build_option_symbol(ticker, exp_date, strike, "P")

    stock_price = pricing["stock_price"]
    call_limit = pricing["call_limit"]
    put_limit = pricing["put_limit"]

    # Net debit per spread = pay stock - receive call + pay put, per share, times 100
    net_debit_per_share = stock_price - call_limit + put_limit
    net_debit_total = round(net_debit_per_share * 100.0, 2)

    order = {
        "orderType": "NET_DEBIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": str(round(net_debit_per_share, 2)),
        "orderLegCollection": [
            {
                "orderLegType": "EQUITY",
                "instruction": "BUY",
                "quantity": 100,
                "instrument": {
                    "symbol": ticker,
                    "assetType": "EQUITY",
                },
            },
            {
                "orderLegType": "OPTION",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {
                    "symbol": call_symbol,
                    "assetType": "OPTION",
                },
            },
            {
                "orderLegType": "OPTION",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
                "instrument": {
                    "symbol": put_symbol,
                    "assetType": "OPTION",
                },
            },
        ],
    }
    return order


def format_order_preview(hit: dict, pricing: dict, next_pricing: dict = None) -> str:
    """Pretty-printed order preview for confirmation message."""
    ticker = hit["ticker"]
    walk = pricing["walk_step"]
    walk_label = "initial" if walk == 0 else f"retry #{walk}"

    lines = [
        f"⚠️ *CONFIRM ITM CONVERSION ({walk_label})*",
        f"*{ticker}* @ ${hit['spot']}  ·  exp {hit['exp_date']} ({hit['dte']}d)",
        f"",
        f"📦 1 spread = 3 legs:",
        f"  · BUY 100 shares @ ~${pricing['stock_price']:.2f}",
        f"  · SELL 1 C ${hit['strike']:g} @ limit ${pricing['call_limit']:.2f}",
        f"  · BUY  1 P ${hit['strike']:g} @ limit ${pricing['put_limit']:.2f}",
        f"",
        f"💵 Net credit on options: ${pricing['net_credit']:.2f}/sh",
        f"📊 Cost basis: ${pricing['cost_basis_per_share']:.2f}/sh",
        f"🎯 Locked profit (after $1.30 comm): *${pricing['locked_total']:.2f}* total",
        f"📈 APY: *{pricing['apy']:.1f}%*",
        f"",
    ]
    if next_pricing and can_improve(next_pricing):
        lines.append(
            f"_If filled doesn't happen in 30s, next improve → {next_pricing['apy']:.1f}% APY_"
        )
    lines.append(f"Reply `YES {ticker}` within 60s to submit.")
    return "\n".join(lines)
