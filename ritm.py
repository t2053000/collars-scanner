"""
ritm.py - Reverse ITM conversion scanner.
Setup: SHORT 100 stock + BUY 1 call + SELL 1 put (strike > spot).
Borrow assumed at 25% APR. Uses injected schwab client from main.py.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DTE_MIN = 1
DTE_MAX = 14
BORROW_RATE_PCT = 25.0
COMMISSION_PER_CONTRACT = 1.30
MIN_OI = 50
MAX_SPREAD_PCT = 0.40
MIN_LOCKED_AFTER_COMM_PER_CONTRACT = 5.0
FALLBACK_STEP_FRAC = 0.15
MAX_HITS = 50

_schwab_client = None


def _load_tickers_safely():
    try:
        import github_store
    except ImportError:
        logger.warning("github_store not importable")
        return []
    for name in ["load_tickers", "read_tickers", "get_tickers", "list_tickers",
                 "load_file", "read_file", "fetch_tickers"]:
        fn = getattr(github_store, name, None)
        if callable(fn):
            try:
                result = fn("tickers.txt")
                if result:
                    logger.info(f"loaded tickers via github_store.{name}, count={len(result)}")
                    return result
            except TypeError:
                try:
                    result = fn()
                    if result:
                        logger.info(f"loaded tickers via github_store.{name}() no-arg, count={len(result)}")
                        return result
                except Exception as e:
                    logger.warning(f"github_store.{name}() no-arg failed: {e}")
            except Exception as e:
                logger.warning(f"github_store.{name} failed: {e}")
                continue
    logger.warning("no working ticker-loader found in github_store")
    return []


def _get_client():
    global _schwab_client
    if _schwab_client is not None:
        return _schwab_client
    try:
        from schwab_client import SchwabClient
        candidate = SchwabClient()
        if candidate is not None:
            _schwab_client = candidate
            return candidate
    except Exception as e:
        logger.warning(f"fallback SchwabClient() construction failed: {e}")
    return None


def _mid(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def _spread_pct(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return 1.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 1.0
    return (ask - bid) / mid


def _try_get_quote(client, ticker):
    for name in ["get_quote", "quote", "get_price", "fetch_quote"]:
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                return fn(ticker)
            except Exception:
                continue
    return None


def _try_get_chain(client, ticker):
    for name in ["get_option_chain", "option_chain", "chain", "get_chain", "fetch_option_chain"]:
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                return fn(ticker)
            except Exception:
                continue
    return None


def _extract_spot(quote):
    """quote may be a float (price) or dict (full quote object). Return spot price."""
    if quote is None:
        return None
    if isinstance(quote, (int, float)):
        return float(quote)
    if isinstance(quote, dict):
        return (quote.get("last") or quote.get("mark") or
                quote.get("lastPrice") or quote.get("price"))
    return None


def scan_ritm(tickers=None, schwab_client=None):
    client = schwab_client or _get_client()
    if client is None:
        logger.error("scan_ritm: no schwab client available")
        return []

    if tickers is None:
        tickers = _load_tickers_safely()

    hits = []
    today = datetime.now().date()

    for ticker in tickers:
        ticker = ticker.strip().upper()
        if not ticker:
            continue
        try:
            quote = _try_get_quote(client, ticker)
            spot = _extract_spot(quote)
            if not spot or spot <= 0:
                continue

            chain = _try_get_chain(client, ticker)
            if not chain or not isinstance(chain, dict):
                continue

            call_map = chain.get("callExpDateMap", {})
            put_map = chain.get("putExpDateMap", {})

            for exp_key in call_map:
                if exp_key not in put_map:
                    continue
                exp_date_str = exp_key.split(":")[0]
                exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte < DTE_MIN or dte > DTE_MAX:
                    continue

                for strike_str in call_map[exp_key]:
                    if strike_str not in put_map[exp_key]:
                        continue
                    strike = float(strike_str)
                    if strike <= spot:
                        continue

                    call_data = call_map[exp_key][strike_str][0]
                    put_data = put_map[exp_key][strike_str][0]
                    call_bid = call_data.get("bid")
                    call_ask = call_data.get("ask")
                    put_bid = put_data.get("bid")
                    put_ask = put_data.get("ask")

                    call_oi = call_data.get("openInterest", 0) or 0
                    put_oi = put_data.get("openInterest", 0) or 0
                    if call_oi < MIN_OI or put_oi < MIN_OI:
                        continue
                    if _spread_pct(call_bid, call_ask) > MAX_SPREAD_PCT:
                        continue
                    if _spread_pct(put_bid, put_ask) > MAX_SPREAD_PCT:
                        continue

                    call_mid = _mid(call_bid, call_ask)
                    put_mid = _mid(put_bid, put_ask)
                    if call_mid is None or put_mid is None:
                        continue

                    net_premium = put_mid - call_mid
                    gap = strike - spot
                    borrow = spot * (BORROW_RATE_PCT / 100.0) * (dte / 365.0)
                    locked_per_share = net_premium - gap - borrow
                    commission_per_share = COMMISSION_PER_CONTRACT / 100.0
                    locked_after_comm = locked_per_share - commission_per_share
                    locked_total = locked_after_comm * 100.0

                    if locked_total < MIN_LOCKED_AFTER_COMM_PER_CONTRACT:
                        continue

                    cost_basis = spot
                    if cost_basis <= 0 or dte <= 0:
                        continue
                    apy = (locked_after_comm / cost_basis) * (365.0 / dte) * 100.0

                    call_fallback = call_mid + FALLBACK_STEP_FRAC * (call_ask - call_bid)
                    put_fallback = put_mid - FALLBACK_STEP_FRAC * (put_ask - put_bid)
                    fb_net = put_fallback - call_fallback
                    fb_locked = fb_net - gap - borrow - commission_per_share
                    fb_apy = (fb_locked / cost_basis) * (365.0 / dte) * 100.0 if fb_locked > 0 else 0.0

                    hits.append({
                        "ticker": ticker,
                        "spot": round(spot, 2),
                        "strike": strike,
                        "exp_date": exp_date_str,
                        "dte": dte,
                        "call_bid": call_bid,
                        "call_ask": call_ask,
                        "put_bid": put_bid,
                        "put_ask": put_ask,
                        "call_cost": round(call_mid, 2),
                        "put_credit": round(put_mid, 2),
                        "net_premium_credit": round(net_premium, 2),
                        "gap": round(gap, 2),
                        "borrow_cost_per_share": round(borrow, 4),
                        "locked_per_share": round(locked_per_share, 2),
                        "locked_total": round(locked_total, 2),
                        "locked_apy": round(apy, 1),
                        "fallback_locked": round(fb_locked * 100, 2),
                        "fallback_apy": round(fb_apy, 1),
                        "call_oi": call_oi,
                        "put_oi": put_oi,
                    })

        except Exception as e:
            logger.warning(f"/ritm scan error on {ticker}: {e}")
            continue

    hits.sort(key=lambda h: h["locked_apy"])
    logger.info(f"/ritm scan complete: {len(hits)} hits across {len(tickers)} tickers")
    return hits[-MAX_HITS:]


def format_ritm_hit(hit, idx, total):
    return (
        f"*{idx}/{total}  {hit['ticker']}*  spot ${hit['spot']}\n"
        f"  strike ${hit['strike']:g}  exp {hit['exp_date']} ({hit['dte']}d)\n"
        f"  call mid ${hit['call_cost']}  put mid ${hit['put_credit']}\n"
        f"  net credit ${hit['net_premium_credit']}  gap ${hit['gap']}\n"
        f"  borrow @25%: -${hit['borrow_cost_per_share']:.2f}/sh\n"
        f"  *locked ${hit['locked_total']:.2f}* @ *{hit['locked_apy']:.1f}% APY*\n"
        f"  fallback: ${hit['fallback_locked']:.2f} @ {hit['fallback_apy']:.1f}% APY\n"
    )


class RitmScanner:
    def __init__(self, schwab_client=None, div_freqs=None, *args, **kwargs):
        global _schwab_client
        self.schwab = schwab_client
        self.div_freqs = div_freqs or {}
        if schwab_client is not None:
            _schwab_client = schwab_client
            logger.info("RitmScanner init: cached schwab client globally for scan_ritm()")
        logger.info(f"RitmScanner init: schwab={'set' if schwab_client else 'none'}, "
                    f"div_freqs_count={len(self.div_freqs) if hasattr(self.div_freqs, '__len__') else 0}")

    def run(self, *args, **kwargs):
        return scan_ritm(schwab_client=self.schwab)

    def scan(self, *args, **kwargs):
        return scan_ritm(schwab_client=self.schwab)

    def execute(self, *args, **kwargs):
        return scan_ritm(schwab_client=self.schwab)

    @staticmethod
    def format(hits):
        if not hits:
            return "No /ritm hits."
        lines = [f"📊 /ritm found {len(hits)} hits (borrow 25%):\n"]
        for i, h in enumerate(hits, 1):
            lines.append(format_ritm_hit(h, i, len(hits)))
        return "\n".join(lines)