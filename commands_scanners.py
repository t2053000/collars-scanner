"""
bot/commands_scanners.py

The commands that kick off a scan: /scan, /spreads, /deepcall, /dca,
/csp, /itm, /ritm, /itmib. Each just resolves a ticker list and hands
off to `_run_scan` with the right scanner + formatter.
"""
from telegram.constants import ParseMode

import github_store
from scanner  import CollarScanner
from spreads  import SpreadScanner
from deepcall import DeepCallScanner, clamp_cushion, DEFAULT_CUSHION_PCT
from dca      import DcaScanner
from csp      import CspScanner
from itm      import ItmScanner
from ritm     import RitmScanner
from itm_ibkr import ItmIbkrScanner

from .helpers import authorized_only
from .scan_runner import _run_scan


@authorized_only
async def cmd_scan(update, context):
    scanner = context.application.bot_data["collar_scanner"]
    await _run_scan(update, context, scanner, "🔎", CollarScanner.format_summary)


@authorized_only
async def cmd_spreads(update, context):
    scanner = context.application.bot_data["spread_scanner"]
    if context.args:
        sym = context.args[0].upper().strip()
        if sym.isalpha() and 1 <= len(sym) <= 6:
            await _run_scan(update, context, scanner, "💸",
                            SpreadScanner.format_summary, tickers_override=[sym])
            return
    await _run_scan(update, context, scanner, "💸", SpreadScanner.format_summary)


@authorized_only
async def cmd_deepcall(update, context):
    scanner = context.application.bot_data["deepcall_scanner"]
    cushion_pct = DEFAULT_CUSHION_PCT
    if context.args:
        try:
            requested = float(context.args[0])
            clamped, was_clamped = clamp_cushion(requested)
            cushion_pct = clamped
            if was_clamped:
                await update.message.reply_text(f"⚠️ Using *{clamped:g}%* cushion", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Usage: `/deepcall [N]`", parse_mode=ParseMode.MARKDOWN)
            return
    await _run_scan(update, context, scanner, "🛡️", DeepCallScanner.format_summary,
                    scan_kwargs={"cushion_pct": cushion_pct},
                    summary_kwargs={"cushion_pct": cushion_pct})


@authorized_only
async def cmd_dca(update, context):
    scanner = context.application.bot_data["dca_scanner"]
    div_tickers = github_store.get_div_tickers()
    if not div_tickers:
        await update.message.reply_text("_Empty._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    tickers = sorted(div_tickers.keys())
    await _run_scan(update, context, scanner, "💰", DcaScanner.format_summary,
                    tickers_override=tickers, hits_with_buttons=True, scanner_key="dca")


@authorized_only
async def cmd_csp(update, context):
    scanner = context.application.bot_data["csp_scanner"]
    div_tickers = github_store.get_div_tickers()
    if not div_tickers:
        await update.message.reply_text("_Empty._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    tickers = sorted(div_tickers.keys())
    await _run_scan(update, context, scanner, "💵", CspScanner.format_summary,
                    tickers_override=tickers)


@authorized_only
async def cmd_itm(update, context):
    scanner     = context.application.bot_data["itm_scanner"]
    div_tickers = github_store.get_div_tickers()

    args         = [a.lower() for a in (context.args or [])]
    reverse_mode = "r" in args
    hiv_tickers = github_store.get_latest_hiv_tickers()
    tickers     = hiv_tickers if hiv_tickers else github_store.get_tickers()

    combined = sorted(set(tickers))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers

    if reverse_mode:
        original_scan_ticker = scanner.scan_ticker
        scanner.scan_ticker  = scanner.scan_ticker_reverse
        await _run_scan(update, context, scanner, "🔄", ItmScanner.format_summary,
                        tickers_override=combined, hits_with_buttons=True, scanner_key="itm_r")
        scanner.scan_ticker = original_scan_ticker
    else:
        await _run_scan(update, context, scanner, "🔒", ItmScanner.format_summary,
                        tickers_override=combined, hits_with_buttons=True, scanner_key="itm")


@authorized_only
async def cmd_ritm(update, context):
    scanner     = context.application.bot_data["ritm_scanner"]
    div_tickers = github_store.get_div_tickers()

    hiv_tickers = github_store.get_latest_hiv_tickers()
    tickers     = hiv_tickers if hiv_tickers else github_store.get_tickers()

    combined = sorted(set(tickers))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    await _run_scan(update, context, scanner, "🔄", RitmScanner.format_summary,
                    tickers_override=combined)


@authorized_only
async def cmd_itmib(update, context):
    """Reverse ITM scan using IBKR market data. Execution still via Schwab."""
    scanner = context.application.bot_data.get("itm_ibkr_scanner")
    if scanner is None:
        await update.message.reply_text("IBKR scanner unavailable — check VPS/Tailscale connection.")
        return
    div_tickers = github_store.get_div_tickers()
    tickers     = github_store.get_tickers()

    combined = sorted(set(tickers))
    if not combined:
        await update.message.reply_text("_No tickers._", parse_mode=ParseMode.MARKDOWN)
        return
    scanner.ticker_freqs = div_tickers
    original = scanner.scan_ticker
    scanner.scan_ticker = scanner.scan_ticker_reverse
    await _run_scan(update, context, scanner, "🔄", ItmIbkrScanner.format_summary,
                    tickers_override=combined, hits_with_buttons=True, scanner_key="itm_r")
    scanner.scan_ticker = original
