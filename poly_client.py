"""Thin client for the Polymarket Data API.

Endpoints used:
  GET /trades?user={addr}&limit=...&offset=...    -- all trades for a wallet
  GET /positions?user={addr}&sizeThreshold=0      -- open positions
  GET /value?user={addr}                          -- current portfolio value

These are public, unauthenticated endpoints served at https://data-api.polymarket.com
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass
class Trade:
    """One fill — the raw API returns floats as strings, we normalize here."""
    timestamp: int        # unix seconds
    market: str           # condition_id
    outcome: str          # "Yes" / "No" / team name
    side: str             # "BUY" / "SELL"
    size: float           # shares
    price: float          # 0..1
    raw: dict             # keep the original for anything we missed

    @property
    def notional(self) -> float:
        return self.size * self.price


class PolyClient:
    def __init__(self, timeout: httpx.Timeout = DEFAULT_TIMEOUT, limits: httpx.Limits | None = None):
        kwargs = {"timeout": timeout, "headers": {"User-Agent": "copy-bot/0.1"}}
        if limits is not None:
            kwargs["limits"] = limits
        self._client = httpx.Client(**kwargs)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- retry wrapper ----
    def _get(self, url: str, params: dict | None = None, max_retries: int = 4) -> Any:
        delay = 0.5
        for attempt in range(max_retries):
            try:
                r = self._client.get(url, params=params)
                if r.status_code == 429:
                    log.warning("rate limited on %s — sleep %.1fs", url, delay)
                    time.sleep(delay); delay *= 2; continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600 and attempt < max_retries - 1:
                    log.warning("server %s on %s — retry", e.response.status_code, url)
                    time.sleep(delay); delay *= 2; continue
                raise
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
                if attempt < max_retries - 1:
                    log.warning("transport error %s — retry", type(e).__name__)
                    time.sleep(delay); delay *= 2; continue
                raise

    # ---- public methods ----
    def get_trades(self, wallet: str, limit: int = 500, offset: int = 0) -> list[Trade]:
        """Fetch trades for a wallet. One page at a time — caller paginates."""
        data = self._get(f"{DATA_API}/trades", params={
            "user": wallet.lower(),
            "limit": limit,
            "offset": offset,
            "takerOnly": "false",  # include maker trades
        })
        if not isinstance(data, list):
            log.warning("unexpected /trades response shape for %s: %r", wallet, type(data))
            return []
        return [self._parse_trade(t) for t in data]

    def get_all_trades(self, wallet: str, page_size: int = 500, max_pages: int = 20) -> list[Trade]:
        """Paginated; caps at max_pages * page_size for safety.

        Polymarket's /trades endpoint returns HTTP 400 once offset exceeds a server-side
        cap (observed at ~3500). We treat that as "end of available history" rather than
        a fatal error, so partial-but-useful results aren't discarded.
        """
        trades: list[Trade] = []
        for page in range(max_pages):
            try:
                batch = self.get_trades(wallet, limit=page_size, offset=page * page_size)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    log.info("pagination cap for %s at offset %d (%d trades collected)",
                             wallet, page * page_size, len(trades))
                    break
                raise
            trades.extend(batch)
            if len(batch) < page_size:
                break
        return trades

    def get_positions(self, wallet: str) -> list[dict]:
        """Returns all positions (open + recently closed with remaining PnL data)."""
        data = self._get(f"{DATA_API}/positions", params={
            "user": wallet.lower(),
            "sizeThreshold": 0,
        })
        return data if isinstance(data, list) else []

    def get_value(self, wallet: str) -> dict | None:
        """Total portfolio value / PnL snapshot. May not be available for all wallets."""
        try:
            data = self._get(f"{DATA_API}/value", params={"user": wallet.lower()})
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def get_market(self, condition_id: str) -> dict | None:
        """Fetch resolved market info (payout, outcome) from CLOB API."""
        try:
            return self._get(f"{CLOB_API}/markets/{condition_id}")
        except httpx.HTTPStatusError:
            return None

    # ---- parsing ----
    @staticmethod
    def _parse_trade(raw: dict) -> Trade:
        def f(v, default=0.0):
            try: return float(v)
            except (TypeError, ValueError): return default
        def i(v, default=0):
            try: return int(v)
            except (TypeError, ValueError): return default
        return Trade(
            timestamp=i(raw.get("timestamp")),
            market=str(raw.get("conditionId") or raw.get("market") or ""),
            outcome=str(raw.get("outcome") or ""),
            side=str(raw.get("side") or "").upper(),
            size=f(raw.get("size")),
            price=f(raw.get("price")),
            raw=raw,
        )
