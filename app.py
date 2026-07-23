"""
bot/app.py

Wires every command/callback/message handler onto a python-telegram-bot
Application. `build_app(...)` is the single public entry point the rest
of the project (main.py) calls.
"""
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
)

from .commands_basic import (
    cmd_start, cmd_help, cmd_whoami, cmd_list, cmd_add, cmd_remove, cmd_logs,
)
from .commands_positions import cmd_positions
from .commands_scanners import (
    cmd_scan, cmd_spreads, cmd_deepcall, cmd_dca, cmd_csp, cmd_itm, cmd_ritm, cmd_itmib,
)
from .callbacks_trade import (
    cb_confirm_trade, cb_cancel_trade,
    cb_confirm_dca, cb_cancel_dca,
    cb_confirm_rtrade, cb_cancel_rtrade,
)
from .order_monitor import cb_improve, cb_cancel
from .token_commands import cmd_refresh_token, cmd_submit_token
from .itmt import cmd_itmt
from .commands_fills import cmd_fills, cmd_stop, cmd_cancel_all
from .handlers import handle_yes_reply


def build_app(telegram_token, collar_scanner, spread_scanner, deepcall_scanner,
              dca_scanner, csp_scanner, itm_scanner, ritm_scanner,
              schwab_clients: dict, primary_user_id: int, itm_ibkr_scanner=None):
    app = Application.builder().token(telegram_token).concurrent_updates(True).build()
    app.bot_data["collar_scanner"]    = collar_scanner
    app.bot_data["spread_scanner"]    = spread_scanner
    app.bot_data["deepcall_scanner"]  = deepcall_scanner
    app.bot_data["dca_scanner"]       = dca_scanner
    app.bot_data["csp_scanner"]       = csp_scanner
    app.bot_data["itm_scanner"]       = itm_scanner
    app.bot_data["ritm_scanner"]      = ritm_scanner
    app.bot_data["schwab_clients"]    = schwab_clients
    app.bot_data["primary_user_id"]   = primary_user_id
    app.bot_data["itm_ibkr_scanner"]  = itm_ibkr_scanner
    primary_schwab = schwab_clients.get(primary_user_id)
    app.bot_data["schwab_client"]     = primary_schwab

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("whoami",        cmd_whoami))
    app.add_handler(CommandHandler("list",          cmd_list))
    app.add_handler(CommandHandler("add",           cmd_add))
    app.add_handler(CommandHandler("remove",        cmd_remove))
    app.add_handler(CommandHandler("scan",          cmd_scan))
    app.add_handler(CommandHandler("spreads",       cmd_spreads))
    app.add_handler(CommandHandler("deepcall",      cmd_deepcall))
    app.add_handler(CommandHandler("deepcalls",     cmd_deepcall))
    app.add_handler(CommandHandler("dca",           cmd_dca))
    app.add_handler(CommandHandler("csp",           cmd_csp))
    app.add_handler(CommandHandler("itm",           cmd_itm))
    app.add_handler(CommandHandler("ritm",          cmd_ritm))
    app.add_handler(CommandHandler("itmib",         cmd_itmib))
    app.add_handler(CommandHandler("positions",     cmd_positions))
    app.add_handler(CommandHandler("logs",          cmd_logs))
    app.add_handler(CommandHandler("refresh_token", cmd_refresh_token))
    app.add_handler(CommandHandler("submit_token",  cmd_submit_token))

    app.add_handler(CommandHandler("i",              cmd_itm))
    app.add_handler(CommandHandler("c",              cmd_cancel_all))
    app.add_handler(CommandHandler("itmt",           cmd_itmt))
    app.add_handler(CommandHandler("itmm",           cmd_itmt))
    app.add_handler(CommandHandler("r",              cmd_refresh_token))
    app.add_handler(CommandHandler("s",              cmd_submit_token))
    app.add_handler(CommandHandler("stop",           cmd_stop))
    app.add_handler(CommandHandler("x",              cmd_stop))
    app.add_handler(CommandHandler("fills",          cmd_fills))
    app.add_handler(CommandHandler("f",              cmd_fills))

    app.add_handler(CallbackQueryHandler(cb_confirm_trade,  pattern=r"^confirm_trade:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_trade,   pattern=r"^cancel_trade:"))
    app.add_handler(CallbackQueryHandler(cb_confirm_dca,    pattern=r"^confirm_dca:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_dca,     pattern=r"^cancel_dca:"))
    app.add_handler(CallbackQueryHandler(cb_confirm_rtrade, pattern=r"^confirm_rtrade:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_rtrade,  pattern=r"^cancel_rtrade:"))
    app.add_handler(CallbackQueryHandler(cb_improve, pattern=r"^improve:"))
    app.add_handler(CallbackQueryHandler(cb_cancel,  pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_yes_reply))

    return app
