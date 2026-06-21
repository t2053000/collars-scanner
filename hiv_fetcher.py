"""
hiv_fetcher.py
Fetches high-IV optionable stocks from Finviz screener.
Filters: optionable, IV > 75%, price < $50, options vol > 500,
         market cap > $300M (small+), avg volume > 500K.
Sorted by IV descending. Fetches up to 250 tickers.

Called by APScheduler in main.py every 15 minutes.
Saves timestamped CSV to GitHub: tickers/tickers_YYYYMMDD_HHMM.csv
"""

import logging
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import github_store

logger = logging.getLogger(__name__)

BASE_URL = (
    "https://finviz.com/screener.ashx"
    "?v=111"
    "&f=op_optionable,op_option_implied_volatility_o75,sh_price_u50,"
    "optoptvolume_o500,cap_smallover,sh_avgvol_o500"
    "&o=-op_option_implied_volatility"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://finviz.com/",
}

TARGET       = 250
ROWS_PER_PAGE = 20


def _fetch_page(row_start: int) -> list[dict]:
    url  = BASE_URL + f"&r={row_start}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the data table — first col is a row number digit
    target_table = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) > 5:
            first_cols = [td.get_text(strip=True) for td in rows[1].find_all("td")]
            if first_cols and first_cols[0].isdigit():
                target_table = table
                break

    if target_table is None:
        logger.warning(f"hiv_fetcher: no data table found at row {row_start}")
        return []

    results = []
    for row in target_table.find_all("tr")[1:]:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 11 or not cols[0].isdigit():
            continue
        ticker = cols[1].strip()
        if not ticker:
            continue
        results.append({
            "ticker": ticker,
            "price":  cols[8],
            "change": cols[9],
            "volume": cols[10],
        })

    return results


def fetch_hiv_tickers() -> list[dict]:
    """Fetch up to TARGET tickers from Finviz. Returns list of dicts."""
    all_results = []
    page        = 1
    row_start   = 1

    while len(all_results) < TARGET:
        try:
            rows = _fetch_page(row_start)
        except Exception as e:
            logger.error(f"hiv_fetcher: page {page} failed: {e}")
            break

        if not rows:
            break

        all_results.extend(rows)
        logger.debug(f"hiv_fetcher: page {page} → {len(rows)} rows (total {len(all_results)})")

        if len(rows) < ROWS_PER_PAGE:
            break

        row_start += ROWS_PER_PAGE
        page      += 1
        time.sleep(1.5)

    # deduplicate by ticker
    seen = set()
    deduped = []
    for r in all_results:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            deduped.append(r)

    return deduped[:TARGET]


def run_hiv_fetch_job():
    """
    Job entry point called by APScheduler.
    Fetches tickers and saves timestamped CSV to GitHub.
    """
    logger.info("hiv_fetcher: starting scheduled fetch...")
    try:
        rows = fetch_hiv_tickers()
        if not rows:
            logger.warning("hiv_fetcher: no tickers fetched — skipping save")
            return

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
        gh_path   = f"tickers/tickers_{timestamp}.csv"

        # Build CSV manually — no pandas needed
        lines = ["ticker,price,change,volume"]
        for r in rows:
            lines.append(f"{r['ticker']},{r['price']},{r['change']},{r['volume']}")
        csv_str = "\n".join(lines) + "\n"

        github_store.save_file(gh_path, csv_str, f"hiv tickers {timestamp}")
        logger.info(f"hiv_fetcher: saved {len(rows)} tickers to {gh_path}")

    except Exception as e:
        logger.error(f"hiv_fetcher: job failed: {e}")
