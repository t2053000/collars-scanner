"""
schwab_client.py
Schwab API wrapper with on-the-fly token refresh.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

import httpx
import schwab
from schwab.client import Client

logger = logging.getLogger(__name__)


class SchwabClient:
    def __init__(self):
        self.api_key      = os.environ["SCHWAB_APP_KEY"]
        self.api_secret   = os.environ["SCHWAB_APP_SECRET"]
        self.token_path   = os.getenv("SCHWAB_TOKEN_PATH", "token.json")
        self.redirect_uri = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
        self._client = None

    def initialize(self):
        self._client = schwab.auth.client_from_token_file(
            self.token_path,
            self.api_key,
            self.api_secret,
        )
        logger.info("Schwab client initialised from token file.")

    def reload(self):
        """Re-initialise the underlying client after token.json was rewritten."""
        self.initialize()
        logger.info("Schwab client reloaded after token refresh.")

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> float:
        resp = self._client.get_quote(symbol)
        resp.raise_for_status()
        data = resp.json()
        return float(data[symbol]["quote"]["lastPrice"])

    def get_option_chain(self, symbol: str, strike_count: int = 5) -> dict:
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

    # ------------------------------------------------------------------
    # OAuth helpers used by /refresh_token + /submit_token
    # ------------------------------------------------------------------

    def build_authorize_url(self) -> str:
        """The URL the user pastes into their phone browser."""
        return (
            "https://api.schwabapi.com/v1/oauth/authorize"
            f"?client_id={self.api_key}&redirect_uri={self.redirect_uri}"
        )

    def exchange_code_for_token(self, redirect_url: str) -> None:
        """
        Take the full redirected URL (https://127.0.0.1/?code=...&session=...),
        exchange the code for an access + refresh token,
        write to token.json in schwab-py's format, and reload the client.
        """
        parsed = urlparse(redirect_url)
        qs = parse_qs(parsed.query)
        code_list = qs.get("code")
        if not code_list:
            raise ValueError("No 'code=' parameter found in URL.")
        code = code_list[0]

        # Schwab requires the literal '@' decoded; parse_qs already decodes %40 etc.
        # Do not strip / re-quote.

        creds = f"{self.api_key}:{self.api_secret}".encode()
        headers = {
            "Authorization": "Basic " + base64.b64encode(creds).decode(),
            "Content-Type":  "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": self.redirect_uri,
        }

        resp = httpx.post(
            "https://api.schwabapi.com/v1/oauth/token",
            headers=headers,
            data=data,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Token exchange failed: HTTP {resp.status_code} – {resp.text[:300]}"
            )
        token_payload = resp.json()

        # schwab-py expects:  {"creation_timestamp": int, "token": {...}}
        wrapped = {
            "creation_timestamp": int(time.time()),
            "token": token_payload,
        }
        with open(self.token_path, "w") as f:
            json.dump(wrapped, f)

        logger.info("New token written to %s", self.token_path)
        self.reload()
