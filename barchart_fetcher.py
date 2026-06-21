"""
barchart_fetcher.py
Fetches high-IV options screener data from Barchart Premier.
Two-step: 1) GET page to extract CSRF token, 2) POST download with cookie.
Requires BARCHART_COOKIE env var.
Runs every 20 min during market hours (9:30am-4pm ET, weekdays only).
Saves to GitHub: tickers/bc_YYYYMMDD_HHMM.csv
"""

import os
import re
import csv
import io
import logging
from datetime import datetime, time
import pytz
import requests
import github_store

logger = logging.getLogger(__name__)

SCREENER_URL = "https://www.barchart.com/options/options-screener"
DOWNLOAD_URL = "https://www.barchart.com/my/download"

POST_PARAMS = {
    "customFieldTitles": '{"percentChange":"Option %Chg","priceChange":"Option Chg","earningsDate":"Next Earnings","dividend":"Next Dividend","dividendExDate":"Next Ex-Div Date"}',
    "fileName": "Options Screener-High IV small caps",
    "method": "/options/get",
    "page": "1",
    "customGetParameters": (
        "between(volatility,50,)"
        "&in(baseSymbolType,(1,7))"
        "&between(baseLastPrice,1,50)"
        "&between(daysToExpiration,1,45)"
        "&in(expirationType,(monthly,weekly))"
        "&in(symbolType,(call))"
        "&between(moneyness,-25,25)"
        "&between(lastPrice,0.1,)"
        "&between(volume,100,)"
        "&between(openInterest,50,)"
        "&ge(tradeTime,previousTradingDay)"
        "&in(exchange,(AMEX,NYSE,NASDAQ))"
        "&ne(isAdjusted,1)"
    ),
    "orderBy": "volatility",
    "orderDir": "desc",
    "fields": "baseSymbol,volatility,baseLastPrice,expirationDate,daysToExpiration,expirationType,symbolType,strikePrice,moneyness,bidPrice,askPrice,lastPrice,volume,weightedImpliedVolatility,openInterest,vega,delta,tradeTime,exchange",
    "hasOptions": "true",
    "limit": "5000",
    "customView": "true",
    "exclude": "raw,symbolCode,updatedFields,lastPriceDirection,customLinks,hasOptions,expirationType,baseSymbolType,legs,exchange",
    "pageTitle": " Options Screener ",
}

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 OPR/129.0.0.0",
    "Origin": "https://www.barchart.com",
    "Referer": "https://www.barchart.com/options/options-screener",
}


def _is_market_hours() -> bool:
    """Returns True if current time is within market hours (9:30am-4pm ET, Mon-Fri)."""
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return time(9, 30) <= t <= time(16, 0)


def fetch_barchart_hiv_tickers(cookie: str = None) -> list[dict]:
    """
    Fetch high-IV tickers from Barchart screener.
    Returns list of dicts: ticker, price, iv, expiry, oi
    """
    cookie = cookie or os.environ.get("BARCHART_COOKIE", "")
    if not cookie:
        raise ValueError("BARCHART_COOKIE env var not set")

    session = requests.Session()
    session.headers.update(HEADERS_BASE)
    session.headers["Cookie"] = cookie

    # Step 1: GET page to extract CSRF token
    resp = session.get(SCREENER_URL, timeout=15)
    resp.raise_for_status()

    token_match = (
        re.search(r'meta-csrf-token["\s]+content="([^"]+)"', resp.text) or
        re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text) or
        re.search(r'"_token"\s*value="([^"]+)"', resp.text)
    )
    if not token_match:
        raise ValueError("Could not extract CSRF token from Barchart page")

    csrf_token = token_match.group(1)
    logger.info(f"barchart_fetcher: got CSRF token")

    # Step 2: POST download
    post_data = {**POST_PARAMS, "_token": csrf_token}
    resp2 = session.post(
        DOWNLOAD_URL,
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=True,
    )
    resp2.raise_for_status()

    content_type = resp2.headers.get("Content-Type", "")
    if "text/csv" not in content_type and not resp2.text.strip().startswith('"'):
        raise ValueError(f"Unexpected response — not CSV. Content-Type: {content_type}")

    # Parse CSV
    reader  = csv.DictReader(io.StringIO(resp2.text))
    rows    = list(reader)
    logger.info(f"barchart_fetcher: parsed {len(rows)} rows")

    # Deduplicate by ticker, filter price < 50
    seen    = set()
    results = []
    for row in rows:
        ticker = row.get("Symbol", "").strip().upper()
        if not ticker or ticker in seen:
            continue
        try:
            price = float(row.get("Price~", 0) or 0)
        except ValueError:
            price = 0
        if price <= 0 or price > 50:
            continue
        seen.add(ticker)
        results.append({
            "ticker": ticker,
            "price":  price,
            "iv":     row.get("IV", ""),
            "expiry": row.get("Exp Date", ""),
            "oi":     row.get("Open Int", ""),
        })

    logger.info(f"barchart_fetcher: {len(results)} unique tickers (price < $50)")
    return results


def run_barchart_fetch_job():
    """
    APScheduler job — runs every 20 min.
    Skips if outside market hours.
    Saves to GitHub: tickers/bc_YYYYMMDD_HHMM.csv
    """
    if not _is_market_hours():
        logger.debug("barchart_fetcher: outside market hours — skipping")
        return

    logger.info("barchart_fetcher: starting fetch...")
    try:
        rows = fetch_barchart_hiv_tickers()
        if not rows:
            logger.warning("barchart_fetcher: no tickers fetched — skipping save")
            return

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
        gh_path   = f"tickers/bc_{timestamp}.csv"

        lines = ["ticker,price,iv,expiry,oi"]
        for r in rows:
            lines.append(f"{r['ticker']},{r['price']},{r['iv']},{r['expiry']},{r['oi']}")
        csv_str = "\n".join(lines) + "\n"

        github_store.save_file(gh_path, csv_str, f"barchart tickers {timestamp}")
        logger.info(f"barchart_fetcher: saved {len(rows)} tickers to {gh_path}")

    except Exception as e:
        logger.error(f"barchart_fetcher: job failed: {e}")
