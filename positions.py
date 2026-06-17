"""
positions.py
/positions command — show all positions expiring by this Friday EOD.
Groups by underlying ticker, shows projected P/L at expiration
assuming spot stays flat, with APY annualized.
"""

import logging
from collections import defaultdict
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

COMMISSION_PER_CONTRACT = 1.30  # per leg


def _this_friday() -> date:
    """Return this week's Friday date regardless of what day today is."""
    today = date.today()
    # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        return today  # today is Friday
    return today + timedelta(days=days_until_friday)


def _parse_option_symbol(symbol: str) -> Optional[dict]:
    """
    Parse OCC option symbol from the right — works for any ticker length.
    Last 15 chars are always: YYMMDD(6) + C/P(1) + strike(8)
    e.g. 'AAPL  260620C00295000' or 'GOOGL 260620P00185000'
    """
    try:
        symbol = symbol.strip()
        if len(symbol) < 15:
            return None
        root   = symbol[:-15].strip()
        date_s = symbol[-15:-9]
        right  = symbol[-9]
        strike = int(symbol[-8:]) / 1000.0
        exp    = datetime.strptime(date_s, "%y%m%d").date()
        if right not in ("C", "P"):
            return None
        if not root:
            return None
        return {"ticker": root, "expiry": exp, "right": right, "strike": strike}
    except Exception:
        return None


def compute_positions(raw_positions: list) -> str:
    """
    Given raw Schwab positions list, compute projected P/L for all
    tickers with at least one option expiring today through this Friday.
    Returns formatted message string.
    """
    today  = date.today()
    friday = _this_friday()
    max_dte = (friday - today).days
    # dte for APY — use days until friday, min 1 to avoid div/0
    dte = max(max_dte, 1)

    # Separate stock and option positions
    stocks  = {}   # ticker → {qty, avg_price, mark}
    options = []   # list of option position dicts

    for pos in raw_positions:
        instrument = pos.get("instrument", {})
        asset_type = instrument.get("assetType", "")
        symbol     = instrument.get("symbol", "")
        long_qty   = float(pos.get("longQuantity",  0))
        short_qty  = float(pos.get("shortQuantity", 0))
        qty        = long_qty - short_qty
        avg_price  = float(pos.get("averagePrice", 0))
        mkt_value  = float(pos.get("marketValue",  0))

        if asset_type == "EQUITY":
            mark = mkt_value / qty if qty != 0 else 0.0
            stocks[symbol] = {
                "qty":       qty,
                "avg_price": avg_price,
                "mark":      mark,
            }

        elif asset_type == "OPTION":
            parsed = _parse_option_symbol(symbol)
            if parsed is None:
                logger.debug(f"Could not parse option symbol: {symbol}")
                continue
            # mark per share (Schwab marketValue is for the whole position)
            contracts = abs(qty)
            mark_per_share = mkt_value / (contracts * 100) if contracts > 0 else 0.0
            options.append({
                "symbol":    symbol,
                "ticker":    parsed["ticker"],
                "expiry":    parsed["expiry"],
                "right":     parsed["right"],
                "strike":    parsed["strike"],
                "qty":       qty,
                "avg_price": avg_price,
                "mark":      mark_per_share,
            })

    # Filter options expiring today through this Friday (inclusive)
    window_options = [
        o for o in options
        if today <= o["expiry"] <= friday
    ]

    # Get tickers with at least one option in window
    window_tickers = set(o["ticker"] for o in window_options)
    if not window_tickers:
        return (
            f"📭 No positions expiring between today "
            f"({today.strftime('%b %d')}) and "
            f"Friday ({friday.strftime('%b %d')})."
        )

    lines = []
    lines.append(
        f"📊 *Positions expiring by {friday.strftime('%a %b %d')}*\n"
        f"{'─' * 32}"
    )

    total_pl = 0.0

    for ticker in sorted(window_tickers):
        ticker_opts = [o for o in window_options if o["ticker"] == ticker]
        stock       = stocks.get(ticker)

        # Get spot from stock mark
        spot = stock["mark"] if stock and stock["mark"] > 0 else None
        if spot is None or spot <= 0:
            # Fall back to nearest strike as rough proxy
            spot = ticker_opts[0]["strike"] if ticker_opts else 0.0
        if spot <= 0:
            continue

        # ---------------------------------------------------------------
        # Projected P/L at expiration (spot stays flat)
        # Long  call: max(spot - strike, 0) - avg_price  per share × 100 × contracts
        # Short call: avg_price - max(spot - strike, 0)
        # Long  put:  max(strike - spot, 0) - avg_price
        # Short put:  avg_price - max(strike - spot, 0)
        # Stock:      (spot - avg_price) × qty
        # ---------------------------------------------------------------

        stock_pl   = 0.0
        stock_cost = 0.0
        if stock:
            stock_pl   = (spot - stock["avg_price"]) * stock["qty"]
            stock_cost = stock["avg_price"] * abs(stock["qty"])

        options_pl   = 0.0
        options_cost = 0.0
        leg_strs     = []

        for o in sorted(ticker_opts, key=lambda x: (x["expiry"], x["strike"], x["right"])):
            contracts = abs(o["qty"])
            if o["right"] == "C":
                intrinsic = max(spot - o["strike"], 0)
            else:
                intrinsic = max(o["strike"] - spot, 0)

            if o["qty"] > 0:   # long
                leg_pl = (intrinsic - o["avg_price"]) * contracts * 100
            else:              # short
                leg_pl = (o["avg_price"] - intrinsic) * contracts * 100

            leg_pl      -= COMMISSION_PER_CONTRACT * contracts
            options_pl  += leg_pl
            options_cost += o["avg_price"] * contracts * 100

            direction = "long" if o["qty"] > 0 else "short"
            exp_str   = o["expiry"].strftime("%m/%d")
            leg_strs.append(
                f"{direction} {contracts:.0f}x "
                f"{o['right']}${o['strike']:g} {exp_str} "
                f"@ ${o['avg_price']:.2f}"
            )

        total_ticker_pl = stock_pl + options_pl
        total_pl       += total_ticker_pl

        cost_basis = stock_cost if stock_cost > 0 else options_cost
        if cost_basis > 0 and dte > 0:
            apy = (total_ticker_pl / cost_basis) * (365.0 / dte) * 100.0
        else:
            apy = 0.0

        # Stock leg string
        if stock:
            direction = "long" if stock["qty"] > 0 else "short"
            stock_str = f"{direction} {abs(stock['qty']):.0f}sh @ ${stock['avg_price']:.2f}"
            leg_strs.insert(0, stock_str)

        pl_emoji = "✅" if total_ticker_pl >= 0 else "🔴"
        apy_str  = f"{apy:+.1f}%" if cost_basis > 0 else "n/a"

        lines.append(
            f"\n*{ticker}* @ ${spot:.2f}\n"
            + "\n".join(f"  {l}" for l in leg_strs) + "\n"
            f"  {pl_emoji} P/L: *${total_ticker_pl:+.0f}* · APY: *{apy_str}*"
        )

    lines.append(f"\n{'─' * 32}")
    total_emoji = "✅" if total_pl >= 0 else "🔴"
    lines.append(f"{total_emoji} *Total P/L: ${total_pl:+.0f}*")

    return "\n".join(lines)
