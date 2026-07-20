"""Paper book — virtual bankroll that opens positions on 'copy' signals.

Starts with $1000. Sizes positions using fractional Kelly based on signal
strength, subject to per-trade and total-exposure caps.

Position lifecycle:
  1. OPEN when a 'copy' signal fires: record entry price, size, cost basis.
  2. (later) A background task checks /markets/<conditionId> for resolution.
  3. CLOSE on resolution: credit payout ($1 * shares if winning side, $0 otherwise).
     Update bankroll.

This is paper-only. No real orders are placed, no USDC moves. All numbers are
stored in SQLite.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import notify

log = logging.getLogger(__name__)

TRADE_LOG_PATH = Path(__file__).parent / "data" / "trade_log.jsonl"


def _log_trade(event: str, data: dict):
    """Append a trade event to the persistent JSONL log file.

    Every open, close, and skip gets logged with full context for later analysis.
    File: data/trade_log.jsonl — one JSON object per line, never overwritten.
    """
    entry = {
        "ts": int(time.time()),
        "ts_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        **data,
    }
    try:
        TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADE_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.warning("trade log write failed: %s", e)

# ---- Load from config ----
from config import cfg

STARTING_BANKROLL_USD = cfg["sizing"]["starting_bankroll"]
BASE_RISK_FRAC = cfg["sizing"]["base_risk_frac"]
MAX_PER_TRADE_FRAC = cfg["sizing"]["max_per_trade_frac"]
MAX_TOTAL_EXPOSURE_FRAC = cfg["sizing"]["max_total_exposure"]
MIN_TRADE_USD = cfg["sizing"]["min_trade_usd"]
CONVICTION_MULTIPLIER = cfg["sizing"]["conviction_multiplier"]

MAX_RESOLUTION_HOURS = cfg["resolution"]["max_hours"]
MIN_RESOLUTION_HOURS = cfg["resolution"].get("min_hours", 0)
DUPLICATE_COOLDOWN_HOURS = cfg.get("duplicate_cooldown_hours", 6)
MAX_ENTRY_PRICE = cfg["entry_price"]["max"]
MIN_ENTRY_PRICE = cfg["entry_price"]["min"]
BLOCKED_CATEGORIES = set(cfg["categories"]["blocked"])
BLOCKED_OUTCOMES   = set(o.lower() for o in cfg.get("outcomes", {}).get("blocked", []))
BLOCK_SPREADS      = cfg.get("outcomes", {}).get("block_spreads", True)
BLOCK_EXACT_SCORE  = cfg.get("outcomes", {}).get("block_exact_score", True)
BLOCK_HALFTIME     = cfg.get("outcomes", {}).get("block_halftime", True)
# Spread/handicap markets: title starts with "Spread:"/"Handicap:" or carries a
# signed line like "(-1.5)" / "(+2.5)". The outcome label is just a team name, so
# the title is the reliable signal.
_SPREAD_LINE_RE = re.compile(r"\([+-]\d+(?:\.\d+)?\)")


def _is_spread_market(title: str) -> bool:
    t = (title or "").lower()
    return ("spread" in t) or ("handicap" in t) or bool(_SPREAD_LINE_RE.search(title or ""))


def _is_exact_score_market(title: str) -> bool:
    t = (title or "").lower()
    return ("exact score" in t) or ("correct score" in t)


def _is_halftime_market(title: str) -> bool:
    t = (title or "").lower()
    return ("halftime" in t) or ("half time" in t) or ("at half" in t) \
        or ("1st half" in t) or ("first half" in t)
MAX_POSITIONS_PER_EVENT = cfg["concentration"]["max_positions_per_event"]
MAX_POSITIONS_PER_GAME  = cfg["concentration"].get("max_positions_per_game", 1)
BLOCK_OPPOSING_SIDES    = cfg["concentration"].get("block_opposing_sides", True)
STRATEGY_VERSION = cfg.get("strategy_version", "v0")

# Words that are never team/competitor names — stripped before deriving a game key.
_GAME_STOPWORDS = {
    "will", "win", "wins", "won", "draw", "drawn", "end", "ends", "ending", "in", "a", "an",
    "the", "vs", "v", "score", "exact", "spread", "handicap", "map", "total", "over", "under",
    "on", "to", "of", "and", "or", "game", "match", "result", "results", "who", "which",
    "between", "no", "yes", "any", "other", "point", "points", "half", "quarter", "set",
    "moneyline", "be", "first", "next", "than", "more", "less", "at", "by", "with", "vs.",
}


def _game_signature(title: str, end_date: str = "") -> tuple[frozenset, str]:
    """Derive (team-token set, date) identifying the real-world game a market belongs to.

    Two markets on the same match share ≥1 team token AND the same date — e.g.
    "Will Austria win on 2026-06-17?" and "Will Austria vs. Jordan end in a draw?"
    both yield 'austria' + date, so they collide even with different condition_ids.
    Returns (empty set, "") when no teams can be extracted (caller must not over-block).
    """
    import re
    t = (title or "").lower()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", t)
    date = m.group(1) if m else (end_date[:10] if end_date else "")
    # Strip a leading market-type label before the colon ("Counter-Strike:", "Exact Score:",
    # "Map Handicap:") — otherwise the sport name itself would false-match unrelated games.
    t = re.sub(r"^[\w\s.-]{1,18}:\s*", "", t)
    t = re.sub(r"[^a-z\s]", " ", t)          # drop digits/punctuation (score numbers, '?')
    tokens = {w for w in t.split() if len(w) > 2 and w not in _GAME_STOPWORDS}
    return frozenset(tokens), date


def _strength_multiplier(strength: int) -> float:
    """Flat sizing — conviction count doesn't increase bet size."""
    if strength <= 1:
        return 1.0
    return CONVICTION_MULTIPLIER


def ensure_paper_tables(conn: sqlite3.Connection):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS paper_bankroll (
        id INTEGER PRIMARY KEY CHECK (id=1),
        cash_usd REAL NOT NULL,
        updated_at INTEGER
      );

      CREATE TABLE IF NOT EXISTS paper_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        market TEXT NOT NULL,
        market_title TEXT DEFAULT '',
        outcome TEXT,
        side TEXT NOT NULL,
        entry_price REAL NOT NULL,
        shares REAL NOT NULL,
        cost_usd REAL NOT NULL,
        signal_strength INTEGER,
        opened_ts INTEGER NOT NULL,
        closed_ts INTEGER,
        close_price REAL,
        payout_usd REAL,
        pnl_usd REAL,
        status TEXT NOT NULL DEFAULT 'open'
      );
      CREATE INDEX IF NOT EXISTS idx_pos_status ON paper_positions(status);
      CREATE INDEX IF NOT EXISTS idx_pos_market ON paper_positions(market);

      CREATE TABLE IF NOT EXISTS paper_bankroll_history (
        ts INTEGER NOT NULL,
        cash_usd REAL NOT NULL,
        open_exposure_usd REAL NOT NULL,
        equity_usd REAL NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_bh_ts ON paper_bankroll_history(ts);
    """)
    # Seed bankroll row separately since executescript doesn't take params
    conn.execute(
        "INSERT OR IGNORE INTO paper_bankroll(id, cash_usd, updated_at) VALUES(1, ?, ?)",
        (STARTING_BANKROLL_USD, int(time.time())),
    )
    conn.commit()


def get_cash(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT cash_usd FROM paper_bankroll WHERE id=1").fetchone()
    return row[0] if row else STARTING_BANKROLL_USD


def set_cash(conn: sqlite3.Connection, new_cash: float):
    conn.execute(
        "UPDATE paper_bankroll SET cash_usd=?, updated_at=? WHERE id=1",
        (new_cash, int(time.time())),
    )


def total_open_exposure(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM paper_positions WHERE status='open'"
    ).fetchone()
    return float(row[0] or 0.0)


def current_equity(conn: sqlite3.Connection) -> dict:
    """Equity = cash + sum(open_positions_cost). Note: doesn't mark-to-market."""
    cash = get_cash(conn)
    exp = total_open_exposure(conn)
    return {"cash": cash, "exposure": exp, "equity": cash + exp}


def compute_size_usd(
    bankroll_cash: float,
    bankroll_total: float,
    current_exposure: float,
    signal_strength: int,
) -> tuple[float, str]:
    """Return (size_in_usd, reason). size=0 means skip.

    Uses the TOTAL bankroll (cash + open exposure) to compute the percentage,
    so a hot streak of open positions doesn't shrink subsequent sizing.
    """
    if current_exposure >= MAX_TOTAL_EXPOSURE_FRAC * bankroll_total:
        return 0.0, f"exposure_cap:${current_exposure:.0f}"

    mult = _strength_multiplier(signal_strength)
    target = BASE_RISK_FRAC * mult * bankroll_total
    target = min(target, MAX_PER_TRADE_FRAC * bankroll_total)
    target = min(target, bankroll_cash)  # can't spend more than we have
    if target < MIN_TRADE_USD:
        return 0.0, f"below_min:${target:.2f}"
    return round(target, 2), f"kelly:strength={signal_strength},mult={mult}"


MAX_SOLO_POSITIONS = 3  # max ×1 (solo elite) open positions at once


def open_paper_position(
    conn: sqlite3.Connection,
    signal_id: int,
    market: str,
    outcome: str,
    side: str,
    our_hypo_price: float,
    signal_strength: int,
    market_title: str = "",
    category: str = "",
    end_date: str = "",
    market_slug: str = "",
    event_id: str = "",
    game_start: str = "",
    weighted_conviction: float = 0.0,
) -> dict:
    """Open a paper position if sizing allows. Returns decision dict.

    Skips if:
      - price outside allowed range
      - blocked category
      - already in this market (any side)
      - too many positions on same event
      - solo elite cap
      - exposure cap reached
      - market resolves outside MAX_RESOLUTION_HOURS window
    """
    if our_hypo_price <= 0 or our_hypo_price >= 1:
        return {"opened": False, "reason": f"bad_price:{our_hypo_price}"}

    # Risk/reward filter: don't buy near-certainties or extreme longshots
    if our_hypo_price >= MAX_ENTRY_PRICE:
        potential_return = ((1.0 / our_hypo_price) - 1) * 100
        reason = f"price_too_high:{our_hypo_price:.2f}(only+{potential_return:.0f}%return)"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title, "category": category,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}
    if our_hypo_price <= MIN_ENTRY_PRICE:
        reason = f"price_too_low:{our_hypo_price:.2f}(longshot)"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title, "category": category,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}

    # Category filter
    if category and category.lower() in BLOCKED_CATEGORIES:
        reason = f"blocked_category:{category}"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}

    # Outcome type filter: block O/U totals (Over/Under) — structurally unprofitable
    if outcome and BLOCKED_OUTCOMES and outcome.lower() in BLOCKED_OUTCOMES:
        reason = f"blocked_outcome:{outcome}"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}

    # Spread/handicap filter — "win by N.5" markets are structurally unprofitable
    # (-$2380 all-time, 75% of all profit). Our edge is "who wins", not the margin.
    if BLOCK_SPREADS and _is_spread_market(market_title):
        reason = "blocked_spread"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title, "category": category,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}

    # Exact-score filter — predicting the precise scoreline is unrelated to "who wins"
    # (-$419 all-time, -$293 in last 72h). Same class as spreads/O-U.
    if BLOCK_EXACT_SCORE and _is_exact_score_market(market_title):
        reason = "blocked_exact_score"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title, "category": category,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}

    # Halftime filter — leading/drawing at the 45' mark is not "who wins" (breakeven
    # all-time but fragile). Precautionary block to keep the strategy pure "who wins".
    if BLOCK_HALFTIME and _is_halftime_market(market_title):
        reason = "blocked_halftime"
        _log_trade("SKIP", {"reason": reason, "market_title": market_title, "category": category,
                            "entry_price": our_hypo_price, "signal_strength": signal_strength})
        return {"opened": False, "reason": reason}

    # Resolution window check
    if MAX_RESOLUTION_HOURS > 0 and end_date:
        from datetime import datetime, timezone
        try:
            # Parse ISO date — handle both "2026-04-17T00:00:00Z" and "2026-04-17"
            ed = end_date.replace("Z", "+00:00")
            if "T" in ed:
                dt = datetime.fromisoformat(ed)
            else:
                dt = datetime.fromisoformat(ed + "T23:59:59+00:00")
            now = datetime.now(timezone.utc)
            hours_until = (dt - now).total_seconds() / 3600
            if hours_until > MAX_RESOLUTION_HOURS:
                return {"opened": False, "reason": f"too_far:{hours_until:.0f}h>{MAX_RESOLUTION_HOURS}h"}
            if MIN_RESOLUTION_HOURS > 0 and 0 < hours_until < MIN_RESOLUTION_HOURS:
                return {"opened": False, "reason": f"resolves_too_soon:{hours_until:.1f}h<{MIN_RESOLUTION_HOURS}h"}
            if hours_until < -24:
                # Already past — likely already resolved, skip
                return {"opened": False, "reason": f"already_past:{hours_until:.0f}h"}
        except (ValueError, TypeError):
            pass  # unparseable date — allow the trade

    # Block any bet on a market we already have a position on (prevents opposite-side hedging)
    existing = conn.execute(
        """SELECT id, outcome, side FROM paper_positions
            WHERE status='open' AND market=?""",
        (market,),
    ).fetchone()
    if existing:
        return {"opened": False, "reason": f"already_in_market:pos_{existing[0]}({existing[1]},{existing[2]})"}

    # Block opposing outcome on same event — prevents betting both sides of same game
    # e.g. if we're long "Team A wins", block "Team B wins" on the same event_id
    if BLOCK_OPPOSING_SIDES and event_id:
        opposing = conn.execute(
            """SELECT id, outcome, market FROM paper_positions
                WHERE status='open' AND event_id=? AND market != ?""",
            (event_id, market),
        ).fetchone()
        if opposing:
            return {"opened": False, "reason": f"opposing_side_blocked:event_{event_id[:12]}({opposing[1]})"}

    # Also block by title similarity for same-game markets without shared event_id
    # e.g. "Will France win?" and "Will France NOT win?" same game different market
    if BLOCK_OPPOSING_SIDES and market_title:
        import re
        # Extract base game name: strip "Will X win", "O/U", "Spread" prefixes
        base = re.sub(r'^(will |spread:|o/u \d[\d.]*\s*)', '', market_title.lower()).strip()
        base = re.sub(r'\?.*$', '', base).strip()  # strip trailing ?
        if len(base) > 8:  # only apply if we have a meaningful base name
            same_game = conn.execute(
                """SELECT id, market_title, outcome FROM paper_positions
                    WHERE status='open' AND market != ?
                      AND LOWER(market_title) LIKE ?""",
                (market, f"%{base[:25]}%"),
            ).fetchone()
            if same_game:
                return {"opened": False, "reason": f"same_game_title_blocked:{base[:20]}(pos_{same_game[0]})"}

    # Duplicate cooldown: skip if same market+outcome+side closed within N hours
    if DUPLICATE_COOLDOWN_HOURS > 0:
        cooldown_cutoff = int(time.time()) - int(DUPLICATE_COOLDOWN_HOURS * 3600)
        recent = conn.execute(
            """SELECT id FROM paper_positions
                WHERE market=? AND outcome=? AND side=?
                  AND status IN ('closed','exited') AND closed_ts >= ?""",
            (market, outcome, side, cooldown_cutoff),
        ).fetchone()
        if recent:
            return {"opened": False, "reason": f"cooldown:same_bet_closed_within_{DUPLICATE_COOLDOWN_HOURS:.0f}h"}

    # Game-level re-entry lockout (direction-agnostic): once ANY position on this
    # real-world game has closed/exited, don't re-enter the same game for N hours —
    # regardless of market, outcome, or side. Stops the exit-then-rebuy / direction-flip
    # churn (e.g. BUY "Germany win", exit, then SELL "Germany win" 30 min later) that the
    # side-keyed cooldown above misses. Matches by _game_signature (team token + date).
    if DUPLICATE_COOLDOWN_HOURS > 0 and market_title:
        cand_tokens, cand_date = _game_signature(market_title, end_date)
        if cand_tokens and cand_date:
            cooldown_cutoff = int(time.time()) - int(DUPLICATE_COOLDOWN_HOURS * 3600)
            for oid, otitle, oend in conn.execute(
                """SELECT id, market_title, end_date FROM paper_positions
                    WHERE status IN ('closed','exited') AND closed_ts >= ?""",
                (cooldown_cutoff,),
            ).fetchall():
                o_tokens, o_date = _game_signature(otitle or "", oend or "")
                if o_date == cand_date and (cand_tokens & o_tokens):
                    shared = ",".join(sorted(cand_tokens & o_tokens))
                    return {"opened": False,
                            "reason": f"game_cooldown:{shared}@{cand_date}_within_{DUPLICATE_COOLDOWN_HOURS:.0f}h(pos_{oid})"}

    # Per-event concentration cap (e.g., max 2 bets on Tottenham vs Brighton)
    if event_id and MAX_POSITIONS_PER_EVENT > 0:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open' AND event_id=?",
            (event_id,),
        ).fetchone()[0]
        if event_count >= MAX_POSITIONS_PER_EVENT:
            return {"opened": False, "reason": f"event_cap:{event_count}/{MAX_POSITIONS_PER_EVENT}"}

    # Game-level backstop — catches correlated same-game markets even when event_id is
    # missing or differs (e.g. "Austria win" + "Austria vs Jordan draw"). Two markets
    # collide if they share a team token AND the same date. This is the real fix for the
    # World-Cup correlation problem: one bet per real-world game, period.
    if MAX_POSITIONS_PER_GAME > 0 and market_title:
        cand_tokens, cand_date = _game_signature(market_title, end_date)
        if cand_tokens and cand_date:  # only enforce when we confidently identified the game
            for oid, otitle, oend in conn.execute(
                "SELECT id, market_title, end_date FROM paper_positions WHERE status='open'"
            ).fetchall():
                o_tokens, o_date = _game_signature(otitle or "", oend or "")
                if o_date == cand_date and (cand_tokens & o_tokens):
                    shared = ",".join(sorted(cand_tokens & o_tokens))
                    return {"opened": False,
                            "reason": f"same_game_blocked:{shared}@{cand_date}(pos_{oid})"}

    # Per-game slug cap — prevents betting moneyline + O/U on the same real-world game.
    # Strips line suffixes like "-total-8pt5", "-spread-1pt5" to get the base game slug.
    if market_slug and MAX_POSITIONS_PER_GAME > 0:
        import re
        base_slug = re.sub(r'-(total|spread|over|under|half|quarter|map|set)-.*$', '', market_slug)
        if base_slug != market_slug:  # only apply when a suffix was actually stripped
            game_count = conn.execute(
                """SELECT COUNT(*) FROM paper_positions
                    WHERE status='open' AND market_slug LIKE ?""",
                (base_slug + '%',),
            ).fetchone()[0]
            if game_count >= MAX_POSITIONS_PER_GAME:
                return {"opened": False, "reason": f"game_slug_cap:{base_slug}({game_count}/{MAX_POSITIONS_PER_GAME})"}

    # Solo elite cap
    if signal_strength == 1:
        solo_count = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open' AND signal_strength=1"
        ).fetchone()[0]
        if solo_count >= MAX_SOLO_POSITIONS:
            return {"opened": False, "reason": f"solo_cap:{solo_count}/{MAX_SOLO_POSITIONS}"}

    eq = current_equity(conn)
    size_usd, reason = compute_size_usd(
        eq["cash"], eq["equity"], eq["exposure"], signal_strength
    )
    if size_usd <= 0:
        return {"opened": False, "reason": reason, "size_usd": 0}

    shares = size_usd / our_hypo_price
    now = int(time.time())

    # migrate: add columns if missing (existing DBs)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_positions)")}
    for col, ddl in [("market_title", "TEXT DEFAULT ''"), ("end_date", "TEXT DEFAULT ''"),
                     ("category", "TEXT DEFAULT ''"), ("market_slug", "TEXT DEFAULT ''"),
                     ("event_id", "TEXT DEFAULT ''"), ("strategy_version", "TEXT DEFAULT ''"),
                     ("weighted_conviction", "REAL DEFAULT 0"), ("game_start_ts", "INTEGER DEFAULT 0")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {ddl}")

    # Parse game start time -> unix ts. in_play = opened after this (bet on a live game).
    game_start_ts = 0
    if game_start:
        try:
            from datetime import datetime, timezone
            gs = game_start.rstrip("Z").split(".")[0]
            if "T" not in gs:
                gs += "T00:00:00"
            game_start_ts = int(datetime.fromisoformat(gs).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            game_start_ts = 0

    cur = conn.execute(
        """INSERT INTO paper_positions
           (signal_id, market, market_title, market_slug, category, outcome, side, entry_price, shares, cost_usd,
            signal_strength, opened_ts, end_date, event_id, strategy_version, weighted_conviction, game_start_ts, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
        (signal_id, market, market_title, market_slug, category, outcome, side, our_hypo_price, shares, size_usd,
         signal_strength, now, end_date, event_id, STRATEGY_VERSION, weighted_conviction, game_start_ts),
    )
    pos_id = cur.lastrowid

    set_cash(conn, eq["cash"] - size_usd)
    _snapshot_bankroll(conn)
    conn.commit()

    log.info("paper OPEN #%d: [%s] %s %s '%s' @ %.3f $%.2f (×%d)",
             pos_id, category or "?", side, outcome, market_title[:40], our_hypo_price, size_usd, signal_strength)
    notify.alert_position_opened(
        market_title=market_title, side=side, outcome=outcome,
        entry_price=our_hypo_price, size_usd=size_usd,
        conviction=signal_strength, weighted_score=weighted_conviction,
        category=category or "?",
    )
    _log_trade("OPEN", {
        "position_id": pos_id, "market": market, "market_title": market_title,
        "market_slug": market_slug, "category": category, "outcome": outcome,
        "side": side, "entry_price": our_hypo_price, "cost_usd": round(size_usd, 2),
        "shares": round(shares, 4), "signal_strength": signal_strength,
        "end_date": end_date, "event_id": event_id,
        "strategy_version": STRATEGY_VERSION, "weighted_conviction": weighted_conviction,
    })
    return {
        "opened": True, "position_id": pos_id, "size_usd": size_usd,
        "shares": shares, "reason": reason,
    }


def close_paper_position(
    conn: sqlite3.Connection,
    position_id: int,
    close_price: float,
) -> dict:
    """Close at close_price (0.0 or 1.0 for resolved binary markets, or mid for sold).

    payout = shares * close_price. For BUY positions this means winning side → $1.
    For SELL positions (shorting Yes, e.g. buying No), the payout still equals
    shares * close_price, but we'd want to validate side semantics once we're
    actually mirroring those.
    """
    row = conn.execute(
        "SELECT shares, cost_usd, side FROM paper_positions WHERE id=? AND status='open'",
        (position_id,),
    ).fetchone()
    if not row:
        return {"closed": False, "reason": "not_found_or_already_closed"}

    shares, cost_usd, side = row
    payout = shares * close_price
    pnl = payout - cost_usd
    now = int(time.time())

    conn.execute(
        """UPDATE paper_positions
              SET status='closed', closed_ts=?, close_price=?, payout_usd=?, pnl_usd=?
            WHERE id=?""",
        (now, close_price, payout, pnl, position_id),
    )

    # Credit cash
    cash = get_cash(conn)
    set_cash(conn, cash + payout)
    _snapshot_bankroll(conn)
    conn.commit()

    log.info("paper CLOSE #%d: close=%.3f payout=$%.2f pnl=$%+.2f",
             position_id, close_price, payout, pnl)

    # Fetch full position context for the trade log and notifications
    full = conn.execute(
        """SELECT market, market_title, market_slug, category, outcome, side,
                  entry_price, signal_strength, opened_ts, end_date
             FROM paper_positions WHERE id=?""",
        (position_id,),
    ).fetchone()
    if full:
        notify.alert_position_closed(
            market_title=full[1], outcome=full[4],
            entry_price=full[6], close_price=close_price,
            pnl_usd=pnl, cost_usd=cost_usd,
        )
        result = "WON" if pnl > 0 else "LOST" if pnl < 0 else "BREAK_EVEN"
        _log_trade("CLOSE", {
            "position_id": position_id, "result": result,
            "market": full[0], "market_title": full[1], "market_slug": full[2],
            "category": full[3], "outcome": full[4], "side": full[5],
            "entry_price": full[6], "close_price": close_price,
            "cost_usd": round(cost_usd, 2), "payout_usd": round(payout, 2),
            "pnl_usd": round(pnl, 2), "signal_strength": full[7],
            "opened_ts": full[8], "end_date": full[9],
            "hold_seconds": now - full[8] if full[8] else 0,
        })

    return {"closed": True, "pnl_usd": pnl, "payout_usd": payout}


def _snapshot_bankroll(conn: sqlite3.Connection):
    """Record a bankroll history point — used by the dashboard chart."""
    eq = current_equity(conn)
    conn.execute(
        "INSERT INTO paper_bankroll_history(ts, cash_usd, open_exposure_usd, equity_usd) VALUES(?,?,?,?)",
        (int(time.time()), eq["cash"], eq["exposure"], eq["equity"]),
    )
