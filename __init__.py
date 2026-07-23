"""
bot package

This used to be a single ~1500-line bot.py. It's now split by
responsibility across submodules (state, helpers, commands_*,
callbacks_trade, order_monitor, trade_cards, scan_runner, itmt,
handlers, app). `build_app` is re-exported here so existing callers
(`import bot as bot_module; bot_module.build_app(...)`) keep working
unchanged.
"""
from .app import build_app

__all__ = ["build_app"]
