"""Leader ranker.

Pulls trade history for every seed wallet and computes skill metrics we can
actually copy against. Writes a ranked leader table to SQLite.

The composite score penalizes:
  - insufficient sample size (<30 resolved positions)
  - very low avg trade size (noise wallets)
  - narrow category diversity (likely one-shot lucky wallets)

And rewards:
  - positive edge (fill_price → resolution_price delta, size-weighted)
  - consistent hit rate vs the implied probability at entry
  - persistence across multiple leaderboard snapshots (from seeds.json)

This module is designed to be called daily by the worker. It's idempotent —
re-running replaces the stored ranking.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import httpx

from poly_client import PolyClient, Trade

log = logging.getLogger(__name__)

BASE = Path(__file__).parent

# Concurrent HTTP workers for the per-wallet trade/positions/value fetches.
# httpx.Client is thread-safe, so a single client is shared across the pool
# (same pattern as the poll loop's POLL_WORKERS). Tuned to 12: the data-api
# rate-limits hard above ~12 (measured: 8w=38min, 12w=30min, 20w=102min as the
# 429 backoff serializes everything). 12 is just below that cliff.
RANK_WORKERS = 12
SEEDS_PATH = BASE / "data" / "seeds.json"
DB_PATH = BASE / "data" / "copybot.db"

# Skill thresholds
MIN_MARKETS = 5                    # wallet must have traded at least this many distinct markets
MIN_AVG_NOTIONAL_USD = 25.0        # drops noise / micro accounts
MAX_DAYS_INACTIVE = 60             # drops dead wallets
RECENT_WINDOW_DAYS = 30            # used for recency scoring


@dataclass
class LeaderStats:
    wallet: str
    trades_total: int
    markets_total: int
    resolved_positions: int
    hit_rate: float           # fraction of resolved positions that paid out
    avg_edge_bps: float       # size-weighted edge in basis points
    avg_notional: float       # avg $ per trade
    total_pnl_usd: float
    categories: list[str]
    persistence: int          # from seeds.json
    best_seed_rank: int       # from seeds.json
    score: float              # composite 0..1
    excluded_reason: str | None

    def as_row(self) -> tuple:
        return (
            self.wallet, self.trades_total, self.markets_total, self.resolved_positions,
            self.hit_rate, self.avg_edge_bps, self.avg_notional, self.total_pnl_usd,
            json.dumps(self.categories), self.persistence, self.best_seed_rank,
            self.score, self.excluded_reason, int(time.time()),
        )


# ----------------------------- DB -----------------------------
def ensure_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 30s timeout (vs Python's 5s default): under WAL the only thing that blocks a
    # writer is another writer. The 6h ranker re-scores thousands of wallets while the
    # parallel poll writes signals — at 5s the ranker errored ("database is locked").
    # 30s lets it wait its turn instead of failing.
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS leaders (
        wallet TEXT PRIMARY KEY,
        trades_total INTEGER,
        markets_total INTEGER,
        resolved_positions INTEGER,
        hit_rate REAL,
        avg_edge_bps REAL,
        avg_notional REAL,
        total_pnl_usd REAL,
        categories TEXT,
        persistence INTEGER,
        best_seed_rank INTEGER,
        score REAL,
        excluded_reason TEXT,
        updated_at INTEGER
      );
      CREATE INDEX IF NOT EXISTS idx_leaders_score ON leaders(score DESC);

      CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        leader_wallet TEXT NOT NULL,
        market TEXT NOT NULL,
        outcome TEXT,
        side TEXT,
        leader_price REAL,
        leader_size REAL,
        leader_ts INTEGER,
        observed_ts INTEGER,
        our_hypo_price REAL,
        our_hypo_slippage_bps REAL,
        liquidity_bucket TEXT,
        latency_bucket TEXT,
        filter_status TEXT,
        category TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_signals_observed ON signals(observed_ts DESC);
      CREATE INDEX IF NOT EXISTS idx_signals_leader   ON signals(leader_wallet);

      CREATE TABLE IF NOT EXISTS wallet_cursors (
        wallet TEXT PRIMARY KEY,
        last_trade_ts INTEGER
      );
    """)
    conn.commit()
    return conn


# ----------------------------- Analysis -----------------------------
def _parse_position(p: dict) -> dict:
    """Best-effort parse of Polymarket /positions response row.
    Different field names have been observed — we try all of them.
    """
    def f(*keys, default=0.0):
        for k in keys:
            v = p.get(k)
            if v is not None:
                try: return float(v)
                except (TypeError, ValueError): continue
        return default
    return {
        "market": str(p.get("conditionId") or p.get("market") or ""),
        "asset":  str(p.get("asset") or p.get("tokenId") or ""),
        "size":   f("size", "shares"),
        "avg_price": f("avgPrice", "averagePrice"),
        "current_value": f("currentValue", "value"),
        "cash_pnl":    f("cashPnl", "realizedPnl"),           # locked-in gains from partial sells
        "initial_value": f("initialValue", "costBasis"),
        "redeemable": bool(p.get("redeemable") or False),      # true if market resolved & wallet can redeem
    }


def _analyze(wallet: str, trades: list[Trade], positions: list[dict],
             value: dict | None, seed_meta: dict) -> LeaderStats:
    """Skill-score a wallet.

    IMPORTANT: we do NOT try to reconstruct PnL from trade history. Polymarket
    auto-redeems winning positions at resolution, and those payouts appear in
    neither /trades nor /positions once cleared — so any "PnL = sell - buy"
    calculation will systematically misclassify held-to-resolution winners as
    massive losers. Instead, we trust Polymarket's own leaderboard (encoded as
    seed_meta.persistence and seed_meta.best_rank) as the skill signal, and use
    trade data only to verify the wallet is alive, active, and trading real money.

    The `/value` endpoint is used opportunistically for display-only PnL.
    """
    import time as _time
    now = int(_time.time())

    if not trades:
        return LeaderStats(
            wallet=wallet, trades_total=0, markets_total=0, resolved_positions=0,
            hit_rate=0, avg_edge_bps=0, avg_notional=0, total_pnl_usd=0,
            categories=seed_meta.get("categories", []),
            persistence=seed_meta.get("persistence", 0),
            best_seed_rank=seed_meta.get("best_rank", 999),
            score=0, excluded_reason="no_trades",
        )

    # --- trade-derived activity signals (reliable) ---
    markets = {t.market for t in trades if t.market}
    last_trade_ts = max(t.timestamp for t in trades)
    days_since_last = (now - last_trade_ts) / 86400
    total_volume = sum(t.notional for t in trades)
    avg_notional = total_volume / len(trades)

    # --- position-derived resolved count (for display only) ---
    parsed = [_parse_position(p) for p in positions]
    open_markets = {p["market"] for p in parsed if p["size"] > 0.01 and not p["redeemable"] and p["market"]}
    # "closed" markets = traded but not currently open. Includes sold-out, redeemed, and already-cleared.
    closed_markets = markets - open_markets

    # --- display-only PnL from /value endpoint if available ---
    # Polymarket sometimes exposes total realized PnL here; falls back to 0 silently.
    total_pnl_usd = 0.0
    if value:
        for key in ("realizedPnl", "cashPnl", "totalPnl", "pnl"):
            v = value.get(key)
            if v is not None:
                try:
                    total_pnl_usd = float(v); break
                except (TypeError, ValueError):
                    continue

    # --- exclusion gates ---
    excluded = None
    if len(markets) < MIN_MARKETS:
        excluded = f"low_markets:{len(markets)}"
    elif avg_notional < MIN_AVG_NOTIONAL_USD:
        excluded = f"low_notional:${avg_notional:.0f}"
    elif days_since_last > MAX_DAYS_INACTIVE:
        excluded = f"inactive:{days_since_last:.0f}d"

    # --- composite score (leaderboard-trust based) ---
    score = 0.0
    if excluded is None:
        # 1. Best leaderboard rank (the strongest skill signal Polymarket gives us)
        best_rank = seed_meta.get("best_rank", 999)
        top_rank_bonus = max(0.0, (50 - min(best_rank, 50)) / 50)  # #1→1.0, #50→0.0
        # 2. Persistence across multiple leaderboard snapshots (weekly + monthly + all-time + category breakdowns)
        persist_bonus = min(1.0, seed_meta.get("persistence", 0) / 5)
        # 3. Recent activity — very-recent traders are more actionable
        recent_bonus = 1.0 if days_since_last <= 7 else max(0.0, 1 - (days_since_last - 7) / RECENT_WINDOW_DAYS)
        # 4. Volume — real money at stake. $10M lifetime volume saturates.
        vol_bonus = min(1.0, math.log10(max(1, total_volume)) / 7)
        # 5. Market breadth — 40+ distinct markets saturates
        breadth_bonus = min(1.0, len(markets) / 40)

        score = (
            0.40 * top_rank_bonus
            + 0.25 * persist_bonus
            + 0.15 * recent_bonus
            + 0.12 * vol_bonus
            + 0.08 * breadth_bonus
        )

    # For dashboard compatibility, pack derived activity into hit_rate/edge fields
    # (they're really just display placeholders now, not true hit rate / edge).
    return LeaderStats(
        wallet=wallet,
        trades_total=len(trades),
        markets_total=len(markets),
        resolved_positions=len(closed_markets),
        hit_rate=0.0,                           # unreliable without resolution data
        avg_edge_bps=0.0,                       # unreliable without resolution data
        avg_notional=avg_notional,
        total_pnl_usd=total_pnl_usd,            # from /value if available, else 0
        categories=seed_meta.get("categories", []),
        persistence=seed_meta.get("persistence", 0),
        best_seed_rank=seed_meta.get("best_rank", 999),
        score=score,
        excluded_reason=excluded,
    )


# ----------------------------- Main entry -----------------------------
def load_seeds() -> dict[str, dict]:
    return json.loads(SEEDS_PATH.read_text())


def _rank_one(client: PolyClient, wallet: str, seed_meta: dict) -> LeaderStats | None:
    """Fetch + analyze a single wallet (runs in a worker thread). Returns None if
    the trade fetch fails so one bad wallet never aborts the run. Pure w.r.t. the
    DB — the caller does the (sequential) SQLite write."""
    try:
        trades = client.get_all_trades(wallet, page_size=500, max_pages=10)
    except Exception as e:
        # 408/timeouts on deep pagination are transient — warn (not exception,
        # which would flood Sentry with tracebacks) and skip this wallet.
        log.warning("fetch trades failed for %s: %s", wallet, e)
        return None
    try:
        positions = client.get_positions(wallet)
    except Exception as e:
        log.warning("fetch positions failed for %s: %s", wallet, e)
        positions = []
    try:
        value = client.get_value(wallet)  # returns None on failure
    except Exception:
        value = None
    return _analyze(wallet, trades, positions, value, seed_meta)


def rank_seeds(limit: int | None = None, dry_run: bool = False) -> list[LeaderStats]:
    seeds = load_seeds()
    wallets = list(seeds.keys())
    if limit:
        wallets = wallets[:limit]

    log.info("ranking %d seed wallets (parallel, %d workers)", len(wallets), RANK_WORKERS)
    conn = ensure_db()
    results: list[LeaderStats] = []

    # Shared thread-safe client with a pool sized for the worker count.
    limits = httpx.Limits(max_connections=RANK_WORKERS * 2,
                          max_keepalive_connections=RANK_WORKERS)
    done = 0
    total = len(wallets)
    with PolyClient(limits=limits) as client, \
         ThreadPoolExecutor(max_workers=RANK_WORKERS) as ex:
        futs = {ex.submit(_rank_one, client, w, seeds[w]): w for w in wallets}
        for fut in as_completed(futs):
            done += 1
            try:
                stats = fut.result()
            except Exception as e:
                log.warning("rank worker failed for %s: %s", futs[fut], e)
                continue
            if stats is None:
                continue
            results.append(stats)
            # SQLite is single-writer — do the write here, in the main thread.
            if not dry_run:
                conn.execute(
                    """INSERT OR REPLACE INTO leaders VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    stats.as_row(),
                )
                conn.commit()
            if done % 200 == 0 or done == total:
                log.info("ranked %d/%d seeds", done, total)

    conn.close()
    results.sort(key=lambda s: s.score, reverse=True)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="rank only first N seeds (dev)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = rank_seeds(limit=args.limit, dry_run=args.dry_run)
    qualifying = [s for s in out if s.excluded_reason is None]
    top = qualifying[:20]
    print(f"\n=== Top {len(top)} qualifying leaders ===")
    print(f"{'wallet':<44} {'score':>6} {'best_rank':>10} {'pers':>5} {'trades':>7} "
          f"{'markets':>8} {'avg_$':>8} {'categories'}")
    for s in top:
        print(f"{s.wallet:<44} {s.score:>6.3f} #{s.best_seed_rank:<9d} "
              f"{s.persistence:>5d} {s.trades_total:>7d} {s.markets_total:>8d} "
              f"{s.avg_notional:>8.0f}  {','.join(s.categories)}")
    excluded = [s for s in out if s.excluded_reason]
    print(f"\n{len(qualifying)}/{len(out)} wallets qualified, {len(excluded)} excluded")
    if excluded:
        from collections import Counter
        reasons = Counter(s.excluded_reason.split(":")[0] for s in excluded)
        print(f"Exclusion reasons: {dict(reasons)}")
    print("\nNote: score = weighted combo of leaderboard rank (40%) + cross-list persistence (25%)")
    print("      + recent activity (15%) + trade volume (12%) + market breadth (8%).")
    print("      PnL/hit-rate are NOT computed — Polymarket's leaderboard already reflects those.")
