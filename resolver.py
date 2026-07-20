"""Resolver — closes paper positions when markets resolve.

Uses CLOB API's /markets/{condition_id} endpoint which returns:
  - closed: True/False
  - tokens: [{outcome: "Yes", winner: true, price: 1}, {outcome: "No", winner: false, price: 0}]

The tokens.winner field is the definitive resolution signal.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import httpx

from paper_book import close_paper_position

log = logging.getLogger(__name__)

CLOB_MARKETS_URL = "https://clob.polymarket.com/markets"
RESOLVER_INTERVAL = 900  # 15 min


def _fetch_market(http: httpx.Client, condition_id: str) -> dict | None:
    try:
        r = http.get(f"{CLOB_MARKETS_URL}/{condition_id}", timeout=10.0)
        if r.status_code != 200:
            return None
        return r.json()
    except httpx.HTTPError as e:
        log.warning("CLOB fetch failed for %s: %s", condition_id[:16], e)
        return None


def _resolve_outcome(market_data: dict, our_outcome: str) -> float | None:
    """Determine close price for our outcome. Returns 1.0 (won), 0.0 (lost), or None (not resolved).

    Token matching logic:
      - Exact match on outcome label (handles "Yes"/"No")
      - If our outcome is a team/player name (not "Yes"/"No"), it maps to the "Yes" token
        because the condition is always "Will [team/player] win?"
      - If our outcome is "Over"/"Under", map to "Yes"/"No" respectively
    """
    if not market_data.get("closed"):
        return None

    tokens = market_data.get("tokens", [])
    if not tokens:
        return None

    # Check if any token has winner field set
    has_resolution = any(t.get("winner") is True for t in tokens)
    if not has_resolution:
        # Market closed but not yet resolved (settlement pending)
        return None

    # Build outcome → winner mapping
    token_map = {}
    for t in tokens:
        outcome_label = t.get("outcome", "")
        token_map[outcome_label.lower()] = t.get("winner", False)

    # Try exact match first
    if our_outcome.lower() in token_map:
        return 1.0 if token_map[our_outcome.lower()] else 0.0

    # Non-standard outcomes (team names, player names, "Over"/"Under")
    # These map to "Yes" because the condition is "Will X win/happen?"
    standard_no = {"no", "under", "draw"}
    if our_outcome.lower() in standard_no:
        return 1.0 if token_map.get("no", False) else 0.0
    else:
        # Any non-standard outcome (team name, player name, "Over") → maps to "Yes"
        return 1.0 if token_map.get("yes", False) else 0.0


def resolve_once(conn: sqlite3.Connection, http: httpx.Client) -> int:
    """One resolver pass. Returns # of positions closed."""
    rows = conn.execute(
        """SELECT id, market, outcome
             FROM paper_positions
            WHERE status='open'"""
    ).fetchall()
    if not rows:
        return 0

    # Ensure columns exist
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_positions)")}
    for col, ddl in [("end_date", "TEXT DEFAULT ''"), ("category", "TEXT DEFAULT ''")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {ddl}")
            conn.commit()

    # Dedupe markets
    markets_to_check = sorted({r[1] for r in rows})
    market_cache: dict[str, dict] = {}
    for cid in markets_to_check:
        data = _fetch_market(http, cid)
        if data:
            market_cache[cid] = data
        time.sleep(0.1)

    # Backfill end_date
    for pos_id, market, outcome in rows:
        data = market_cache.get(market, {})
        end_date = ""
        for key in ("end_date_iso", "endDate", "end_date", "game_start_time"):
            val = data.get(key)
            if val:
                end_date = str(val)
                break
        if end_date:
            conn.execute(
                "UPDATE paper_positions SET end_date=? WHERE id=? AND (end_date IS NULL OR end_date='')",
                (end_date, pos_id),
            )

    # Resolve positions
    closed = 0
    now_ts = int(time.time())

    for pos_id, market, outcome in rows:
        data = market_cache.get(market)
        if not data:
            continue

        price = _resolve_outcome(data, outcome)

        # ---- Fallback: market past end_date + price at extreme ----
        # Polymarket sometimes delays setting closed=True after the event ends.
        # If the market is past its end_date AND price is at a terminal extreme
        # (>0.995 or <0.005), treat it as effectively resolved.
        if price is None:
            end_date_str = ""
            for key in ("end_date_iso", "game_start_time", "end_date"):
                val = data.get(key)
                if val:
                    end_date_str = str(val)
                    break
            if end_date_str:
                try:
                    from datetime import datetime, timezone
                    ed = end_date_str.rstrip("Z").split(".")[0]
                    dt = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc)
                    hours_past = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    if hours_past >= 3:  # at least 3 hours past end date
                        tokens = data.get("tokens", [])
                        # Find our token's current price
                        our_price = None
                        token_map = {t.get("outcome","").lower(): float(t.get("price", 0.5))
                                     for t in tokens}
                        standard_no = {"no", "under", "draw"}
                        if outcome.lower() in token_map:
                            our_price = token_map[outcome.lower()]
                        elif outcome.lower() in standard_no:
                            our_price = token_map.get("no", 0.5)
                        else:
                            our_price = token_map.get("yes", 0.5)

                        if our_price is not None and (our_price >= 0.995 or our_price <= 0.005):
                            price = 1.0 if our_price >= 0.995 else 0.0
                            log.info("pos #%d: price-based resolution (%.4f, %.1fh past end_date) outcome=%s",
                                     pos_id, our_price, hours_past, outcome)
                except Exception as e:
                    log.debug("fallback resolve parse error: %s", e)

        if price is None:
            continue

        result = "WON" if price == 1.0 else "LOST"
        question = data.get("question", data.get("description", ""))[:45]
        log.info("pos #%d: %s — %s [%s] (payout=$%.2f/share)",
                 pos_id, result, question, outcome, price)

        close_paper_position(conn, pos_id, price)
        closed += 1

    conn.commit()
    return closed
