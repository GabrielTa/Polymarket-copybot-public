"""Leader quality weighting — scores each leader by historical win rate.

Instead of counting all leaders equally toward conviction (4 leaders = ×4),
we weight by performance. A leader with 70% win rate on 20+ trades counts
as 1.4 toward conviction. A leader with 30% win rate counts as 0.6.

weighted_conviction_v2 also applies:
  - Time decay:  e^(-λ × hours_since_trade) — stale conviction counts less
  - Size weight: leader_trade_size / their_median_size — big-for-them bets count more
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import defaultdict

from config import cfg

log = logging.getLogger(__name__)

# Shared TTL for all caches
CACHE_TTL = 300  # 5 minutes

_cache: dict[str, float] = {}
_cache_ts: float = 0

_size_cache: dict[str, float] = {}
_size_cache_ts: float = 0

_excluded_cache: set[str] = set()
_excluded_cache_ts: float = 0


# ---------------------------------------------------------------------------
# Quality weights (win-rate based)
# ---------------------------------------------------------------------------

def _build_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """Calculate win-rate-based weight for each leader from closed positions."""
    min_trades = cfg["leader_weighting"]["min_trades_for_weight"]

    rows = conn.execute("""
        SELECT s.leader_wallet,
               COUNT(*) as total,
               SUM(CASE WHEN pp.pnl_usd > 0 THEN 1 ELSE 0 END) as wins
          FROM paper_positions pp
          JOIN signals s ON pp.signal_id = s.id
         WHERE pp.status = 'closed'
      GROUP BY s.leader_wallet
        HAVING total >= ?
    """, (min_trades,)).fetchall()

    weights = {}
    for wallet, total, wins in rows:
        win_rate = wins / total
        # 50% win rate → 1.0x, 70% → 1.4x, 30% → 0.6x
        weights[wallet] = round(win_rate / 0.5, 2)

    log.debug("leader weights: %d leaders scored, avg=%.2f",
              len(weights), sum(weights.values()) / len(weights) if weights else 0)
    return weights


def get_leader_weight(conn: sqlite3.Connection, wallet: str) -> float:
    """Get quality weight for a leader. Cached for performance."""
    global _cache, _cache_ts

    if not cfg["leader_weighting"]["enabled"]:
        return cfg["leader_weighting"]["default_weight"]

    now = time.time()
    if now - _cache_ts > CACHE_TTL:
        _cache = _build_weights(conn)
        _cache_ts = now

    return _cache.get(wallet, cfg["leader_weighting"]["default_weight"])


# ---------------------------------------------------------------------------
# Per-leader median trade size
# ---------------------------------------------------------------------------

def _build_size_cache(conn: sqlite3.Connection) -> dict[str, float]:
    """Compute each leader's median historical trade size from signals."""
    rows = conn.execute(
        "SELECT leader_wallet, leader_size FROM signals WHERE leader_size > 0"
    ).fetchall()

    sizes: dict[str, list[float]] = defaultdict(list)
    for wallet, size in rows:
        sizes[wallet].append(float(size))

    result = {}
    for wallet, sz_list in sizes.items():
        sz_list.sort()
        n = len(sz_list)
        if n % 2 == 0:
            result[wallet] = (sz_list[n // 2 - 1] + sz_list[n // 2]) / 2
        else:
            result[wallet] = sz_list[n // 2]
    return result


def get_leader_median_size(conn: sqlite3.Connection, wallet: str) -> float:
    """Get a leader's median trade size. Cached. Returns 100.0 if unknown."""
    global _size_cache, _size_cache_ts

    now = time.time()
    if now - _size_cache_ts > CACHE_TTL:
        _size_cache = _build_size_cache(conn)
        _size_cache_ts = now

    return _size_cache.get(wallet, 100.0)


# ---------------------------------------------------------------------------
# Leader auto-exclusion
# ---------------------------------------------------------------------------

def _build_excluded(conn: sqlite3.Connection) -> set[str]:
    """Compute the set of leaders to exclude based on config thresholds.

    Uses a rolling window of the most recent N trades per leader so that
    a previously bad leader who turns it around gets re-admitted automatically.
    """
    excl_cfg = cfg.get("leader_exclusion", {})
    blocked: set[str] = set(excl_cfg.get("blocked", []))

    auto_cfg = excl_cfg.get("auto_exclude", {})
    if not auto_cfg.get("enabled", False):
        return blocked

    min_trades = auto_cfg.get("min_trades", 15)
    rolling_window = auto_cfg.get("rolling_window", 30)
    max_win_rate = auto_cfg.get("max_win_rate", 0.40)
    max_roi_pct = auto_cfg.get("max_roi_pct", -25.0)

    if rolling_window and rolling_window > 0:
        # Evaluate only each leader's most recent N trades via ROW_NUMBER window function
        rows = conn.execute("""
            SELECT leader_wallet,
                   COUNT(*) as total,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl_usd) as total_pnl,
                   SUM(cost_usd) as total_cost
              FROM (
                SELECT s.leader_wallet, pp.pnl_usd, pp.cost_usd,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.leader_wallet
                           ORDER BY pp.closed_ts DESC
                       ) AS rn
                  FROM paper_positions pp
                  JOIN signals s ON pp.signal_id = s.id
                 WHERE pp.status = 'closed'
              )
             WHERE rn <= ?
          GROUP BY leader_wallet
            HAVING total >= ?
        """, (rolling_window, min_trades)).fetchall()
    else:
        # All-time stats
        rows = conn.execute("""
            SELECT s.leader_wallet,
                   COUNT(*) as total,
                   SUM(CASE WHEN pp.pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pp.pnl_usd) as total_pnl,
                   SUM(pp.cost_usd) as total_cost
              FROM paper_positions pp
              JOIN signals s ON pp.signal_id = s.id
             WHERE pp.status = 'closed'
          GROUP BY s.leader_wallet
            HAVING total >= ?
        """, (min_trades,)).fetchall()

    auto_excluded: set[str] = set()
    for wallet, total, wins, total_pnl, total_cost in rows:
        win_rate = wins / total
        roi = (total_pnl / total_cost * 100) if total_cost else 0.0
        if win_rate < max_win_rate or roi < max_roi_pct:
            auto_excluded.add(wallet)
            log.debug("auto-excluding %s: win=%.1f%% roi=%.1f%% (last %d trades)",
                      wallet[:12], win_rate * 100, roi, total)

    if auto_excluded:
        log.info("leader_exclusion: %d auto-excluded, %d manually blocked",
                 len(auto_excluded), len(blocked))
    return blocked | auto_excluded


def get_excluded_leaders(conn: sqlite3.Connection) -> set[str]:
    """Return the current set of excluded leader wallets. Cached."""
    global _excluded_cache, _excluded_cache_ts

    now = time.time()
    if now - _excluded_cache_ts > CACHE_TTL:
        _excluded_cache = _build_excluded(conn)
        _excluded_cache_ts = now

    return _excluded_cache


# ---------------------------------------------------------------------------
# Conviction scoring
# ---------------------------------------------------------------------------

def weighted_conviction(conn: sqlite3.Connection, wallets: list[str]) -> tuple[float, dict[str, float]]:
    """Legacy quality-only weighted conviction. Kept for backward compatibility."""
    if not cfg["leader_weighting"]["enabled"]:
        return float(len(wallets)), {w: 1.0 for w in wallets}

    weights = {w: get_leader_weight(conn, w) for w in wallets}
    total = sum(weights.values())
    return round(total, 2), weights


def weighted_conviction_v2(
    conn: sqlite3.Connection,
    conv_items: list[dict],
) -> tuple[float, dict[str, float]]:
    """Full weighted conviction: quality × time_decay × size_weight.

    conv_items: [{"wallet": str, "size": float, "ts": int}, ...]

    Weights:
      quality    = win_rate / 0.5  (1.0 baseline at 50% win rate)
      time_decay = e^(-λ × hours_since_trade)  (λ from config)
      size_weight = min(trade_size / leader_median_size, 3.0)  (capped at 3×)

    Returns (total_score, {wallet: combined_weight})
    """
    lambda_decay = cfg.get("conviction", {}).get("time_decay_lambda", 0.3)
    now = time.time()

    weights: dict[str, float] = {}
    for item in conv_items:
        wallet = item["wallet"]
        size = float(item.get("size") or 0.0)
        ts = int(item.get("ts") or now)

        quality = get_leader_weight(conn, wallet)

        hours_ago = max(0.0, (now - ts) / 3600)
        time_decay = math.exp(-lambda_decay * hours_ago)

        median_size = get_leader_median_size(conn, wallet)
        if median_size > 0 and size > 0:
            size_weight = min(size / median_size, 3.0)
        else:
            size_weight = 1.0

        weights[wallet] = round(quality * time_decay * size_weight, 3)

    total = sum(weights.values())
    return round(total, 2), weights
