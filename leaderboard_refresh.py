"""Leaderboard refresh — pulls fresh top wallets from Polymarket every hour.

Fetches top 50 by PnL across all categories and time periods from the
data-api /v1/leaderboard endpoint, merges new wallets into seeds.json,
and scores any new wallets via the ranker.

Runs as a loop in the worker (every REFRESH_INTERVAL seconds) or
standalone via: python leaderboard_refresh.py
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE = Path(__file__).parent
SEEDS_PATH = BASE / "data" / "seeds.json"
LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"

REFRESH_INTERVAL = 3600  # 1 hour

# Categories and periods to fetch
CATEGORIES = ["OVERALL", "SPORTS", "CRYPTO", "POLITICS", "CULTURE"]
PERIODS = ["WEEK", "MONTH", "ALL"]
CATEGORY_MAP = {
    "OVERALL": "all", "SPORTS": "sports", "CRYPTO": "crypto",
    "POLITICS": "politics", "CULTURE": "culture",
}
PERIOD_MAP = {"WEEK": "weekly", "MONTH": "monthly", "ALL": "all_time"}


def fetch_leaderboard(
    http: httpx.Client,
    category: str = "OVERALL",
    period: str = "WEEK",
    limit: int = 50,
) -> list[dict]:
    """Fetch one leaderboard page. Returns list of {proxyWallet, rank, pnl, vol, ...}."""
    try:
        r = http.get(LEADERBOARD_URL, params={
            "category": category,
            "timePeriod": period,
            "orderBy": "PNL",
            "limit": limit,
            "offset": 0,
        })
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        log.warning("leaderboard fetch failed for %s/%s: %s", category, period, e)
        return []


def refresh_seeds(http: httpx.Client) -> dict:
    """Pull all leaderboards and merge new wallets into seeds.json.
    Returns summary: {new_wallets: N, updated_wallets: N, total: N}.
    """
    # Load existing seeds
    if SEEDS_PATH.exists():
        seeds = json.loads(SEEDS_PATH.read_text())
    else:
        seeds = {}

    new_count = 0
    updated_count = 0

    for category in CATEGORIES:
        for period in PERIODS:
            entries = fetch_leaderboard(http, category, period)
            cat_key = CATEGORY_MAP.get(category, category.lower())
            tf_key = PERIOD_MAP.get(period, period.lower())

            for entry in entries:
                wallet = (entry.get("proxyWallet") or "").lower()
                if not wallet or not wallet.startswith("0x"):
                    continue
                rank = entry.get("rank")
                if isinstance(rank, str):
                    try:
                        rank = int(rank)
                    except ValueError:
                        rank = 999

                # Init or update seed entry
                is_new = wallet not in seeds
                s = seeds.setdefault(wallet, {
                    "appearances": [],
                    "categories": [],
                    "timeframes": [],
                    "best_rank": 999,
                    "persistence": 0,
                })

                # Check if this (cat, tf) appearance already exists
                existing = [
                    a for a in s["appearances"]
                    if a["category"] == cat_key and a["timeframe"] == tf_key
                ]
                if existing:
                    if rank < existing[0]["rank"]:
                        existing[0]["rank"] = rank
                        updated_count += 1
                else:
                    s["appearances"].append({
                        "category": cat_key, "timeframe": tf_key, "rank": rank,
                    })
                    if not is_new:
                        updated_count += 1

                # Recompute derived fields
                s["categories"] = sorted({a["category"] for a in s["appearances"]})
                s["timeframes"] = sorted({a["timeframe"] for a in s["appearances"]})
                s["best_rank"] = min(a["rank"] for a in s["appearances"])
                s["persistence"] = len({
                    (a["category"], a["timeframe"]) for a in s["appearances"]
                })

                if is_new:
                    new_count += 1

            time.sleep(0.15)  # polite

    # Sort by persistence desc, best rank asc
    ordered = dict(sorted(
        seeds.items(),
        key=lambda kv: (-kv[1]["persistence"], kv[1]["best_rank"]),
    ))

    SEEDS_PATH.write_text(json.dumps(ordered, indent=2))

    summary = {
        "new_wallets": new_count,
        "updated_wallets": updated_count,
        "total": len(ordered),
    }
    log.info("leaderboard refresh: %d new, %d updated, %d total seeds",
             new_count, updated_count, len(ordered))
    return summary


def refresh_once() -> dict:
    """One refresh pass."""
    http = httpx.Client(headers={"User-Agent": "copy-bot/0.1"}, timeout=10.0)
    try:
        return refresh_seeds(http)
    finally:
        http.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = refresh_once()
    print(f"\nLeaderboard refresh complete:")
    print(f"  New wallets:     {summary['new_wallets']}")
    print(f"  Updated wallets: {summary['updated_wallets']}")
    print(f"  Total seeds:     {summary['total']}")

    # Show top 10 by persistence
    seeds = json.loads(SEEDS_PATH.read_text())
    print(f"\nTop 10 by persistence:")
    for i, (addr, w) in enumerate(list(seeds.items())[:10], 1):
        print(f"  {i:2d}. {addr}  p={w['persistence']}  best=#{w['best_rank']}  cats={w['categories']}")
