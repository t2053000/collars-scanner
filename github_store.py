"""
schwab_client.py
Schwab API client wrapper with defensive input normalization.
"""

import logging
from datetime import datetime, timedelta

from schwab import auth, client
from schwab.client import Client

logger = logging.getLogger(__name__)


class SchwabClient:
    def __init__(self, token_path: str):
        self._client = auth.easy_client(
            api_key="your_api_key",           # keep your real values
            app_secret="your_app_secret",
            callback_url="your_callback_url",
            token_path=token_path,
        )

    # =====================================================================
    # Defensive normalization added to protect against list/tuple input
    # =====================================================================

    def get_option_chain(self, symbol: str, strike_count: int = 30) -> dict:
        # === DEFENSIVE NORMALIZATION ===
        if isinstance(symbol, (list, tuple)):
            if symbol:
                logger.warning(
                    f"[schwab_client] get_option_chain received list/tuple, "
                    f"using first element: {symbol[0]}"
                )
                symbol = symbol[0]
            else:
                logger.error("[schwab_client] get_option_chain received empty list/tuple")
                return {}
        if not symbol or not isinstance(symbol, str):
            logger.error(f"[schwab_client] get_option_chain received invalid symbol: {symbol}")
            return {}

        from_date = datetime.now().date()
        to_date = (datetime.now() + timedelta(days=800)).date()

        resp = self._client.get_option_chain(
            symbol,
            contract_type=Client.Options.ContractType.ALL,
            strike_count=strike_count,
            include_underlying_quote=True,
            from_date=from_date,
            to_date=to_date,
        )
        resp.raise_for_status()
        return resp.json()

    def get_option_chain_lite(self, symbol: str, days: int = 15) -> dict:
        # === DEFENSIVE NORMALIZATION ===
        if isinstance(symbol, (list, tuple)):
            if symbol:
                logger.warning(
                    f"[schwab_client] get_option_chain_lite received list/tuple, "
                    f"using first element: {symbol[0]}"
                )
                symbol = symbol[0]
            else:
                logger.error("[schwab_client] get_option_chain_lite received empty list/tuple")
                return {}
        if not symbol or not isinstance(symbol, str):
            logger.error(f"[schwab_client] get_option_chain_lite received invalid symbol: {symbol}")
            return {}

        try:
            from_date = datetime.now().date()
            to_date = (datetime.now() + timedelta(days=days)).date()

            resp = self._client.get_option_chain(
                symbol,
                contract_type=Client.Options.ContractType.ALL,
                strike_count=4,
                include_underlying_quote=True,
                from_date=from_date,
                to_date=to_date,
            )
            if resp.status_code != 200:
                return {}
            return resp.json()
        except Exception as e:
            logger.debug(f"[{symbol}] get_option_chain_lite failed: {e}")
            return {}

    # =====================================================================
    # Other methods (unchanged)
    # =====================================================================

    def get_fundamentals(self, symbol: str):
        try:
            resp = self._client.get_fundamental(symbol)
            resp.raise_for_status()
            data = resp.json()
            return data.get("fundamental", {})
        except Exception as e:
            logger.warning(f"get_fundamentals failed for {symbol}: {e}")
            return {}

    def get_quote(self, symbol: str):
        try:
            resp = self._client.get_quote(symbol)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"get_quote failed for {symbol}: {e}")
            return {}