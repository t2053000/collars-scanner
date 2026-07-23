"""
bot/scan_runner.py

Generic scan engine shared by every scanner command (/scan, /spreads,
/deepcall, /dca, /csp, /itm, /ritm, /itmib). Runs `scanner.scan_ticker`
concurrently across a ticker list, formats the summary, and — for
scanners that support it — fires off trade-confirmation buttons for
the best hits.
"""
import asyncio
import logging
from collections import Counter

from telegram.constants import ParseMode

import github_store

from .helpers import _send_robust, _edit_robust, _maybe_save_token
from .state import _LAST_ERRORS, SCAN_CONCURRENCY, TICKER_BLACKLIST, MAX_TRADE_BUTTONS
from .trade_cards import _send_itm_trade_button, _send_rtrade_button, _send_dca_trade_button

logger = logging.getLogger(__name__)


async def _run_scan(update, context, scanner, label_emoji, format_summary_fn,
                    tickers_override=None, scan_kwargs=None, summary_kwargs=None,
                    hits_with_buttons=False, scanner_key=None):
    tickers = tickers_override if tickers_override is not None else github_store.get_tickers()
    tickers = [t for t in tickers if t not in TICKER_BLACKLIST]
    if not tickers:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return

    status_msg = await update.message.reply_text(
        f"{label_emoji} Scanning {len(tickers)} tickers…",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_running_loop()
    all_hits = []
    errors = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    successful = 0
    debug_totals = Counter()
    scan_kwargs = scan_kwargs or {}

    async def scan_one(tk):
        nonlocal successful
        async with sem:
            try:
                result = await loop.run_in_executor(
                    None, lambda: scanner.scan_ticker(tk, **scan_kwargs)
                )
                if isinstance(result, tuple):
                    hits, debug = result
                    debug_totals.update(debug)
                else:
                    hits = result
                all_hits.extend(hits)
                ok = True
            except Exception as e:
                logger.exception(f"scan error for {tk}")
                err_type = type(e).__name__
                short = str(e)[:120].replace("\n", " ")
                full = str(e)[:400].replace("\n", " ")
                errors.append(f"{tk}: {err_type} – {short}")
                _LAST_ERRORS.append(f"{tk}: {err_type} – {full}")
                ok = False
        if ok:
            successful += 1

    await asyncio.gather(*(scan_one(t) for t in tickers))

    kwargs = dict(all_hits=all_hits, scanned=len(tickers),
                  successful=successful, errors=errors)
    if summary_kwargs:
        kwargs.update(summary_kwargs)
    if debug_totals:
        kwargs["debug_totals"] = dict(debug_totals)
    try:
        messages = format_summary_fn(**kwargs)
    except TypeError:
        kwargs.pop("debug_totals", None)
        try:
            messages = format_summary_fn(**kwargs)
        except TypeError:
            for k in list(summary_kwargs or {}):
                kwargs.pop(k, None)
            messages = format_summary_fn(**kwargs)

    await _edit_robust(status_msg, messages[0])
    for extra in messages[1:]:
        await _send_robust(update.message.reply_text, extra)

    primary_uid = context.application.bot_data.get("primary_user_id", 0)
    _maybe_save_token(primary_uid)

    if hits_with_buttons and all_hits:
        if scanner_key == "itm":
            all_hits.sort(key=lambda r: r.get("locked_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_itm_trade_button(update, context, hit)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"trade button send failed for {hit.get('ticker')}: {e}")

        elif scanner_key == "itm_r":
            all_hits.sort(key=lambda r: r.get("locked_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_rtrade_button(update, context, hit)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"rtrade button send failed for {hit.get('ticker')}: {e}")

        elif scanner_key == "dca":
            all_hits.sort(key=lambda r: r.get("score_apy", 0))
            top_hits = all_hits[-MAX_TRADE_BUTTONS:]
            for hit in top_hits:
                try:
                    await _send_dca_trade_button(update, context, hit)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.warning(f"dca button send failed for {hit.get('ticker')}: {e}")
