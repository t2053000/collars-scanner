"""
IBKR TWS API client — wraps ib_insync for option chain scanning.
Connects to IB Gateway via Tailscale tunnel.
Used for scanning only — order execution stays on Schwab.
Connection: Railway → Tailscale → Hostinger VPS → IB Gateway port 4002
"""
import os
import logging
import threading
import asyncio
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

# ib_insync must be installed: pip install ib_insync
try:
    from ib_insync import IB, Stock, Option, Contract, util
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False
    logger.warning("ib_insync not installed — IBKR scanning unavailable")

IBKR_HOST = os.getenv("IBKR_HOST", "localhost")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))
IBKR_CLIENT_ID = 10


class IbkrClient:
    """
    Thin wrapper around ib_insync IB connection.
    Thread-safe — uses a lock for all TWS API calls.
    """
    def __init__(self, host: str = IBKR_HOST, port: int = IBKR_PORT,
                 client_id: int = IBKR_CLIENT_ID):
        if not IB_AVAILABLE:
            raise RuntimeError("ib_insync not installed")
        self.host = host
        self.port = port
        self.client_id = client_id
        self._ib = IB()
        self._lock = threading.Lock()
        self._connected = False

    def connect(self):
        # Fix for asyncio in threads
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        if self._connected:
            return
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id,
                             timeout=10, readonly=True)
            self._connected = True
            logger.info(f"IbkrClient connected to {self.host}:{self.port} "
                        f"clientId={self.client_id}")
        except Exception as e:
            self._connected = False
            raise RuntimeError(f"IbkrClient connect failed: {e}")

    def disconnect(self):
        if self._connected:
            self._ib.disconnect()
            self._connected = False
            logger.info("IbkrClient disconnected")

    def _ensure_connected(self):
        if not self._connected or not self._ib.isConnected():
            self.connect()

    # -----------------------------------------------------------------------
    # Market data
    # -----------------------------------------------------------------------
    def get_spot(self, ticker: str) -> Optional[float]:
        """Return last/mark price for a stock."""
        with self._lock:
            self._ensure_connected()
            contract = Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            ticker_data = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(1.5)
            price = ticker_data.last or ticker_data.close or ticker_data.bid
            self._ib.cancelMktData(contract)
            return float(price) if price and price > 0 else None

    def get_option_chain(self, ticker: str) -> dict:
        """
        Fetch option chain for ticker.
        Returns dict in same format as Schwab's get_option_chain response
        so scan_ticker_reverse can consume it identically.
        """
        # Fix for asyncio in threads
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        with self._lock:
            self._ensure_connected()
            stock = Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(stock)
            # Get spot price
            mkt = self._ib.reqMktData(stock, "", False, False)
            self._ib.sleep(1.5)
            spot = mkt.last or mkt.close or mkt.bid
            self._ib.cancelMktData(stock)
            if not spot or spot <= 0:
                return {}
            # Get available expirations and strikes
            chains = self._ib.reqSecDefOptParams(
                stock.symbol, "", stock.secType, stock.conId)
            if not chains:
                return {}
            chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
            expirations = sorted(chain.expirations)
            strikes = sorted(chain.strikes)
            today = datetime.utcnow()
            call_exp_map = {}
            put_exp_map = {}
            for exp_str in expirations:
                try:
                    exp_dt = datetime.strptime(exp_str, "%Y%m%d")
                except ValueError:
                    continue
                dte = (exp_dt - today).days
                if dte < 1 or dte > 14:
                    continue
                exp_date = exp_dt.strftime("%Y-%m-%d")
                exp_key = f"{exp_date}:{dte}"
                # Only fetch strikes near spot — 4 above and below
                near_strikes = [s for s in strikes
                                if spot * 0.85 <= s <= spot * 1.15]
                if not near_strikes:
                    continue
                call_strikes = {}
                put_strikes = {}
                # Build option contracts for this expiry
                call_contracts = [
                    Option(ticker, exp_str, s, "C", "SMART")
                    for s in near_strikes
                ]
                put_contracts = [
                    Option(ticker, exp_str, s, "P", "SMART")
                    for s in near_strikes
                ]
                all_contracts = call_contracts + put_contracts
                try:
                    self._ib.qualifyContracts(*all_contracts)
                except Exception:
                    continue
                tickers_data = self._ib.reqTickers(*all_contracts)
                self._ib.sleep(2.0)
                for td in tickers_data:
                    c = td.contract
                    if not hasattr(c, "right"):
                        continue
                    bid = td.bid if td.bid and td.bid > 0 else 0.0
                    ask = td.ask if td.ask and td.ask > 0 else 0.0
                    oi = int(td.callOpenInterest or td.putOpenInterest or 0)
                    opt_dict = {
                        "bid": bid,
                        "ask": ask,
                        "openInterest": oi,
                        "last": td.last or 0.0,
                    }
                    strike_str = str(float(c.strike))
                    if c.right == "C":
                        call_strikes[strike_str] = [opt_dict]
                    else:
                        put_strikes[strike_str] = [opt_dict]
                if call_strikes:
                    call_exp_map[exp_key] = call_strikes
                if put_strikes:
                    put_exp_map[exp_key] = put_strikes
            return {
                "underlyingPrice": float(spot),
                "callExpDateMap": call_exp_map,
                "putExpDateMap": put_exp_map,
            }

    def get_short_availability(self, ticker: str) -> dict:
        """
        Check if shares are available to short and get borrow rate.
        Returns {"available": bool, "borrow_rate_pct": float}
        """
        # Fix for asyncio in threads
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        with self._lock:
            self._ensure_connected()
            try:
                stock = Stock(ticker, "SMART", "USD")
                self._ib.qualifyContracts(stock)
                availability = self._ib.reqShortableShares(stock)
                self._ib.sleep(1.0)
                available = availability is not None and availability > 100
                return {"available": available, "borrow_rate_pct": 0.0}
            except Exception as e:
                logger.warning(f"get_short_availability failed for {ticker}: {e}")
                return {"available": True, "borrow_rate_pct": 0.0}

    def get_fundamentals(self, ticker: str) -> dict:
        """
        Stub — IBKR fundamentals require separate subscription.
        Returns empty dict so caller falls back to defaults.
        """
        return {}
