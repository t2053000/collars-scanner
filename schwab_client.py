"""
schwab_client.py
Schwab API wrapper.
"""

import os
import logging
from datetime import datetime, timedelta

import schwab
from schwab.client import Client

logger = logging.getLogger(__name__)


class SchwabClient:
    def __init__(self):
        self.api_key = os.environ["SCHWAB_APP_KEY"]
        self.api_secret = os.environ["SCHWAB_APP_SECRET"]
        self.token_path = os.getenv("SCHWAB_TOKEN_PATH", "token.json")
        self.redirect_uri = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
        self._client = None

    def initialize(self):
        self._client = schwab.auth.client_from_token_file(
            self.token_path,
            self.api_key,
            self.api_secret,
        )
        logger.info("Schwab client initialised from token file.")

    def get_quote(self, symbol: str) -> float:
        resp = self._client.get_quote(symbol)
        resp.raise_for_status()
        data = resp.json()
        return float(data[symbol]["quote"]["lastPrice"])

    def get_option_chain(self, symbol: str, strike_count: int = 30) -> dict:
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

    def get_fundamentals(self, symbol: str) -> dict:
        """
        Returns a dict like:
        {
          "dividendAmount": <annual $>,
          "dividendYield":  <%>,
          "exDividendDate": "YYYY-MM-DD..."
        }
        Returns empty dict on failure.
        """
        try:
            resp = self._client.get_instruments(
                symbol,
                projection=Client.Instrument.Projection.FUNDAMENTAL,
            )
            resp.raise_for_status()
            data = resp.json()
            instruments = data.get("instruments") or []
            if not instruments:
                return {}
            f = instruments[0].get("fundamental") or {}
            return f
        except Exception as e:
            logger.warning(f"[{symbol}] fundamentals fetch failed: {e}")
            return {}
