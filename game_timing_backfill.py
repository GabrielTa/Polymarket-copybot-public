"""Backfill game_start_ts for historical positions.

Fetches each resolved market's scheduled game start time from the CLOB API and
stores it on paper_positions.game_start_ts, so we can classify every past bet as
PRE-GAME (opened before kickoff) or IN-PLAY (opened after). Cached + incremental.

Usage:  python game_timing_backfill.py [--limit N]
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("timing")
DB = Path(__file__).parent / "data" / "copybot.db"
CLOB = "https://clob.polymarket.com"


def parse_ts(iso: str) -> int:
    try:
        s = str(iso).rstrip("Z").split(".")[0]
        if "T" not in s:
            s += "T00:00:00"
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def backfill(limit: int | None = None) -> None:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    # ensure column exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_positions)")}
    if "game_start_ts" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN game_start_ts INTEGER DEFAULT 0")
        conn.commit()

    rows = conn.execute(
        """SELECT id, market FROM paper_positions
            WHERE status IN ('closed','exited') AND COALESCE(game_start_ts,0)=0
         ORDER BY closed_ts DESC"""
    ).fetchall()
    if limit:
        rows = rows[:limit]
    log.info("backfilling game_start_ts for %d positions", len(rows))

    http = httpx.Client(timeout=15, headers={"User-Agent": "copy-bot/timing"})
    cache: dict[str, int] = {}
    filled = 0
    for i, r in enumerate(rows, 1):
        if r["market"] not in cache:
            gst = 0
            try:
                resp = http.get(f"{CLOB}/markets/{r['market']}")
                if resp.status_code == 200:
                    gst = parse_ts(resp.json().get("game_start_time") or "")
            except Exception:
                gst = 0
            cache[r["market"]] = gst
            time.sleep(0.13)
        gst = cache[r["market"]]
        if gst:
            conn.execute("UPDATE paper_positions SET game_start_ts=? WHERE id=?", (gst, r["id"]))
            filled += 1
        if i % 50 == 0:
            conn.commit()
            log.info("  %d/%d (%d with game start)", i, len(rows), filled)
    conn.commit()
    log.info("done: %d/%d positions got a game_start_ts", filled, len(rows))
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    backfill(limit=args.limit)
