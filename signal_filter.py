"""Signal filter — decides whether to copy a trade based on conviction and quality.

Uses config.yaml for all thresholds and leader_weights for quality scoring.
Tags every decision with strategy_version for A/B comparison.

Shadow filters (no real bets, just tracking):
  shadow_min5      — would conviction >= 5 have blocked this?
  shadow_no_last2h — would blocking sports within 2h of resolve have blocked this?
"""
from __future__ import annotations

import logging
import sqlite3
import time

from config import cfg
from leader_weights import get_excluded_leaders, weighted_conviction_v2

log = logging.getLogger(__name__)

# Load from config
CONVICTION_WINDOW_HOURS = cfg["conviction"]["window_hours"]
CONVICTION_MIN = cfg["conviction"]["min_leaders"]
WEIGHTED_MIN_V2 = cfg["leader_weighting"]["weighted_min_v2"]

MIN_LIQUIDITY_USD = cfg["liquidity"]["min_usd"]
MAX_SLIPPAGE_BPS = cfg["liquidity"]["max_slippage_bps"]

# Change 5: skip leaders on a losing streak (last N copies all lost)
STREAK_BLOCK_ENABLED = cfg.get("streak_block", {}).get("enabled", True)
STREAK_BLOCK_LOSSES  = cfg.get("streak_block", {}).get("min_consecutive_losses", 3)


# Game-level conviction (sports): count leaders agreeing on one GAME's outcome across
# ANY market, not just the exact same market. Reuses the same team-token + stopword
# tokenizer as paper_book so "Germany win" and other Germany-game markets group together.
GAME_LEVEL_CONVICTION = cfg.get("conviction", {}).get("game_level", False)
_CONV_STOPWORDS = {
    "will", "win", "wins", "won", "draw", "drawn", "end", "ends", "ending", "in", "a", "an",
    "the", "vs", "v", "score", "exact", "spread", "handicap", "map", "total", "over", "under",
    "on", "to", "of", "and", "or", "game", "match", "result", "results", "who", "which",
    "between", "no", "yes", "any", "other", "point", "points", "half", "quarter", "set",
    "moneyline", "be", "first", "next", "than", "more", "less", "at", "by", "with", "vs.",
    "advance", "team", "both", "teams", "goals", "goal",
}


def _game_tokens(title: str) -> frozenset:
    import re
    t = (title or "").lower()
    t = re.sub(r"^[\w\s.-]{1,18}:\s*", "", t)   # strip "Counter-Strike:" / "Exact Score:" labels
    t = re.sub(r"[^a-z\s]", " ", t)
    return frozenset(w for w in t.split() if len(w) > 2 and w not in _CONV_STOPWORDS)


def recent_conviction_data(
    conn: sqlite3.Connection,
    market: str,
    side: str,
    excluded: set[str],
    outcome: str = "",
    market_title: str = "",
    category: str = "",
) -> list[dict]:
    """Return per-wallet conviction data in the lookback window.

    For SPORTS with game_level on: counts distinct leaders backing the SAME game
    (shared team token, within the 6h window) + SAME outcome + SAME side, across any
    market — so 4 leaders on one match's outcome via different bets = 4 conviction, and
    unrelated markets that merely share a country name (GDP, politics) are excluded by
    the sports + outcome + side constraints.
    Otherwise falls back to the strict (market, side) grouping.
    """
    cutoff = int(time.time()) - CONVICTION_WINDOW_HOURS * 3600

    cand_tokens = _game_tokens(market_title) if market_title else frozenset()
    if GAME_LEVEL_CONVICTION and category == "sports" and cand_tokens:
        rows = conn.execute(
            """SELECT leader_wallet, market_title, leader_size, observed_ts
                 FROM signals
                WHERE side = ? AND outcome = ? AND category = 'sports' AND observed_ts >= ?""",
            (side, outcome, cutoff),
        ).fetchall()
        per_leader: dict[str, dict] = {}
        for w, mt, sz, ts in rows:
            if w in excluded:
                continue
            if not (_game_tokens(mt or "") & cand_tokens):  # must share a team token = same game
                continue
            cur = per_leader.get(w)
            if cur is None:
                per_leader[w] = {"wallet": w, "size": float(sz or 0), "ts": int(ts), "_n": 1}
            else:
                cur["size"] += float(sz or 0); cur["_n"] += 1
                cur["ts"] = max(cur["ts"], int(ts))
        out = []
        for w, d in per_leader.items():
            d["size"] = d["size"] / max(d.pop("_n"), 1)
            out.append(d)
        return out

    rows = conn.execute(
        """SELECT leader_wallet,
                  COALESCE(AVG(leader_size), 0) AS avg_size,
                  MAX(observed_ts)              AS latest_ts
             FROM signals
            WHERE market = ? AND side = ? AND observed_ts >= ?
         GROUP BY leader_wallet""",
        (market, side, cutoff),
    ).fetchall()
    return [
        {"wallet": w, "size": float(s), "ts": int(ts)}
        for w, s, ts in rows
        if w not in excluded
    ]


def _leader_recent_results(conn: sqlite3.Connection, wallet: str, n: int = 3) -> list[bool]:
    """Return the last N copy results for a leader (True=win). Newest first."""
    rows = conn.execute(
        """SELECT pp.pnl_usd
             FROM paper_positions pp
             JOIN signals s ON pp.signal_id = s.id
            WHERE s.leader_wallet = ? AND pp.status IN ('closed', 'exited')
         ORDER BY pp.closed_ts DESC
            LIMIT ?""",
        (wallet, n),
    ).fetchall()
    return [r[0] > 0 for r in rows]


def _shadow_min5(conviction_n: int, weighted_score: float) -> int:
    """Shadow filter: would min_leaders=5 have allowed this copy? 1=yes, 0=no."""
    return 1 if conviction_n >= 5 and weighted_score >= WEIGHTED_MIN_V2 else 0


def _shadow_no_last2h(end_date: str, category: str) -> int:
    """Shadow filter: would blocking sports in last 2h before resolve have allowed this? 1=yes, 0=no."""
    if category not in ("sports", "crypto"):  # only applies to live-event categories
        return 1  # non-sports: always allowed
    if not end_date:
        return 1  # no end date known: allow
    try:
        from datetime import datetime, timezone
        # end_date can be ISO string like "2025-06-05T20:00:00Z"
        ed = end_date.rstrip("Z").split(".")[0]
        dt = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc)
        secs_to_resolve = (dt - datetime.now(timezone.utc)).total_seconds()
        if 0 < secs_to_resolve < 7200:  # within 2 hours
            return 0  # shadow filter would block
    except Exception:
        pass
    return 1


def classify(
    conn: sqlite3.Connection,
    leader_wallet: str,
    market: str,
    side: str,
    hypo_price: float,
    slippage_bps: float,
    liquidity_usd: float,
    profile: dict | None = None,
    leader_size: float = 0.0,
    end_date: str = "",
    category: str = "",
    market_title: str = "",
    outcome: str = "",
) -> dict:
    """Classify a trade signal. Returns decision dict with shadow filter flags."""

    strategy_version = cfg.get("strategy_version", "v0")

    def _result(conv_n, conv_wallets, strength, status, reason,
                weighted_score=0.0, wallet_weights=None,
                shadow_min5=0, shadow_no_last2h=0):
        return {
            "conviction_count": conv_n,
            "conviction_wallets": ",".join(conv_wallets) if isinstance(conv_wallets, list) else conv_wallets,
            "signal_strength": strength,
            "filter_status": status,
            "filter_reason": reason,
            "strategy_version": strategy_version,
            "weighted_conviction": weighted_score,
            "wallet_weights": wallet_weights or {},
            "shadow_min5": shadow_min5,
            "shadow_no_last2h": shadow_no_last2h,
        }

    # ---- Liquidity gate ----
    if liquidity_usd < MIN_LIQUIDITY_USD:
        return _result(0, [], 0, "observe_only", f"low_liquidity:{liquidity_usd:.0f}<{MIN_LIQUIDITY_USD}")
    if slippage_bps > MAX_SLIPPAGE_BPS:
        return _result(0, [], 0, "observe_only", f"high_slippage:{slippage_bps:.0f}>{MAX_SLIPPAGE_BPS}")

    # ---- Leader exclusion gate ----
    excluded = get_excluded_leaders(conn)
    if leader_wallet in excluded:
        return _result(0, [], 0, "observe_only", f"leader_excluded:{leader_wallet[:12]}")

    # ---- Conviction data (with exclusions applied) ----
    conv_items = recent_conviction_data(conn, market, side, excluded,
                                        outcome=outcome, market_title=market_title, category=category)

    existing = {item["wallet"] for item in conv_items}
    if leader_wallet not in existing:
        conv_items.append({
            "wallet": leader_wallet,
            "size": leader_size,
            "ts": int(time.time()),
        })

    conviction_n = len(conv_items)
    conviction_wallets = [item["wallet"] for item in conv_items]
    signal_strength = min(conviction_n, 10)

    # ---- Full weighted conviction: quality × time_decay × size ----
    weighted_score, wallet_weights = weighted_conviction_v2(conn, conv_items)

    # ---- Compute shadow flags regardless of final decision ----
    s_min5     = _shadow_min5(conviction_n, weighted_score)
    s_no_last2h = _shadow_no_last2h(end_date, category)

    # ---- Decision ----
    use_weighting = cfg["leader_weighting"]["enabled"]

    if use_weighting:
        if conviction_n >= CONVICTION_MIN and weighted_score >= WEIGHTED_MIN_V2:
            # ---- Change 5: block leaders on losing streak ----
            if STREAK_BLOCK_ENABLED:
                recent = _leader_recent_results(conn, leader_wallet, STREAK_BLOCK_LOSSES)
                if len(recent) >= STREAK_BLOCK_LOSSES and not any(recent):
                    log.info("streak_block: %s lost last %d copies — skipping",
                             leader_wallet[:12], STREAK_BLOCK_LOSSES)
                    return _result(conviction_n, conviction_wallets, signal_strength,
                                   "observe_only",
                                   f"streak_blocked:lost_last_{STREAK_BLOCK_LOSSES}",
                                   weighted_score, wallet_weights,
                                   shadow_min5=s_min5, shadow_no_last2h=s_no_last2h)

            return _result(conviction_n, conviction_wallets, signal_strength,
                           "copy",
                           f"conviction:{conviction_n},weighted_v2:{weighted_score:.2f}",
                           weighted_score, wallet_weights,
                           shadow_min5=s_min5, shadow_no_last2h=s_no_last2h)
        elif conviction_n >= CONVICTION_MIN:
            return _result(conviction_n, conviction_wallets, signal_strength,
                           "observe_only",
                           f"conviction_ok:{conviction_n}_low_score:{weighted_score:.2f}<{WEIGHTED_MIN_V2}",
                           weighted_score, wallet_weights,
                           shadow_min5=s_min5, shadow_no_last2h=s_no_last2h)
    else:
        if conviction_n >= CONVICTION_MIN:
            return _result(conviction_n, conviction_wallets, signal_strength,
                           "copy", f"conviction:{conviction_n}",
                           weighted_score, wallet_weights,
                           shadow_min5=s_min5, shadow_no_last2h=s_no_last2h)

    return _result(conviction_n, conviction_wallets, signal_strength,
                   "observe_only", f"below_conviction:{conviction_n}<{CONVICTION_MIN}",
                   weighted_score, wallet_weights,
                   shadow_min5=s_min5, shadow_no_last2h=s_no_last2h)
