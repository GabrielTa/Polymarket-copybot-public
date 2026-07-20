"""Wallet watcher + signal evaluator.

Polls tracked leader wallets every 15s for new trades. For each new trade:
  1. Fetches the live order book for that market.
  2. Simulates what our fill would have been if we submitted a mirror order now.
  3. Buckets the signal by liquidity tier and latency-since-leader-fill.
  4. Runs the signal_filter to classify 'copy' vs 'observe_only'.
  5. If 'copy', opens a paper position via paper_book.
  6. Writes the signal row to SQLite.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from poly_client import PolyClient, Trade, CLOB_API
from signal_filter import classify
from paper_book import ensure_paper_tables, open_paper_position
from shadow_book import ensure_shadow_table, open_shadow_position

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "copybot.db"


# ---- bucket helpers ----
def liquidity_bucket(total_liquidity_usd: float) -> str:
    if total_liquidity_usd >= 200_000:
        return ">200k"
    if total_liquidity_usd >= 50_000:
        return "50k-200k"
    return "<50k"


def latency_bucket(seconds: float) -> str:
    if seconds < 5:    return "<5s"
    if seconds < 15:   return "5-15s"
    if seconds < 60:   return "15-60s"
    return ">60s"


def categorize_market(market_meta: dict | None, title: str = "", slug: str = "", tags: list | None = None) -> str:
    """Category inference from market tags, title, slug, and structural patterns.

    The key insight: Polymarket sports markets follow predictable patterns like
    "Will X win on 2026-", "O/U 2.5", "Spread:", "X vs. Y". We match these
    patterns FIRST before falling back to keyword lists.
    """
    if not market_meta:
        market_meta = {}
    blob_parts = [slug.lower(), title.lower()]
    meta_tags = [t.lower() for t in (market_meta.get("tags") or tags or [])]
    blob_parts.extend(meta_tags)
    slug_val = (market_meta.get("slug") or market_meta.get("market_slug") or "").lower()
    blob_parts.append(slug_val)
    blob = " ".join(blob_parts)

    # Check CLOB/Gamma tags first — most reliable signal
    for t in meta_tags:
        if t in ("sports", "soccer", "basketball", "baseball", "hockey", "tennis",
                 "football", "mma", "boxing", "cricket", "golf", "nascar", "f1",
                 "nfl", "nba", "nhl", "mlb", "wnba", "epl", "la liga", "serie a",
                 "bundesliga", "ligue 1", "champions league", "europa league",
                 "europa conference league", "mls", "games"):
            return "sports"
        if t in ("crypto", "bitcoin", "ethereum", "defi", "blockchain"):
            return "crypto"
        if t in ("politics", "elections", "government", "geopolitics"):
            return "politics"
        if t in ("culture", "entertainment", "movies", "music", "awards"):
            return "culture"

    # Pattern matching — structural indicators that are ALWAYS sports
    import re
    sports_patterns = [
        r"will .+ win on 2\d{3}-",       # "Will X win on 2026-04-18?"
        r" vs\.? ",                        # "X vs Y" or "X vs. Y"
        r"o/u \d",                         # "O/U 2.5"
        r"spread:",                        # "Spread: X (-1.5)"
        r"both teams to score",
        r"end in a draw",
        r"\(-?\d+\.5\)",                   # "(-1.5)" or "(+2.5)" spread notation
    ]
    for pat in sports_patterns:
        if re.search(pat, blob):
            return "sports"

    # Keyword fallback for cases patterns miss
    sports_kw = ["nfl", "nba", "nhl", "mlb", "wnba", "ufc", "mma", "tennis", "atp", "wta",
                 "soccer", "epl", "premier league", "champions league", "la liga",
                 "serie a", "bundesliga", "ligue 1", "mls", "ncaa", "college", "bowl",
                 "grand slam", "wimbledon", "pga", "golf", "nascar", "f1",
                 "formula", "boxing", "cricket", "ipl",
                 "playoff", "finals", "world series", "super bowl", "stanley cup",
                 # Common team suffixes
                 " fc", " sc", " fk", " cf", " ac", " ssc", " rcd",
                 "united", "city fc", "rovers", "wanderers"]
    for kw in sports_kw:
        if kw in blob:
            return "sports"

    crypto_kw = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol",
                 "token", "defi", "blockchain", "altcoin", "memecoin", "doge",
                 "xrp", "bnb", "cardano", "polygon", "avalanche"]
    for kw in crypto_kw:
        if kw in blob:
            return "crypto"

    politics_kw = ["trump", "biden", "election", "president", "congress", "senate",
                   "democrat", "republican", "governor", "mayor", "parliament",
                   "minister", "vote", "ballot", "primary", "caucus", "gop",
                   "tariff", "sanction", "iran", "china trade", "nato", "ukraine",
                   "legislation", "bill pass", "supreme court", "executive order"]
    for kw in politics_kw:
        if kw in blob:
            return "politics"

    culture_kw = ["oscar", "grammy", "emmy", "golden globe", "movie", "film",
                  "song", "album", "artist", "celebrity", "viral", "tiktok",
                  "youtube", "spotify", "netflix", "disney", "box office",
                  "award", "nomination", "reality tv", "bachelor"]
    for kw in culture_kw:
        if kw in blob:
            return "culture"

    return "other"


# ---- book snapshot ----
def fetch_book(client: httpx.Client, token_id: str) -> dict | None:
    try:
        r = client.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=0.5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("book fetch failed for %s: %s", token_id, e)
        return None


def simulate_fill(book: dict, side: str, size: float) -> tuple[float, float]:
    """Walk the book to compute volume-weighted fill price and total depth consumed.
    Returns (avg_price, filled_size). If book is empty, returns (0, 0).
    """
    if not book:
        return 0.0, 0.0
    levels = book.get("asks" if side == "BUY" else "bids", [])
    remaining = size
    notional = 0.0
    filled = 0.0
    # book levels are sorted; each is {"price": "0.52", "size": "120"}
    sorted_levels = sorted(levels, key=lambda l: float(l["price"]), reverse=(side == "SELL"))
    for lvl in sorted_levels:
        if remaining <= 0:
            break
        lvl_price = float(lvl["price"])
        lvl_size = float(lvl["size"])
        take = min(remaining, lvl_size)
        notional += take * lvl_price
        filled += take
        remaining -= take
    avg = notional / filled if filled > 0 else 0.0
    return avg, filled


def total_book_liquidity_usd(book: dict) -> float:
    if not book:
        return 0.0
    tot = 0.0
    for side in ("bids", "asks"):
        for lvl in book.get(side, []):
            try:
                tot += float(lvl["price"]) * float(lvl["size"])
            except (KeyError, ValueError):
                pass
    return tot


# ---- watcher loop ----
def get_tracked_leaders(conn: sqlite3.Connection, top_n: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT wallet, categories, score
             FROM leaders
            WHERE excluded_reason IS NULL
         ORDER BY score DESC
            LIMIT ?""", (top_n,)
    ).fetchall()
    return [{"wallet": w, "categories": json.loads(c), "score": s} for w, c, s in rows]


MAX_CURSOR_AGE_HOURS = 2  # fast-forward cursors older than this to avoid processing ancient trades

def get_last_ts(conn: sqlite3.Connection, wallet: str) -> int:
    row = conn.execute("SELECT last_trade_ts FROM wallet_cursors WHERE wallet=?", (wallet,)).fetchone()
    cutoff = int(time.time()) - MAX_CURSOR_AGE_HOURS * 3600
    if row is None:
        return cutoff  # new wallet: look back 2 hours
    return max(row[0], cutoff)  # existing wallet: never look back more than 2 hours


def set_last_ts(conn: sqlite3.Connection, wallet: str, ts: int):
    conn.execute(
        "INSERT INTO wallet_cursors(wallet, last_trade_ts) VALUES(?,?) "
        "ON CONFLICT(wallet) DO UPDATE SET last_trade_ts=excluded.last_trade_ts",
        (wallet, ts),
    )


POLL_WORKERS = 40            # concurrent HTTP workers for trade + metadata fetches
MAX_TRADES_PER_LEADER = 10   # cap most-recent trades processed per leader per cycle


def _fetch_leader_fresh(poly: PolyClient, wallet: str, last: int) -> tuple[str, list]:
    """Fetch a single leader's recent trades and return the fresh, ordered, capped slice.
    Pure HTTP + filtering — no DB access, so it is safe to run across threads."""
    try:
        trades = poly.get_trades(wallet, limit=50, offset=0)
    except Exception as e:
        log.warning("trade fetch failed for %s: %s", wallet, e)
        return wallet, []
    fresh = [t for t in trades if t.timestamp > last]
    fresh.sort(key=lambda t: t.timestamp)
    return wallet, fresh[-MAX_TRADES_PER_LEADER:]


def _fetch_market_meta(http: httpx.Client, condition_id: str) -> dict:
    """Fetch CLOB market metadata (end_date, slug, tags, event_id) with retry.
    event_id drives the per-event concentration guard, so a silent failure used to
    leave it empty and let correlated same-game bets slip past the cap. HTTP only —
    thread-safe."""
    for _attempt in range(3):
        try:
            r = http.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=8.0)
            if r.status_code == 200:
                j = r.json()
                return {
                    "end_date": str(j.get("end_date_iso") or j.get("game_start_time") or ""),
                    "game_start": str(j.get("game_start_time") or ""),
                    "slug": j.get("market_slug") or "",
                    "tags": j.get("tags") or [],
                    "event_id": j.get("neg_risk_market_id") or j.get("question_id") or "",
                }
            if r.status_code < 500:
                break  # 4xx — won't change on retry
        except Exception:
            pass
        time.sleep(0.5)
    return {}


def poll_once(conn: sqlite3.Connection, http: httpx.Client, poly: PolyClient) -> int:
    """Run one polling cycle. Returns number of new signals recorded.

    Three phases so all HTTP runs concurrently while DB writes stay serialized
    (SQLite connections are not thread-safe):
      1. parallel — fetch every leader's fresh trades
      2. parallel — pre-fetch order books + market metadata (deduped by key)
      3. sequential — classify, open positions, insert signals against the DB
    """
    ensure_paper_tables(conn)
    ensure_shadow_table(conn)
    _migrate_signals_table(conn)

    leaders = get_tracked_leaders(conn)
    if not leaders:
        log.info("no tracked leaders yet — run `python leader_ranker.py` first")
        return 0

    now = int(time.time())
    last_map = {l["wallet"]: get_last_ts(conn, l["wallet"]) for l in leaders}

    # ---- Phase 1: parallel trade fetch across all leaders ----
    leader_trades: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=POLL_WORKERS) as ex:
        futs = [ex.submit(_fetch_leader_fresh, poly, l["wallet"], last_map[l["wallet"]])
                for l in leaders]
        for f in futs:
            wallet, fresh = f.result()
            if fresh:
                leader_trades[wallet] = fresh

    if not leader_trades:
        return 0

    # ---- Phase 2: parallel pre-fetch of books + market metadata (deduped) ----
    token_ids: set[str] = set()
    condition_ids: set[str] = set()
    for fresh in leader_trades.values():
        for t in fresh:
            tok = t.raw.get("asset") or t.raw.get("tokenId") or ""
            if tok and (now - t.timestamp) < 300:
                token_ids.add(tok)
            if t.market:
                condition_ids.add(t.market)

    with ThreadPoolExecutor(max_workers=POLL_WORKERS) as ex:
        book_futs = {tok: ex.submit(fetch_book, http, tok) for tok in token_ids}
        meta_futs = {cid: ex.submit(_fetch_market_meta, http, cid) for cid in condition_ids}
        book_cache: dict[str, dict | None] = {tok: f.result() for tok, f in book_futs.items()}
        meta_cache: dict[str, dict] = {cid: f.result() for cid, f in meta_futs.items()}

    # ---- Phase 3: sequential DB writes (preserve deterministic leader order) ----
    new_signals = 0
    for leader in leaders:
        wallet = leader["wallet"]
        fresh = leader_trades.get(wallet)
        if not fresh:
            continue

        for t in fresh:
            token_id = t.raw.get("asset") or t.raw.get("tokenId") or ""
            market_title = t.raw.get("title") or ""
            market_slug = t.raw.get("slug") or t.raw.get("eventSlug") or ""
            market_category = categorize_market(t.raw, title=market_title, slug=market_slug)

            book = book_cache.get(token_id)  # None if too old or fetch failed

            if book:
                hypo_price, _ = simulate_fill(book, t.side, t.size)
            else:
                hypo_price = t.price  # fall back to leader's actual fill price

            slippage_bps = (hypo_price - t.price) * 10_000 if hypo_price > 0 else 0.0
            if t.side == "SELL":
                slippage_bps = -slippage_bps

            total_liq = total_book_liquidity_usd(book) if book else 0
            liq_bucket = liquidity_bucket(total_liq)
            lat_bucket = latency_bucket(now - t.timestamp)

            # Market metadata from the parallel pre-fetch
            meta = meta_cache.get(t.market, {})
            end_date = meta.get("end_date", "")
            clob_slug = meta.get("slug") or market_slug
            event_id = meta.get("event_id", "")
            game_start = meta.get("game_start", "")
            if meta.get("tags"):
                market_category = categorize_market(t.raw, title=market_title, slug=market_slug, tags=meta["tags"])

            decision = classify(
                conn, wallet, t.market, t.side,
                hypo_price=hypo_price,
                liquidity_usd=total_liq, slippage_bps=slippage_bps,
                leader_size=t.size,
                end_date=end_date,
                category=market_category,
                market_title=market_title,
                outcome=t.outcome,
            )

            pos_result = {"opened": False, "reason": "filter_rejected"}
            if decision["filter_status"] == "copy" and hypo_price > 0:
                pos_result = open_paper_position(
                    conn, 0, t.market, t.outcome, t.side,
                    hypo_price, decision["signal_strength"],
                    market_title=market_title,
                    category=market_category,
                    end_date=end_date,
                    market_slug=clob_slug,
                    event_id=event_id,
                    game_start=game_start,
                    weighted_conviction=decision.get("weighted_conviction", 0.0),
                )

            cur = conn.execute(
                """INSERT INTO signals
                   (leader_wallet, market, outcome, side, leader_price, leader_size,
                    leader_ts, observed_ts, our_hypo_price, our_hypo_slippage_bps,
                    liquidity_bucket, latency_bucket, filter_status, category,
                    conviction_count, conviction_wallets, signal_strength, filter_reason,
                    market_title, market_slug, position_opened, position_reason,
                    shadow_min5, shadow_no_last2h)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wallet, t.market, t.outcome, t.side, t.price, t.size,
                 t.timestamp, now, hypo_price, slippage_bps,
                 liq_bucket, lat_bucket, decision["filter_status"], market_category,
                 decision["conviction_count"], decision["conviction_wallets"],
                 decision["signal_strength"], decision["filter_reason"],
                 market_title, market_slug,
                 1 if pos_result.get("opened") else 0,
                 pos_result.get("reason", ""),
                 decision.get("shadow_min5", 0),
                 decision.get("shadow_no_last2h", 0)),
            )
            signal_id = cur.lastrowid

            if pos_result.get("opened") and pos_result.get("position_id"):
                conn.execute("UPDATE paper_positions SET signal_id=? WHERE id=?",
                             (signal_id, pos_result["position_id"]))

            # Shadow book: if this passed conviction but got SKIPPED by a downstream
            # filter, record a hypothetical position so we measure the win rate of
            # everything we don't bet (validates every filter forward). No real money.
            if decision["filter_status"] == "copy" and not pos_result.get("opened"):
                try:
                    open_shadow_position(
                        conn, signal_id, t.market, t.outcome, t.side,
                        hypo_price, decision["signal_strength"],
                        pos_result.get("reason", ""),
                        market_title=market_title, category=market_category,
                        end_date=end_date, market_slug=clob_slug,
                    )
                except Exception as e:
                    log.warning("shadow open failed (%s): %s", type(e).__name__, e)

            new_signals += 1

        set_last_ts(conn, wallet, fresh[-1].timestamp)
        conn.commit()

    return new_signals


def _migrate_signals_table(conn: sqlite3.Connection):
    """Add columns and indexes introduced by the strategy layer if they don't exist."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    for col, ddl in [
        ("conviction_count",    "INTEGER DEFAULT 0"),
        ("conviction_wallets",  "TEXT"),
        ("signal_strength",     "INTEGER DEFAULT 1"),
        ("filter_reason",       "TEXT"),
        ("market_title",        "TEXT DEFAULT ''"),
        ("market_slug",         "TEXT DEFAULT ''"),
        ("position_opened",     "INTEGER DEFAULT 0"),
        ("position_reason",     "TEXT DEFAULT ''"),
        ("shadow_min5",         "INTEGER DEFAULT 0"),  # shadow: would min_leaders=5 have copied?
        ("shadow_no_last2h",    "INTEGER DEFAULT 0"),  # shadow: would blocking last-2h sports have copied?
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {ddl}")
    # Critical index for conviction queries — without it each query is a full table scan
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_market_side ON signals(market, side, observed_ts)")
    conn.commit()
