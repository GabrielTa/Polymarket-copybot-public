"""CLV backfill — Closing Line Value for every resolved copied trade.

CLV = did the market price move TOWARD us after we entered, measured just before
the event (NOT the 0/1 resolution). Positive CLV = we got a good price / captured
real information; negative CLV = we were exit liquidity. CLV converges to skill in
dozens of bets instead of hundreds and is immune to win/loss variance.

For each resolved position we:
  1. resolve the CLOB token_id from the condition_id (cached per market)
  2. fetch the token's price history
  3. take the last price strictly BEFORE the event time (end_date) as the "close"
     — never the terminal 0/1 print
  4. CLV = close - entry (BUY) / entry - close (SELL)
  5. flag closes at 0/1 extremes as unreliable (market resolved before our marker)

Results land in the clv_scores table. Incremental: re-runs skip already-scored
positions unless --refresh. Rate-limited + market-cache so re-runs are cheap.

Usage:  python clv_backfill.py [--limit N] [--refresh]

⚠️ VERIFY before trusting: the price API is correct as of writing, but a
systematically wrong "close" poisons every downstream verdict. Hand-check 3 rows
of clv_scores against the Polymarket UI price chart first.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("clv")
BASE = Path(__file__).parent
DB = BASE / "data" / "copybot.db"
CLOB = "https://clob.polymarket.com"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS clv_scores (
            position_id INTEGER PRIMARY KEY,
            leader_wallet TEXT,
            token_id TEXT,
            entry_price REAL,
            close_price REAL,
            clv REAL,
            event_ts INTEGER,
            pnl_usd REAL,
            signal_strength INTEGER,
            category TEXT,
            market_title TEXT,
            flag TEXT DEFAULT '',
            scored_ts INTEGER
        )"""
    )
    conn.commit()


def parse_ts(iso: str) -> int | None:
    if not iso:
        return None
    try:
        s = iso.rstrip("Z").split(".")[0]
        if "T" not in s:
            s += "T00:00:00"
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def token_for_outcome(market: dict, outcome: str) -> str | None:
    toks = market.get("tokens", [])
    for t in toks:
        if (t.get("outcome") or "").lower() == (outcome or "").lower():
            return t.get("token_id")
    return toks[0].get("token_id") if toks else None


def backfill(limit: int | None = None, refresh: bool = False) -> None:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    ensure_table(conn)

    done = {r[0] for r in conn.execute("SELECT position_id FROM clv_scores")}
    q = """SELECT pp.id, pp.market, pp.outcome, pp.side, pp.entry_price, pp.end_date,
                  pp.opened_ts, pp.pnl_usd, pp.signal_strength, pp.category, pp.market_title,
                  s.leader_wallet
             FROM paper_positions pp
             LEFT JOIN signals s ON pp.signal_id = s.id
            WHERE pp.status IN ('closed','exited') AND pp.opened_ts > 0
         ORDER BY pp.closed_ts DESC"""
    rows = conn.execute(q).fetchall()
    if not refresh:
        rows = [r for r in rows if r["id"] not in done]
    if limit:
        rows = rows[:limit]
    log.info("scoring %d positions (%d already done)", len(rows), len(done))

    http = httpx.Client(timeout=15, headers={"User-Agent": "copy-bot/clv"})
    mkt_cache: dict[str, dict | None] = {}
    scored = skipped = 0

    for i, r in enumerate(rows, 1):
        entry_ts = r["opened_ts"]
        if not entry_ts:
            skipped += 1
            continue
        # market (cached)
        if r["market"] not in mkt_cache:
            try:
                resp = http.get(f"{CLOB}/markets/{r['market']}")
                mkt_cache[r["market"]] = resp.json() if resp.status_code == 200 else None
            except Exception:
                mkt_cache[r["market"]] = None
            time.sleep(0.15)
        market = mkt_cache[r["market"]]
        if not market:
            skipped += 1
            continue
        tid = token_for_outcome(market, r["outcome"])
        if not tid:
            skipped += 1
            continue
        # Event time = actual game start (the true closing line), falling back to
        # entry+2h when no start time. NEVER end_date (midnight-of-day is not the game).
        gst_raw = market.get("game_start_time") or market.get("gameStartTime")
        gst = parse_ts(str(gst_raw)) if gst_raw else None
        if gst and gst > entry_ts:
            event_ts = gst
        else:
            event_ts = entry_ts + 2 * 3600
        # price history
        try:
            ph = http.get(f"{CLOB}/prices-history", params={"market": tid, "interval": "max", "fidelity": 60})
            hist = ph.json().get("history", []) if ph.status_code == 200 else []
        except Exception:
            hist = []
        time.sleep(0.2)
        # close = last price AFTER our entry and at/before the event (never before entry)
        window = [pt for pt in hist if entry_ts < pt.get("t", 0) <= event_ts and pt.get("p") is not None]
        if not window:
            skipped += 1
            continue
        close = float(window[-1]["p"])
        entry = float(r["entry_price"])
        clv = (close - entry) if (r["side"] or "BUY") == "BUY" else (entry - close)
        flag = "extreme" if (close > 0.97 or close < 0.03) else ""

        conn.execute(
            """INSERT OR REPLACE INTO clv_scores
               (position_id, leader_wallet, token_id, entry_price, close_price, clv,
                event_ts, pnl_usd, signal_strength, category, market_title, flag, scored_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["id"], r["leader_wallet"], tid, entry, close, clv, event_ts,
             r["pnl_usd"], r["signal_strength"], r["category"], r["market_title"], flag,
             int(time.time())),
        )
        scored += 1
        if scored % 50 == 0:
            conn.commit()
            log.info("  %d/%d scored", i, len(rows))

    conn.commit()
    log.info("done: %d scored, %d skipped", scored, skipped)

    # verification sample
    print("\n=== VERIFY these 5 against the Polymarket UI price chart before trusting ===")
    for r in conn.execute(
        "SELECT position_id, entry_price, close_price, clv, flag, market_title FROM clv_scores ORDER BY RANDOM() LIMIT 5"
    ):
        print("  pos %d | entry %.3f -> close %.3f | CLV %+.3f %s | %s"
              % (r["position_id"], r["entry_price"], r["close_price"], r["clv"],
                 ("["+r["flag"]+"]") if r["flag"] else "", (r["market_title"] or "")[:40]))
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    backfill(limit=args.limit, refresh=args.refresh)
