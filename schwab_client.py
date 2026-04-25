"""
schwab_client.py
Wrapper around schwab-py for option chain data.
Token is read from file (path set via env var SCHWAB_TOKEN_PATH).
On Railway, populate SCHWAB_TOKEN_JSON env var and the client
will write it to disk on first start (see token bootstrap in main.py).
"""

import os
import json
import logging
from datetime import datetime, timedelta

import schwab
from schwab.client import Client

logger = logging.getLogger(__name__)


class SchwabClient:
    def __init__(self):
        self.api_key     = os.environ["SCHWAB_APP_KEY"]
        self.api_secret  = os.environ["SCHWAB_APP_SECRET"]
        self.token_path  = os.getenv("SCHWAB_TOKEN_PATH", "token.json")
        self.redirect_uri = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
        self._client = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Load client from saved token file.  Run setup_auth.py first."""
        self._client = schwab.auth.client_from_token_file(
            self.token_path,
            self.api_key,
            self.api_secret,
        )
        logger.info("Schwab client initialised from token file.")

    # ------------------------------------------------------------------
    # Market data helpers
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> float:
        """Return the latest last-price for a symbol."""
        resp = self._client.get_quote(symbol)
        resp.raise_for_status()
        data = resp.json()
        # Schwab quote response: {SYMBOL: {quote: {lastPrice: ...}}}
        return float(data[symbol]["quote"]["lastPrice"])

    def get_option_chain(self, symbol: str, strike_count: int = 5) -> dict:
        """
        Return the full option-chain JSON for *symbol*.
        strike_count controls how many strikes above/below ATM are returned;
        5 is plenty since we only need the first strike away from the spot.
        We request a 1-year window so we always capture at least 10 expirations.
        """
        from_date = datetime.now().date()
        to_date   = (datetime.now() + timedelta(days=365)).date()

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
