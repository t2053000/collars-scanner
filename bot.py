"""
bot.py
Telegram bot — scanners + ITM/DCA trade execution with improve/cancel flow.
Heavy logging on trade flow for debugging.
"""

import asyncio
import logging
import time
import uuid
from collections import Counter, deque
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
   Application, CommandHandler, CallbackQueryHandler,
   MessageHandler, ContextTypes, filters,
)

import github_store
from scanner  import CollarScanner
from spreads  import SpreadScanner
from deepcall import DeepCallScanner, clamp_cushion, DEFAULT_CUSHION_PCT, MIN_CUSHION_PCT, MAX_CUSHION_PCT
from dca      import DcaScanner
from csp      import CspScanner
from itm      import ItmScanner
from ritm     import RitmScanner
import orders

logger = logging.getLogger(__name__)

SCAN_CONCURRENCY = 5
TG_MAX_LEN = 4000
_LAST_ERRORS: deque = deque(maxlen=30)

_PENDING_TRADES: dict = {}
PENDING_TIMEOUT_SEC = 60
_ACTIVE_ORDERS: dict = {}

MAX_TRADE_BUTTONS = 20


def _get_schwab_for_user(context, user_id: int):
   clients     = context.application.bot_data["schwab_clients"]
   primary_uid = context.application.bot_data["primary_user_id"]
   client      = clients.get(user_id) or clients.get(primary_uid)
   if client is None:
       raise RuntimeError(
           f"No Schwab client available for user {user_id} "
           f"and no primary fallback."
       )
   logger.info(
       f"_get_schwab_for_user: user={user_id} "
       f"using={'own' if user_id in clients else 'primary'} client"
   )
   return client


def authorized_only(func):
   @wraps(func)
   async def wrapper(update, context):
       user = update.effective_user
       if not user or not github_store.is_authorized(user.id):
           await update.message.reply_text(
               f"❌ Not authorized. Telegram ID: `{user.id if user else '?'}`",
               parse_mode=ParseMode.MARKDOWN,
           )
           return
       return await func(update, context)
   return wrapper


def authorized_callback(func):
   @wraps(func)
   async def wrapper(update, context):
       user = update.effective_user
       if not user or not github_store.is_authorized(user.id):
           await update.callback_query.answer("Not authorized.")
           return
       return await func(update, context)
   return wrapper


def _truncate(text, limit=TG_MAX_LEN):
   if len(text) <= limit:
       return text
   return text[:limit - 30] + "\n\n_(truncated…)_"


async def _send_robust(send_callable, text, reply_markup=None):
   safe = _truncate(text)
   try:
       if reply_markup:
           await send_callable(safe, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
       else:
           await send_callable(safe, parse_mode=ParseMode.MARKDOWN)
       return
   except BadRequest as e:
       logger.warning(f"BadRequest with markdown: {e}")
   try:
       plain = safe.replace("*", "").replace("_", "").replace("`", "")
       plain = _truncate(plain)
       if reply_markup:
           await send_callable(plain, reply_markup=reply_markup)
       else:
           await send_callable(plain)
   except BadRequest as e:
       logger.error(f"BadRequest plain text: {e}")


async def _edit_robust(message, text, reply_markup=None):
   safe = _truncate(text)
   try:
       if reply_markup is not None:
           await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
       else:
           await message.edit_text(safe, parse_mode=ParseMode.MARKDOWN)
       return
   except BadRequest as e:
       logger.warning(f"Edit BadRequest with markdown: {e}")
   try:
       plain = safe.replace("*", "").replace("_", "").replace("`", "")
       plain = _truncate(plain)
       if reply_markup is not None:
           await message.edit_text(plain, reply_markup=reply_markup)
       else:
           await message.edit_text(plain)
   except BadRequest as e:
       logger.error(f"Edit BadRequest plain text: {e}")


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

async def cmd_start(update, context):
   await update.message.reply_text(
       "👋 *Options Scanner Bot*\n\n"
       "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm`\n"
       "Send `/help` for all commands.",
       parse_mode=ParseMode.MARKDOWN,
   )


async def cmd_help(update, context):
   await update.message.reply_text(
       "*Commands*\n"
       "`/scan` `/spreads` `/deepcall` `/dca` `/csp` `/itm` `/ritm`\n"
       "`/list` `/add` `/remove` `/logs` `/whoami`\n"
       "`/refresh_token` `/submit_token`\n\n"
       "*Trading:*\n"
       "· `/itm` — tap ✅ Confirm to place order instantly\n"
       "· `/itm r` — tap ✅ Confirm to place options order instantly\n"
       "· `/dca` hits → reply `YES TICKER`\n"
       "· Short stock manually on Schwab for /itm r BEFORE confirming\n"
       "_Borrow cost (20% APR) deducted from /itm r APY._",
       parse_mode=ParseMode.MARKDOWN,
   )


async def cmd_whoami(update, context):
   u = update.effective_user
   await update.message.reply_text(
       f"👤 `{u.id}`  —  {u.full_name}", parse_mode=ParseMode.MARKDOWN
   )


@authorized_only
async def cmd_list(update, context):
   tickers = github_store.get_tickers()
   if not tickers:
       await update.message.reply_text("_Empty._", parse_mode=ParseMode.MARKDOWN)
       return
   await update.message.reply_text(
       f"*Watchlist ({len(tickers)})*\n" + ", ".join(f"`{t}`" for t in tickers),
       parse_mode=ParseMode.MARKDOWN,
   )


@authorized_only
async def cmd_add(update, context):
   if not context.args:
       await update.message.reply_text("Usage: `/add AAPL`", parse_mode=ParseMode.MARKDOWN)
       return
   lines = [github_store.add_ticker(t)[1] for t in context.args]
   await update.message.reply_text("\n".join(lines))


@authorized_only
async def cmd_remove(update, context):
   if not context.args:
       await update.message.reply_text("Usage: `/remove AAPL`", parse_mode=ParseMode.MARKDOWN)
       return
   lines = [github_store.remove_ticker(t)[1] for t in context.args]
   await update.message.reply_text("\n".join(lines))


@authorized_only
async def cmd_logs(update, context):
   if not _LAST_ERRORS:
       await update.message.reply_text("_No errors._", parse_mode=ParseMode.MARKDOWN)
       return
   body = "\n".join(_LAST_ERRORS)
   if len(body) > 3800:
       body = body[-3800:]
   await update.message.reply_text(
       "*Recent errors:*\n```\n" + body + "\n```",
       parse_mode=ParseMode.MARKDOWN,
   )


# ---------------------------------------------------------------------------
# Generic scan runner
# ---------------------------------------------------------------------------

async def _run_scan(update, context, scanner, label_emoji, format_summary_fn,
                   tickers_override=None, scan_kwargs=None, summary_kwargs=None,
                   hits_with_buttons=False, scanner_key=None):
   tickers = tickers_override if tickers_override is not None else github_store.get_tickers()
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

   if hits_with_buttons and all_hits:
       if scanner_key == "itm":
           all_hits.sort(key=lambda r: r.get("locked_apy", 0))
           top_hits = all_hits[-MAX_TRADE_BUTTONS:]
           for hit in top_hits:
               try:
                   await _send_itm_trade_button(update, context, hit)
                   await asyncio.sleep(0.5)
               except Exception as e:
                   logger.warning(f"trade button send failed for {hit.get('ticker')}: {e}")

       elif scanner_key == "itm_r":
           all_hits.sort(key=lambda r: r.get("locked_apy", 0))
           top_hits = all_hits[-MAX_TRADE_BUTTONS:]
           for hit in top_hits:
               try:
                   await _send_rtrade_button(update, context, hit)
                   await asyncio.sleep(0.5)
               except Exception as e:
                   logger.warning(f"rtrade button send failed for {hit.get('ticker')}: {e}")

       elif scanner_key == "dca":
           all_hits.sort(key=lambda r: r.get("score_apy", 0))
           top_hits = all_hits[-MAX_TRADE_BUTTONS:]
           for hit in top_hits:
               try:
                   await _send_dca_trade_button(update, context, hit)
                   await asyncio.sleep(0.5)
               except Exception as e:
                   logger.warning(f"dca button send failed for {hit.get('ticker')}: {e}")


# ---------------------------------------------------------------------------
# Trade buttons
# ---------------------------------------------------------------------------

async def _send_itm_trade_button(update, context, hit):
   """ITM: inline ✅ Confirm / ❌ Cancel — no typing needed."""
   trade_id = uuid.uuid4().hex[:8]
   user_id  = update.effective_user.id
   apy      = hit.get("locked_apy", 0)

   logger.info(f"_send_itm_trade_button: user={user_id} trade_id={trade_id} "
               f"ticker={hit.get('ticker')} apy={apy}")

   _PENDING_TRADES[(user_id, trade_id)] = {
       "hit":        hit,
       "walk_step":  0,
       "expires_at": time.time() + PENDING_TIMEOUT_SEC * 30,
       "reverse":    False,
       "trade_type": "itm",
   }

   summary = (
       f"🔒 *{hit['ticker']}* @ ${hit['spot']} · {hit['exp_date']} ({hit['dte']}d)\n"
       f"Strike ${hit['strike']:g} · Net credit ${hit.get('net_credit', 0):.2f}/sh\n"
       f"💳 Pay ${hit.get('primary_debit', 0):.2f}/sh → *{apy:.1f}% APY*\n"
       f"OI {hit['call_oi']}/{hit['put_oi']} · Locked ${hit.get('locked_total', 0):.0f}"
   )
   keyboard = InlineKeyboardMarkup([[
       InlineKeyboardButton(
           f"✅ Confirm @ {apy:.1f}% APY",
           callback_data=f"confirm_trade:{trade_id}",
       ),
       InlineKeyboardButton(
           "❌ Cancel",
           callback_data=f"cancel_trade:{trade_id}",
       ),
   ]])
   try:
       await update.message.reply_text(
           summary, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
   except BadRequest as e:
       logger.warning(f"itm trade button markdown failed: {e}")
       plain = summary.replace("*", "").replace("_", "").replace("`", "")
       try:
           await update.message.reply_text(plain, reply_markup=keyboard)
       except BadRequest as e2:
           logger.error(f"itm trade button failed