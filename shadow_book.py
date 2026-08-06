"""Shadow book — in-memory (no real money) tracking of bets we DON'T make.

Any signal that passes conviction but gets SKIPPED by a downstream filter
(price band, resolution window, category/derivative blocks, concentration,
cooldown, exposure) is recorded here as a hypothetical position and resolved
on market resolution. This measures the win rate + PnL of everything we skip,
so every filter decision is validated *forward* on new data — not just by the
one-time backtest.

Flat $100 stake per shadow position, so win rate / ROI are comparable across
skip reasons and categories. Completely independent of the real paper book.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time

log = logging.getLogger(__name__)

SHADOW_STAKE = 100.0


def ensure_shadow_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shadow_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            market TEXT NOT NULL,
            market_title TEXT DEFAULT '',
            market_slug TEXT DEFAULT '',
            category TEXT DEFAULT '',
            outcome TEXT,
            side TEXT,
            entry_price REAL NOT NULL,
            shares REAL NOT NULL,
            signal_strength INTEGER DEFAULT 0,
            skip_reason TEXT DEFAULT '',
            end_date TEXT DEFAULT '',
            opened_ts INTEGER NOT NULL,
            closed_ts INTEGER,
            close_price REAL,
            pnl_usd REAL,
            status TEXT DEFAULT 'open'
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_positions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_reason ON shadow_positions(skip_reason)")
    # Links an exit-hold shadow back to the real position that was exited, so the
    # two can be compared directly (exit P&L vs hold-to-resolution P&L).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_positions)")}
    if "ref_position_id" not in cols:
        conn.execute("ALTER TABLE shadow_positions ADD COLUMN ref_position_id INTEGER DEFAULT 0")
    conn.commit()


def reason_bucket(reason: str) -> str:
    """Normalize 'price_too_low:0.32(longshot)' -> 'price_too_low'."""
    return re.split(r"[:(]", reason or "")[0].strip() or "unknown"


def open_shadow_position(
    conn: sqlite3.Connection,
    signal_id: int,
    market: str,
    outcome: str,
    side: str,
    entry_price: float,
    signal_strength: int,
    skip_reason: str,
    market_title: str = "",
    category: str = "",
    end_date: str = "",
    market_slug: str = "",
) -> None:
    """Record a hypothetical position for a skipped signal. No real money. Never raises."""
    if not market or entry_price <= 0 or entry_price >= 1:
        return
    # Dedup: one open shadow per market+outcome (mirrors the real book's already_in_market)
    existing = conn.execute(
        "SELECT 1 FROM shadow_positions WHERE status='open' AND market=? AND outcome=?",
        (market, outcome),
    ).fetchone()
    if existing:
        return
    shares = SHADOW_STAKE / entry_price
    conn.execute(
        """INSERT INTO shadow_positions
           (signal_id, market, market_title, market_slug, category, outcome, side,
            entry_price, shares, signal_strength, skip_reason, end_date, opened_ts, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
        (signal_id, market, market_title, market_slug, category, outcome, side,
         entry_price, shares, signal_strength, reason_bucket(skip_reason), end_date,
         int(time.time())),
    )


def open_exit_hold_shadow(
    conn: sqlite3.Connection,
    position_id: int,
    market: str,
    outcome: str,
    side: str,
    entry_price: float,
    signal_strength: int,
    exit_reason: str,
    market_title: str = "",
    category: str = "",
    end_date: str = "",
    market_slug: str = "",
) -> None:
    """Record what HOLDING an adverse-exited position to resolution would have paid.

    The exit path is otherwise unmeasured: we know what exiting cost, but not what
    the counterfactual was. Adverse exits realise ~-35% on average; whether that is
    money *saved* (the position was doomed) or money *destroyed* (it would have
    recovered) is unanswerable without this. Priced at the ORIGINAL entry price so
    the resulting P&L is directly comparable to the real position's exit P&L.

    Never raises — instrumentation must not break the exit path.
    """
    try:
        if not market or entry_price <= 0 or entry_price >= 1:
            return
        # One exit-hold shadow per real position.
        existing = conn.execute(
            "SELECT 1 FROM shadow_positions WHERE ref_position_id=?", (position_id,)
        ).fetchone()
        if existing:
            return
        bucket = "exit_hold_conviction" if str(exit_reason).startswith("conviction") else "exit_hold_adverse"
        conn.execute(
            """INSERT INTO shadow_positions
               (signal_id, ref_position_id, market, market_title, market_slug, category,
                outcome, side, entry_price, shares, signal_strength, skip_reason,
                end_date, opened_ts, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
            (0, position_id, market, market_title, market_slug, category, outcome, side,
             entry_price, SHADOW_STAKE / entry_price, signal_strength, bucket,
             end_date, int(time.time())),
        )
    except Exception as e:
        log.warning("exit-hold shadow failed for pos #%s: %s", position_id, e)


def resolve_shadow_once(conn: sqlite3.Connection, http) -> int:
    """Resolve open shadow positions on market resolution. Reuses the real resolver's
    market fetch + outcome logic. Returns # closed."""
    from resolver import _fetch_market, _resolve_outcome

    rows = conn.execute(
        "SELECT id, market, outcome, shares FROM shadow_positions WHERE status='open'"
    ).fetchall()
    if not rows:
        return 0

    markets = sorted({r[1] for r in rows})
    cache: dict[str, dict] = {}
    for cid in markets:
        data = _fetch_market(http, cid)
        if data:
            cache[cid] = data
        time.sleep(0.05)

    closed = 0
    for pos_id, market, outcome, shares in rows:
        data = cache.get(market)
        if not data:
            continue
        price = _resolve_outcome(data, outcome)
        if price is None:
            continue
        payout = shares * price
        pnl = payout - SHADOW_STAKE
        conn.execute(
            "UPDATE shadow_positions SET status='closed', closed_ts=?, close_price=?, pnl_usd=? WHERE id=?",
            (int(time.time()), price, pnl, pos_id),
        )
        closed += 1

    conn.commit()
    return closed
