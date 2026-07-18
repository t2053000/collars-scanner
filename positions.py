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


def compute_positions(raw_positions: list, fills: list = None) -> str:
    """
    Given raw Schwab positions list, compute P/L grouped by expiration
    then by ticker. Uses fills.json for entry cost (accurate) instead of
    Schwab's avg_price (inflated by wash sale adjustments).
    """
    today = date.today()
    fills = fills or []

    # Build fills lookup: (ticker, strike, exp) -> total entry cost
    fills_cost = {}
    for f in fills:
        key = (f.get("ticker"), f.get("strike"), f.get("exp"))
        fills_cost[key] = fills_cost.get(key, 0) + f.get("cost", 0)

    # Separate stock and option positions
    stocks  = {}   # ticker -> {qty, mkt_value, mark}
    options = []   # list of option position dicts

    for pos in raw_positions:
        instrument = pos.get("instrument", {})
        asset_type = instrument.get("assetType", "")
        symbol     = instrument.get("symbol", "")
        long_qty   = float(pos.get("longQuantity",  0))
        short_qty  = float(pos.get("shortQuantity", 0))
        qty        = long_qty - short_qty
        mkt_value  = float(pos.get("marketValue",0))         
        if asset_type in ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF"):

            mark = mkt_value / qty if qty != 0 else 0.0
            stocks[symbol] = {
                "qty":       qty,
                "mkt_value": mkt_value,
                "mark":      mark,
            }

        elif asset_type == "OPTION":
            parsed = _parse_option_symbol(symbol)
            if parsed is None:
                continue
            contracts = abs(qty)
            avg_price = abs(float(pos.get("averagePrice", 0)))
            options.append({
                "ticker":    parsed["ticker"],
                "expiry":    parsed["expiry"],
                "right":     parsed["right"],
                "strike":    parsed["strike"],
                "qty":       qty,
                "avg_price": avg_price,
                "mkt_value": mkt_value,
            })

    future_options = [o for o in options if o["expiry"] >= today]
    if not future_options:
        return "📭 No open option positions."

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

            spot = stock["mark"] if stock and stock["mark"] > 0 else None
            if spot is None or spot <= 0:
                spot = ticker_opts[0]["strike"] if ticker_opts else 0.0
            if spot <= 0:
                continue

            # ── Compute net option credit received (per share) ──
            net_credit_per_share = 0.0
            shares_covered = 0
            leg_parts = []

            for o in sorted(ticker_opts, key=lambda x: (x["strike"], x["right"])):
                contracts = abs(o["qty"])
                d = "S" if o["qty"] < 0 else "L"
                leg_parts.append(f"{d}{contracts:.0f}{o['right']}${o['strike']:g}")

                if o["qty"] < 0:  # sold option — received premium
                    net_credit_per_share += o["avg_price"]
                else:             # bought option — paid premium
                    net_credit_per_share -= o["avg_price"]

                # Track how many shares are covered by calls
                if o["right"] == "C" and o["qty"] < 0:
                    shares_covered += contracts * 100

            # ── Projected P/L at expiry (spot stays flat) ──
            if stock and stock["qty"] > 0 and shares_covered > 0:
                call_strikes = [o["strike"] for o in ticker_opts
                                if o["right"] == "C" and o["qty"] < 0]
                primary_strike = call_strikes[0] if call_strikes else spot
                shares = min(abs(stock["qty"]), shares_covered)
                exp_str_lookup = exp.strftime("%Y-%m-%d")

                # Try fills.json for accurate entry cost
                fills_key = (ticker, primary_strike, exp_str_lookup)
                entry_cost = fills_cost.get(fills_key)

                if entry_cost and entry_cost > 0:
                    # Profit = what we get at expiry - what we paid + net credit
                    exit_value = primary_strike * shares
                    net_credit_total = net_credit_per_share * shares
                    total_pl = exit_value + net_credit_total - entry_cost
                else:
                    # Fallback: use current spot for gap (less accurate)
                    gap = spot - primary_strike
                    pl_per_share = net_credit_per_share - gap
                    total_pl = pl_per_share * shares

                total_pl -= COMMISSION_PER_CONTRACT * len(ticker_opts)
                capital_freed = primary_strike * shares
            else:
                # Options-only position: use mark-to-market
                total_pl = sum(o["mkt_value"] for o in ticker_opts)
                total_pl -= COMMISSION_PER_CONTRACT * len(ticker_opts)
                capital_freed = abs(sum(o["mkt_value"] for o in ticker_opts))

            cost_basis = spot * min(abs(stock["qty"]), shares_covered) if stock else abs(sum(o["mkt_value"] for o in ticker_opts))
            if cost_basis > 0 and dte > 0:
                apy = (total_pl / cost_basis) * (365.0 / dte) * 100.0
            else:
                apy = 0.0

            exp_pl += total_pl
            exp_capital += capital_freed

            pl_sign = "+" if total_pl >= 0 else ""
            legs_str = " ".join(leg_parts)
            qty_str = f"{abs(stock['qty']):.0f}sh+" if stock else ""
            ticker_lines.append(
                f"  {ticker} {qty_str}{legs_str}"
                f" → ${pl_sign}{total_pl:.0f} ({apy:+.0f}%)")

        grand_total_pl += exp_pl
        grand_total_capital += exp_capital

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
