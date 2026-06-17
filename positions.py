"""
positions.py
/positions command — show all positions expiring this Friday.
Groups by underlying ticker, shows projected P/L at expiration
assuming spot stays flat, with APY annualized.

Strategy detection:
  - ITM Conversion:  long stock + short call + long put (same strike, same expiry)
  - Reverse ITM:     short stock + short put + long call (same strike, same expiry)
  - Naked option(s): any option without matched stock/hedge
  - Stock only:      excluded (no option expiring Friday)
"""

import logging
from collections import defaultdict
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

COMMISSION_PER_CONTRACT = 1.30  # per leg


def _next_friday() -> date:
    """Return the date of the next Friday (or today if today is Friday)."""
    today = date.today()
    days_until_friday = (4 - today.weekday()) % 7  # 4 = Friday
    return today + timedelta(days=days_until_friday)


def _parse_option_symbol(symbol: str) -> Optional[dict]:
    """
    Parse OCC option symbol: TICKER  YYMMDD C/P STRIKE(x1000)
    e.g. 'AAPL  260620C00295000'
    Returns dict with ticker, expiry (date), right, strike, or None on failure.
    """
    try:
        symbol = symbol.strip()
        # Find where the date starts — 6 digits after spaces
        # Format: TICKER(6 padded) YYMMDD RIGHT STRIKE(8 digits)
        # e.g. 'AAPL  260620C00295000'
        if len(symbol) < 15:
            return None
        # Root is first 6 chars, stripped
        root   = symbol[:6].strip()
        date_s = symbol[6:12]
        right  = symbol[12]
        strike_raw = symbol[13:]
        exp = datetime.strptime(date_s, "%y%m%d").date()
        strike = int(strike_raw) / 1000.0
        return {"ticker": root, "expiry": exp, "right": right, "strike": strike}
    except Exception:
        return None


def compute_positions(raw_positions: list) -> str:
    """
    Given raw Schwab positions list, compute projected P/L for all
    tickers with at least one option expiring this Friday.
    Returns formatted message string.
    """
    friday = _next_friday()
    dte = (friday - date.today()).days
    if dte == 0:
        dte = 1  # avoid division by zero on expiration day

    # Separate stock and option positions
    stocks = {}   # ticker → {qty, avg_price, mark}
    options = []  # list of option position dicts

    for pos in raw_positions:
        instrument = pos.get("instrument", {})
        asset_type = instrument.get("assetType", "")
        symbol     = instrument.get("symbol", "")
        qty        = float(pos.get("longQuantity", 0)) - float(pos.get("shortQuantity", 0))
        avg_price  = float(pos.get("averagePrice", 0))
        mark       = float(pos.get("marketValue", 0)) / abs(qty) / 100 \
                     if asset_type == "OPTION" and qty != 0 else \
                     float(pos.get("marketValue", 0)) / abs(qty) \
                     if qty != 0 else 0.0

        if asset_type == "EQUITY":
            stocks[symbol] = {
                "qty": qty,
                "avg_price": avg_price,
                "mark": float(pos.get("marketValue", 0)) / qty if qty != 0 else 0.0,
            }
        elif asset_type == "OPTION":
            parsed = _parse_option_symbol(symbol)
            if parsed is None:
                continue
            options.append({
                "symbol":  symbol,
                "ticker":  parsed["ticker"],
                "expiry":  parsed["expiry"],
                "right":   parsed["right"],   # 'C' or 'P'
                "strike":  parsed["strike"],
                "qty":     qty,               # +ve = long, -ve = short
                "avg_price": avg_price,       # per share (already /100 by Schwab)
                "mark":    mark,              # current mark per share
            })

    # Filter options to only those expiring this Friday
    friday_options = [o for o in options if o["expiry"] == friday]

    # Get tickers with at least one Friday option
    friday_tickers = set(o["ticker"] for o in friday_options)
    if not friday_tickers:
        return f"📭 No positions expiring this Friday ({friday.strftime('%b %d')})."

    lines = []
    lines.append(
        f"📊 *Positions expiring {friday.strftime('%a %b %d')}* · {dte}d\n"
        f"{'─' * 32}"
    )

    total_pl = 0.0

    for ticker in sorted(friday_tickers):
        ticker_opts = [o for o in friday_options if o["ticker"] == ticker]
        stock       = stocks.get(ticker)

        # Get current spot from stock mark or from option strike as proxy
        spot = stock["mark"] if stock else None
        if spot is None:
            # Try to infer spot from options (rough)
            spot = ticker_opts[0]["strike"]

        # ---------------------------------------------------------------
        # Projected P/L at expiration (spot stays flat)
        # For each option leg:
        #   Long call:  max(spot - strike, 0) - avg_price  (per share × 100 × contracts)
        #   Short call: avg_price - max(spot - strike, 0)
        #   Long put:   max(strike - spot, 0) - avg_price
        #   Short put:  avg_price - max(strike - spot, 0)
        # For stock:
        #   (spot - avg_price) × qty
        # ---------------------------------------------------------------

        stock_pl = 0.0
        stock_cost = 0.0
        if stock:
            stock_pl   = (spot - stock["avg_price"]) * stock["qty"]
            stock_cost = stock["avg_price"] * abs(stock["qty"])

        options_pl   = 0.0
        options_cost = 0.0
        legs = []
        for o in ticker_opts:
            contracts = abs(o["qty"])
            if o["right"] == "C":
                intrinsic = max(spot - o["strike"], 0)
            else:
                intrinsic = max(o["strike"] - spot, 0)

            if o["qty"] > 0:   # long
                leg_pl = (intrinsic - o["avg_price"]) * contracts * 100
            else:              # short
                leg_pl = (o["avg_price"] - intrinsic) * contracts * 100

            # Commission
            leg_pl -= COMMISSION_PER_CONTRACT * contracts

            options_pl   += leg_pl
            options_cost += o["avg_price"] * contracts * 100
            legs.append(o)

        total_ticker_pl = stock_pl + options_pl
        total_pl += total_ticker_pl

        # Cost basis for APY — use stock cost if present, else options cost
        cost_basis = stock_cost if stock_cost > 0 else options_cost
        if cost_basis > 0 and dte > 0:
            apy = (total_ticker_pl / cost_basis) * (365.0 / dte) * 100.0
        else:
            apy = 0.0

        # Format legs summary
        leg_parts = []
        if stock:
            direction = "long" if stock["qty"] > 0 else "short"
            leg_parts.append(f"{direction} {abs(stock['qty']):.0f}sh @ ${stock['avg_price']:.2f}")
        for o in sorted(legs, key=lambda x: (x["strike"], x["right"])):
            direction = "long" if o["qty"] > 0 else "short"
            right_str = "C" if o["right"] == "C" else "P"
            leg_parts.append(
                f"{direction} {abs(o['qty']):.0f}x {right_str}${o['strike']:g} @ ${o['avg_price']:.2f}")

        pl_emoji = "✅" if total_ticker_pl >= 0 else "🔴"
        apy_str  = f"{apy:+.1f}%" if cost_basis > 0 else "n/a"

        lines.append(
            f"\n*{ticker}* @ ${spot:.2f}\n"
            f"  {' · '.join(leg_parts)}\n"
            f"  {pl_emoji} P/L: *${total_ticker_pl:+.0f}* · APY: *{apy_str}*"
        )

    lines.append(f"\n{'─' * 32}")
    total_emoji = "✅" if total_pl >= 0 else "🔴"
    lines.append(f"{total_emoji} *Total P/L: ${total_pl:+.0f}*")

    return "\n".join(lines)
