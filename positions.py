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
    Given raw Schwab positions list, compute projected P/L grouped
    by expiration date, then by ticker within each expiration.
    Shows expected profit and capital freed per expiration.
    """
    today = date.today()

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

    # Filter to options expiring today or later
    future_options = [o for o in options if o["expiry"] >= today]
    if not future_options:
        return "📭 No open option positions."

    # Get all expiration dates, sorted
    expirations = sorted(set(o["expiry"] for o in future_options))

    lines = []
    lines.append(f"📊 *Open positions — {len(future_options)} legs*\n{'─' * 32}")

    grand_total_pl = 0.0
    grand_total_capital = 0.0

    for exp in expirations:
        exp_options = [o for o in future_options if o["expiry"] == exp]
        exp_tickers = sorted(set(o["ticker"] for o in exp_options))
        dte = max((exp - today).days, 1)

        exp_pl = 0.0
        exp_capital = 0.0

        ticker_lines = []

        for ticker in exp_tickers:
            ticker_opts = [o for o in exp_options if o["ticker"] == ticker]
            stock = stocks.get(ticker)

            # Get spot from stock mark
            spot = stock["mark"] if stock and stock["mark"] > 0 else None
            if spot is None or spot <= 0:
                spot = ticker_opts[0]["strike"] if ticker_opts else 0.0
            if spot <= 0:
                continue

            # Compute P/L per leg
            stock_pl = 0.0
            stock_cost = 0.0
            if stock:
                stock_pl = (spot - stock["avg_price"]) * stock["qty"]
                stock_cost = stock["avg_price"] * abs(stock["qty"])

            options_pl = 0.0
            options_cost = 0.0
            leg_parts = []

            for o in sorted(ticker_opts, key=lambda x: (x["strike"], x["right"])):
                contracts = abs(o["qty"])
                if o["right"] == "C":
                    intrinsic = max(spot - o["strike"], 0)
                else:
                    intrinsic = max(o["strike"] - spot, 0)

                if o["qty"] > 0:
                    leg_pl = (intrinsic - o["avg_price"]) * contracts * 100
                else:
                    leg_pl = (o["avg_price"] - intrinsic) * contracts * 100

                leg_pl -= COMMISSION_PER_CONTRACT * contracts
                options_pl += leg_pl
                options_cost += o["avg_price"] * contracts * 100

                d = "S" if o["qty"] < 0 else "L"
                leg_parts.append(f"{d}{contracts:.0f}{o['right']}${o['strike']:g}")

            total_ticker_pl = stock_pl + options_pl
            # Capital freed = stock value returned at expiry (for covered positions)
            capital_freed = stock_cost if stock and stock["qty"] > 0 else options_cost

            cost_basis = stock_cost if stock_cost > 0 else options_cost
            if cost_basis > 0 and dte > 0:
                apy = (total_ticker_pl / cost_basis) * (365.0 / dte) * 100.0
            else:
                apy = 0.0

            exp_pl += total_ticker_pl
            exp_capital += capital_freed

            pl_sign = "+" if total_ticker_pl >= 0 else ""
            legs_str = " ".join(leg_parts)
            qty_str = f"{abs(stock['qty']):.0f}sh+" if stock else ""
            ticker_lines.append(
                f"  {ticker} {qty_str}{legs_str}"
                f" → ${pl_sign}{total_ticker_pl:.0f} ({apy:+.0f}%)")

        grand_total_pl += exp_pl
        grand_total_capital += exp_capital

        # Expiration header
        day_name = exp.strftime("%a")
        exp_str = exp.strftime("%b %d")
        pl_emoji = "✅" if exp_pl >= 0 else "🔴"
        lines.append(
            f"\n*{day_name} {exp_str}* ({dte}d)"
            f" — {pl_emoji} ${exp_pl:+,.0f}"
            f" · 💰 ${exp_capital:,.0f} freed")
        lines.extend(ticker_lines)

    lines.append(f"\n{'─' * 32}")
    t_emoji = "✅" if grand_total_pl >= 0 else "🔴"
    lines.append(
        f"{t_emoji} *Total: ${grand_total_pl:+,.0f} profit*"
        f" · *${grand_total_capital:,.0f} capital freed*")

    return "\n".join(lines)
