"""
orders.py
Order construction + price walking for the trade-from-Telegram flow.

ITM conversion:         BUY stock + SELL call + BUY put  (strike below spot) — NET_DEBIT
Reverse ITM (options):  SELL put  + BUY call             (strike above spot) — NET_CREDIT
DCA collar:             BUY stock + SELL call + BUY put  (same-strike, dividend capture) — NET_DEBIT
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

COMMISSION_PER_CONTRACT = 1.30
INITIAL_MID_FRAC = 0.15
IMPROVE_STEP_FRAC = 0.10
MIN_APY_FLOOR_PCT = 10.0


def build_option_symbol(ticker: str, exp_date: str, strike: float, opt_type: str) -> str:
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    yymmdd = exp_dt.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"
    ticker_padded = ticker.upper().ljust(6, " ")
    return f"{ticker_padded}{yymmdd}{opt_type.upper()}{strike_str}"


def compute_legs_pricing(hit: dict, walk_step: int = 0):
    """
    Walk pricing for ITM conversion or DCA collar (3-leg).
    walk_step=0 starts at mid +/- 15%; each step adds 10% toward unfavorable side.
    Works with both ITM hits (has call_bid/ask) and DCA hits (uses stored call_credit/put_cost).
    """
    call_bid = hit.get("call_bid")
    call_ask = hit.get("call_ask")
    put_bid = hit.get("put_bid")
    put_ask = hit.get("put_ask")

    if None in (call_bid, call_ask, put_bid, put_ask):
        # DCA hits don't store raw bid/ask — use stored mid-adjusted prices
        call_credit = hit.get("call_credit", 0.01)
        put_cost = hit.get("put_cost", 0.01)
        credit_shrink = 0.02 * walk_step
        call_credit = max(0.01, call_credit - credit_shrink / 2)
        put_cost = put_cost + credit_shrink / 2
    else:
        call_mid = (call_bid + call_ask) / 2.0
        call_spread = call_ask - call_bid
        put_mid = (put_bid + put_ask) / 2.0
        put_spread = put_ask - put_bid

        sell_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        call_credit = call_mid - sell_offset * call_spread

        buy_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        put_cost = put_mid + buy_offset * put_spread

    call_credit = round(max(0.01, call_credit), 2)
    put_cost = round(max(0.01, put_cost), 2)
    net_credit = round(call_credit - put_cost, 2)

    spot = hit["spot"]
    strike = hit["strike"]
    dte = hit["dte"]
    gap = spot - strike

    locked_per_share = net_credit - gap
    commission_per_share = COMMISSION_PER_CONTRACT / 100.0
    locked_per_share_after_comm = locked_per_share - commission_per_share
    locked_total = locked_per_share_after_comm * 100.0

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


def compute_reverse_pricing(hit: dict, walk_step: int = 0):
    """
    Walk pricing for reverse ITM options legs (SELL put + BUY call).
    Returns net credit received from the 2 option legs only.
    """
    call_bid = hit.get("call_bid")
    call_ask = hit.get("call_ask")
    put_bid = hit.get("put_bid")
    put_ask = hit.get("put_ask")

    if None in (call_bid, call_ask, put_bid, put_ask):
        put_credit = hit.get("put_cost", 0.01)
        call_cost = hit.get("call_credit", 0.01)
        shrink = 0.02 * walk_step
        put_credit = max(0.01, put_credit - shrink / 2)
        call_cost = call_cost + shrink / 2
    else:
        put_mid = (put_bid + put_ask) / 2.0
        put_spread = put_ask - put_bid
        call_mid = (call_bid + call_ask) / 2.0
        call_spread = call_ask - call_bid

        sell_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        put_credit = put_mid - sell_offset * put_spread

        buy_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        call_cost = call_mid + buy_offset * call_spread

    put_credit = round(max(0.01, put_credit), 2)
    call_cost = round(max(0.01, call_cost), 2)
    net_credit = round(put_credit - call_cost, 2)

    spot = hit["spot"]
    strike = hit["strike"]
    dte = hit["dte"]
    gap = strike - spot

        borrow_cost = hit.get("borrow_cost", 0.0)

    locked_per_share = net_credit - gap
    commission_per_share = COMMISSION_PER_CONTRACT / 100.0
    locked_per_share_after_comm = (
        locked_per_share - commission_per_share - borrow_cost
    )
    locked_total = locked_per_share_after_comm * 100.0

    cost_basis = spot
    if cost_basis <= 0 or dte <= 0:
        apy = 0.0
    else:
        apy = (locked_per_share_after_comm / cost_basis) * (365.0 / dte) * 100.0

    return {
        "put_limit": put_credit,
        "call_limit": call_cost,
        "net_credit": net_credit,
        "locked_per_share": round(locked_per_share, 2),
        "locked_per_share_after_comm": round(locked_per_share_after_comm, 4),
        "locked_total": round(locked_total, 2),
        "apy": round(apy, 1),
        "walk_step": walk_step,
        "borrow_cost": round(borrow_cost, 4),
    }



def can_improve(pricing_next: dict) -> bool:
    return pricing_next["apy"] >= MIN_APY_FLOOR_PCT


def build_itm_conversion_order(hit: dict, pricing: dict) -> dict:
    """
    3-leg NET_DEBIT: BUY 100 stock + SELL call + BUY put. Strike must be < spot.
    Used for both ITM and DCA trades.
    """
    ticker = hit["ticker"].upper()
    exp_date = hit["exp_date"]
    strike = float(hit["strike"])
    spot = float(pricing["stock_price"])

    if strike >= spot:
        raise ValueError(
            f"ITM conversion requires strike < spot. Got strike={strike}, spot={spot}. "
            f"Ticker={ticker}. Aborting to prevent flipped order."
        )

    call_limit = float(pricing["call_limit"])
    put_limit = float(pricing["put_limit"])
    net_debit_per_share = spot - call_limit + put_limit

    if net_debit_per_share <= 0:
        raise ValueError(
            f"Net debit must be positive. Got {net_debit_per_share:.4f}. "
            f"spot={spot}, call_credit={call_limit}, put_cost={put_limit}. Aborting."
        )

    if net_debit_per_share < strike * 0.5 or net_debit_per_share > spot * 1.1:
        raise ValueError(
            f"Net debit {net_debit_per_share:.2f} sanity-check failed for "
            f"strike={strike}, spot={spot}. Aborting."
        )

    call_symbol = build_option_symbol(ticker, exp_date, strike, "C")
    put_symbol = build_option_symbol(ticker, exp_date, strike, "P")
    price_str = f"{net_debit_per_share:.2f}"

    logger.info(
        f"build_itm_conversion_order: ticker={ticker} exp={exp_date} strike={strike} "
        f"spot={spot} call_credit={call_limit} put_cost={put_limit} NET_DEBIT={price_str}"
    )

    return {
        "orderType": "NET_DEBIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": price_str,
        "orderLegCollection": [
            {
                "orderLegType": "EQUITY",
                "instruction": "BUY",
                "quantity": 100,
                "instrument": {"symbol": ticker, "assetType": "EQUITY"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": call_symbol, "assetType": "OPTION"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": put_symbol, "assetType": "OPTION"},
            },
        ],
    }


def build_reverse_itm_order(hit: dict, pricing: dict) -> dict:
    """
    2-leg NET_CREDIT: SELL put + BUY call. Strike must be > spot.
    User shorts stock manually.
    """
    ticker = hit["ticker"].upper()
    exp_date = hit["exp_date"]
    strike = float(hit["strike"])
    spot = float(hit["spot"])

    if strike <= spot:
        raise ValueError(
            f"Reverse ITM requires strike > spot. Got strike={strike}, spot={spot}. "
            f"Ticker={ticker}. Aborting."
        )

    put_limit = float(pricing["put_limit"])
    call_limit = float(pricing["call_limit"])
    net_credit = round(put_limit - call_limit, 2)

    if net_credit <= 0:
        raise ValueError(
            f"Net credit must be positive for reverse ITM. Got {net_credit:.4f}. "
            f"put_credit={put_limit}, call_cost={call_limit}. Aborting."
        )

    call_symbol = build_option_symbol(ticker, exp_date, strike, "C")
    put_symbol = build_option_symbol(ticker, exp_date, strike, "P")
    price_str = f"{net_credit:.2f}"

    logger.info(
        f"build_reverse_itm_order: ticker={ticker} exp={exp_date} strike={strike} "
        f"spot={spot} put_credit={put_limit} call_cost={call_limit} NET_CREDIT={price_str}"
    )

    return {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": price_str,
        "orderLegCollection": [
            {
                "orderLegType": "OPTION",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": put_symbol, "assetType": "OPTION"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": call_symbol, "assetType": "OPTION"},
            },
        ],
    }


def format_order_preview(hit: dict, pricing: dict, next_pricing: dict = None) -> str:
    ticker = hit["ticker"]
    walk = pricing["walk_step"]
    walk_label = "initial" if walk == 0 else f"retry #{walk}"

    spot = pricing["stock_price"]
    strike = hit["strike"]
    net_debit = spot - pricing["call_limit"] + pricing["put_limit"]

    lines = [
        f"⚠️ *CONFIRM ITM CONVERSION ({walk_label})*",
        f"*{ticker}* @ ${spot}  ·  strike ${strike:g}  ·  exp {hit['exp_date']} ({hit['dte']}d)",
        f"",
        f"📦 3 legs (atomic combo):",
        f"  · BUY 100 shares @ ~${spot:.2f}",
        f"  · SELL 1 C ${strike:g} @ limit ${pricing['call_limit']:.2f}",
        f"  · BUY  1 P ${strike:g} @ limit ${pricing['put_limit']:.2f}",
        f"",
        f"💳 *NET DEBIT: ${net_debit:.2f}/sh (= ${net_debit*100:.0f} total)*",
        f"📊 Locked profit after $1.30 comm: *${pricing['locked_total']:.2f}*",
        f"📈 APY: *{pricing['apy']:.1f}%*",
        f"",
    ]
    if next_pricing and can_improve(next_pricing):
        lines.append(f"_If unfilled in 30s: improve → {next_pricing['apy']:.1f}% APY_")
    lines.append(f"Reply `YES {ticker}` within 60s to submit.")
    return "\n".join(lines)


def format_dca_order_preview(hit: dict, pricing: dict, next_pricing: dict = None) -> str:
    """
    DCA-specific confirmation preview. Shows dividend context alongside
    the standard locked profit so user can make an informed decision.
    """
    ticker = hit["ticker"]
    walk = pricing["walk_step"]
    walk_label = "initial" if walk == 0 else f"retry #{walk}"

    spot = pricing["stock_price"]
    strike = hit["strike"]
    net_debit = spot - pricing["call_limit"] + pricing["put_limit"]

    # Dividend context from the hit
    score_dollars = hit.get("score_dollars", 0)
    score_apy = hit.get("score_apy", 0)
    expected_div = hit.get("expected_div_dollars", 0)
    annual_div = hit.get("annual_div", 0)
    freq = hit.get("freq", "?")
    num_ex_divs = hit.get("expected_ex_divs_collected", 0)
    safety_dollars = hit.get("safety_dollars", 0)
    safety_sign = "+" if safety_dollars >= 0 else ""

    lines = [
        f"⚠️ *CONFIRM DCA COLLAR ({walk_label})*",
        f"*{ticker}* @ ${spot}  ·  strike ${strike:g}  ·  exp {hit['exp_date']} ({hit['dte']}d)",
        f"",
        f"📦 3 legs (atomic combo):",
        f"  · BUY 100 shares @ ~${spot:.2f}",
        f"  · SELL 1 C ${strike:g} @ limit ${pricing['call_limit']:.2f}",
        f"  · BUY  1 P ${strike:g} @ limit ${pricing['put_limit']:.2f}",
        f"",
        f"💳 *NET DEBIT: ${net_debit:.2f}/sh (= ${net_debit*100:.0f} total)*",
        f"🛡️ Safety (options only): *{safety_sign}${safety_dollars:.2f}/sh*",
        f"💸 Div: ${annual_div}/yr · {num_ex_divs} ex-div(s) → +${expected_div:.2f}/sh expected",
        f"🎯 *Score (with div): ${score_dollars:.2f}/sh · {score_apy:.1f}% APY*",
        f"",
    ]
    if next_pricing and can_improve(next_pricing):
        lines.append(f"_If unfilled in 30s: improve → {next_pricing['apy']:.1f}% APY_")
    lines.append(f"Reply `YES {ticker}` within 60s to submit.")
    return "\n".join(lines)


def format_reverse_order_preview(hit: dict, pricing: dict, next_pricing: dict = None) -> str:
    ticker = hit["ticker"]
    walk = pricing["walk_step"]
    walk_label = "initial" if walk == 0 else f"retry #{walk}"

    strike = hit["strike"]
    spot = hit["spot"]

    lines = [
        f"⚠️ *CONFIRM REVERSE ITM — OPTIONS ONLY ({walk_label})*",
        f"*{ticker}* @ ${spot}  ·  strike ${strike:g}  ·  exp {hit['exp_date']} ({hit['dte']}d)",
        f"",
        f"📦 2 legs (options only — short stock manually!):",
        f"  · SELL 1 P ${strike:g} @ limit ${pricing['put_limit']:.2f}",
        f"  · BUY  1 C ${strike:g} @ limit ${pricing['call_limit']:.2f}",
        f"",
        f"💰 *NET CREDIT: ${pricing['net_credit']:.2f} (= ${pricing['net_credit']*100:.0f} total)*",
        f"📊 Locked profit after $1.30 comm: *${pricing['locked_total']:.2f}*",
        f"📈 APY (options legs only): *{pricing['apy']:.1f}%*",
        f"",
        f"⚠️ *SHORT {ticker} stock separately on Schwab before this fills.*",
        f"",
    ]
    if next_pricing and can_improve(next_pricing):
        lines.append(f"_If unfilled in 30s: improve → {next_pricing['apy']:.1f}% APY_")
    lines.append(f"Reply `R {ticker}` within 60s to submit.")
    return "\n".join(lines)