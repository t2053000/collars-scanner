"""
schwab_client.py
Schwab API wrapper with market data + trading.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, quote

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
        self._account_hash = None

    def initialize(self):
        self._client = schwab.auth.client_from_token_file(
            self.token_path,
            self.api_key,
            self.api_secret,
        )
        logger.info("Schwab client initialised from token file.")

    def reload(self):
        self.initialize()
        logger.info("Schwab client reloaded after token refresh.")

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
            return instruments[0].get("fundamental") or {}
        except Exception as e:
            logger.warning(f"[{symbol}] fundamentals fetch failed: {e}")
            return {}

    # ------------------------------------------------------------------
    # OAuth helpers
    # ------------------------------------------------------------------

    def build_authorize_url(self) -> str:
        return (
            "https://api.schwabapi.com/v1/oauth/authorize"
            f"?response_type=code"
            f"&client_id={quote(self.api_key, safe='')}"
            f"&redirect_uri={quote(self.redirect_uri, safe='')}"
        )

    def exchange_code_for_token(self, code_or_url: str) -> None:
        text = code_or_url.strip().strip("'\"<>")
        code = None
        if text.startswith("http"):
            parsed = urlparse(text)
            qs = parse_qs(parsed.query)
            code_list = qs.get("code")
            if code_list:
                code = code_list[0]
        else:
            code = text
        if not code:
            raise ValueError("No 'code' value found in input.")

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
        wrapped = {
            "creation_timestamp": int(time.time()),
            "token": token_payload,
        }
        with open(self.token_path, "w") as f:
            json.dump(wrapped, f)
        logger.info("New token written to %s", self.token_path)
        self.reload()

    # ------------------------------------------------------------------
    # TRADING METHODS
    # ------------------------------------------------------------------

    def get_account_hash(self) -> str:
        """Fetch and cache the encrypted account hash (Schwab uses these instead of account numbers)."""
        if self._account_hash:
            return self._account_hash
        resp = self._client.get_account_numbers()
        resp.raise_for_status()
        accounts = resp.json()
        if not accounts:
            raise RuntimeError("No Schwab accounts found.")
        # Take first account; if you have multiple, this picks the primary
        self._account_hash = accounts[0]["hashValue"]
        logger.info(f"Resolved Schwab account hash (first account).")
        return self._account_hash

    def place_order(self, order_payload: dict) -> str:
        """
        Submit an order. Returns the Schwab orderId as string.
        order_payload follows Schwab's Order schema (see orders.py builders).
        """
        account_hash = self.get_account_hash()
        resp = self._client.place_order(account_hash, order_payload)
        # Schwab returns 201 with order ID in Location header
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Order placement failed: HTTP {resp.status_code} – {resp.text[:500]}"
            )
        # Order ID is in the Location header: .../orders/12345
        location = resp.headers.get("Location", "")
        order_id = location.rsplit("/", 1)[-1] if location else ""
        if not order_id:
            raise RuntimeError(f"Could not extract order ID from response. Headers: {dict(resp.headers)}")
        logger.info(f"Order placed successfully, ID: {order_id}")
        return order_id

    def get_order_status(self, order_id: str) -> dict:
        """Get current status of an order by ID."""
        account_hash = self.get_account_hash()
        resp = self._client.get_order(order_id, account_hash)
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> None:
        """Cancel a working order."""
        account_hash = self.get_account_hash()
        resp = self._client.cancel_order(order_id, account_hash)
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Cancel failed: HTTP {resp.status_code} – {resp.text[:300]}"
            )
        logger.info(f"Order {order_id} cancelled successfully.")
