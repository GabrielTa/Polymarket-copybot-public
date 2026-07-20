"""FastAPI dashboard server.

Endpoints:
  GET  /                          -- the dashboard HTML
  GET  /api/leaders               -- ranked leader table (includes 7d form)
  GET  /api/signals?since=ID      -- signals newer than a given id
  GET  /api/pnl                   -- cumulative hypothetical PnL series
  GET  /api/daily_pnl             -- daily P&L breakdown for bar chart
  GET  /api/live_positions        -- open positions with current CLOB prices
  GET  /api/buckets               -- (liquidity x latency) heatmap data
  GET  /api/stream                -- SSE stream of new signals
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "copybot.db"

app = FastAPI(title="Polymarket Copy Bot Dashboard")
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")

# Simple TTL cache for expensive endpoints
_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 55  # seconds — slightly under the 60s JS poll interval so data stays fresh

def _cached(key: str, ttl: int, fn):
    now = time.time()
    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    result = fn()
    _cache[key] = (now, result)
    return result


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)  # wait for writer locks instead of erroring
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/")
def index():
    return FileResponse(BASE / "web" / "index.html")


@app.get("/shadow")
def shadow_dashboard():
    return FileResponse(BASE / "web" / "shadow.html")


@app.get("/sizing")
def sizing_dashboard():
    return FileResponse(BASE / "web" / "sizing.html")


# Conviction-weighted sizing multipliers (experiment only — real book is untouched).
# str<=4 = 1.0x, str5 = 1.5x, str6+ = 2.0x (capped). A bet's outcome is independent of
# its stake, so we re-weight resolved positions to simulate what this sizing would return.
def _conv_mult(strength: int) -> float:
    if strength is None or strength <= 4:
        return 1.0
    if strength >= 6:
        return 2.0
    return 1.5


def _max_drawdown(equity_series: list[float]) -> float:
    peak = equity_series[0] if equity_series else 0.0
    mdd = 0.0
    for v in equity_series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


@app.get("/api/sizing_sim")
def sizing_sim():
    """Compare the real flat-% book vs a conviction-weighted variant, by re-weighting
    resolved positions. ROI (PnL per $ risked) is the fair comparison since the two
    deploy different capital. No real bets — pure analytics."""
    with db() as c:
        rows = c.execute(
            """SELECT signal_strength AS s, cost_usd AS cost, pnl_usd AS pnl,
                      date(closed_ts,'unixepoch') AS day
                 FROM paper_positions
                WHERE status IN ('closed','exited') AND closed_ts IS NOT NULL
                  AND cost_usd > 0
             ORDER BY closed_ts ASC"""
        ).fetchall()

    from collections import defaultdict
    act_stake = sim_stake = act_pnl = sim_pnl = 0.0
    act_cum, sim_cum = [], []
    daily = defaultdict(lambda: [0.0, 0.0])   # day -> [act_pnl, sim_pnl]
    by_conv = defaultdict(lambda: {"n": 0, "act": 0.0, "sim": 0.0, "mult": 1.0})
    a_run = s_run = 0.0
    for r in rows:
        m = _conv_mult(r["s"])
        a_p = r["pnl"] or 0.0
        s_p = a_p * m
        act_stake += r["cost"]; sim_stake += r["cost"] * m
        act_pnl += a_p; sim_pnl += s_p
        a_run += a_p; s_run += s_p
        act_cum.append(a_run); sim_cum.append(s_run)
        daily[r["day"]][0] += a_p; daily[r["day"]][1] += s_p
        b = by_conv[r["s"] or 0]; b["n"] += 1; b["act"] += a_p; b["sim"] += s_p; b["mult"] = m

    days = sorted(daily)
    return {
        "note": "Experiment: conviction-weighted sizing (str5=1.5x, str6+=2x) vs your real flat-% book. Re-weighted from resolved positions — no real bets.",
        "trades": len(rows),
        "actual": {
            "pnl": round(act_pnl, 2),
            "roi_pct": round(100 * act_pnl / act_stake, 2) if act_stake else 0,
            "max_drawdown": round(_max_drawdown(act_cum), 2),
            "staked": round(act_stake, 2),
        },
        "weighted": {
            "pnl": round(sim_pnl, 2),
            "roi_pct": round(100 * sim_pnl / sim_stake, 2) if sim_stake else 0,
            "max_drawdown": round(_max_drawdown(sim_cum), 2),
            "staked": round(sim_stake, 2),
        },
        "by_conviction": [
            {"strength": k, "mult": by_conv[k]["mult"], "trades": by_conv[k]["n"],
             "actual_pnl": round(by_conv[k]["act"], 2), "weighted_pnl": round(by_conv[k]["sim"], 2)}
            for k in sorted(by_conv)
        ],
        "curve": {
            "days": days,
            "actual": [round(sum(daily[d][0] for d in days[:i+1]), 2) for i in range(len(days))],
            "weighted": [round(sum(daily[d][1] for d in days[:i+1]), 2) for i in range(len(days))],
        },
    }


@app.get("/api/leaders")
def leaders(limit: int = 50):
    return _cached(f"leaders:{limit}", CACHE_TTL, lambda: _leaders_query(limit))

def _leaders_query(limit: int):
    now = int(time.time())
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400

    with db() as c:
        rows = c.execute(
            """SELECT wallet, trades_total, markets_total, resolved_positions,
                      hit_rate, avg_edge_bps, avg_notional, total_pnl_usd,
                      categories, persistence, best_seed_rank, score,
                      excluded_reason, updated_at
                 FROM leaders
             ORDER BY score DESC
                LIMIT ?""", (limit,)
        ).fetchall()

        # 7-day performance
        week_rows = c.execute(
            """SELECT s.leader_wallet,
                      COUNT(*) as n,
                      SUM(CASE WHEN pp.pnl_usd > 0 THEN 1 ELSE 0 END) as wins
                 FROM paper_positions pp
                 JOIN signals s ON pp.signal_id = s.id
                WHERE pp.status = 'closed' AND pp.closed_ts >= ?
             GROUP BY s.leader_wallet""", (week_ago,)
        ).fetchall()

        # 30-day performance
        month_rows = c.execute(
            """SELECT s.leader_wallet,
                      COUNT(*) as n,
                      SUM(CASE WHEN pp.pnl_usd > 0 THEN 1 ELSE 0 END) as wins
                 FROM paper_positions pp
                 JOIN signals s ON pp.signal_id = s.id
                WHERE pp.status = 'closed' AND pp.closed_ts >= ?
             GROUP BY s.leader_wallet""", (month_ago,)
        ).fetchall()

        # Win streaks: get last 20 trades per leader to find longest streak
        streak_data = c.execute(
            """SELECT s.leader_wallet,
                      pp.pnl_usd,
                      pp.closed_ts
                 FROM paper_positions pp
                 JOIN signals s ON pp.signal_id = s.id
                WHERE pp.status = 'closed'
             ORDER BY s.leader_wallet, pp.closed_ts DESC"""
        ).fetchall()

        # Copied trade counts per leader (all-time and last 30 days)
        copied_rows = c.execute(
            """SELECT s.leader_wallet,
                      COUNT(pp.id) as copied_total,
                      SUM(CASE WHEN pp.closed_ts >= ? THEN 1 ELSE 0 END) as copied_30d,
                      SUM(CASE WHEN pp.pnl_usd > 0 THEN 1 ELSE 0 END) as copied_wins
                 FROM signals s
                 JOIN paper_positions pp ON pp.signal_id = s.id
                WHERE pp.status IN ('closed', 'exited')
             GROUP BY s.leader_wallet""", (month_ago,)
        ).fetchall()

    week_map = {w: {"n": n, "wins": wins} for w, n, wins in week_rows}
    month_map = {w: {"n": n, "wins": wins} for w, n, wins in month_rows}
    copied_map = {w: {"total": t, "month": m, "wins": wn} for w, t, m, wn in copied_rows}

    # Calculate win streaks
    streak_map = {}
    current_wallet = None
    current_streak = 0
    max_streak = 0
    for wallet, pnl, _ in streak_data:
        if wallet != current_wallet:
            streak_map[wallet] = max_streak
            current_wallet = wallet
            current_streak = 0
            max_streak = 0
        if pnl > 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    if current_wallet:
        streak_map[current_wallet] = max_streak

    out = [dict(r) for r in rows]
    for r in out:
        r["categories"] = json.loads(r["categories"] or "[]")
        # 7-day metrics
        week = week_map.get(r["wallet"], {})
        r["week_trades"] = week.get("n", 0)
        r["week_wins"] = week.get("wins", 0)
        # 30-day metrics
        month = month_map.get(r["wallet"], {})
        r["month_trades"] = month.get("n", 0)
        r["month_wins"] = month.get("wins", 0)
        r["month_win_rate"] = round(month["wins"] / month["n"] * 100, 1) if month.get("n", 0) > 0 else 0
        # Win streak
        r["win_streak"] = streak_map.get(r["wallet"], 0)
        # Copied trade counts
        copied = copied_map.get(r["wallet"], {})
        r["copied_total"] = copied.get("total", 0)
        r["copied_30d"] = copied.get("month", 0)
        r["copied_wins"] = copied.get("wins", 0)

        # ---- Bot Score: hybrid metric (our copy results + PM external data) ----
        # For leaders with ≥5 copied trades, weight our real copy results heavily.
        # For untested leaders, fall back to the Polymarket-derived score.
        copy_total = r["copied_total"]
        copy_wr = (r["copied_wins"] / copy_total) if copy_total >= 5 else None
        pm_rank = r.get("best_seed_rank") or 999
        pm_rank_component = max(0.0, (100 - min(pm_rank, 100)) / 100)   # #1→1.0, #100+→0.0
        persistence_component = min(1.0, (r.get("persistence") or 0) / 5)
        month_wr_component = (r["month_win_rate"] / 100) if r["month_win_rate"] else 0.0

        if copy_wr is not None:
            # Tested leader: weight our copy results most
            bot_score = (
                0.40 * copy_wr
                + 0.25 * pm_rank_component
                + 0.20 * month_wr_component
                + 0.15 * persistence_component
            )
        else:
            # Untested leader: fall back to Polymarket composite score
            bot_score = r.get("score") or 0.0

        r["bot_score"] = round(bot_score, 3)

    # Re-sort by bot_score descending
    out.sort(key=lambda x: x["bot_score"], reverse=True)
    return {"leaders": out, "count": len(out)}


@app.get("/api/signals")
def signals(since: int = 0, limit: int = 100):
    with db() as c:
        rows = c.execute(
            """SELECT * FROM signals
                WHERE id > ?
             ORDER BY id DESC
                LIMIT ?""", (since, limit)
        ).fetchall()
    return {"signals": [dict(r) for r in rows]}


@app.get("/api/pnl")
def pnl():
    """Real paper bankroll history — one point per bankroll-changing event."""
    with db() as c:
        rows = c.execute(
            """SELECT ts, cash_usd, open_exposure_usd, equity_usd
                 FROM paper_bankroll_history
             ORDER BY ts ASC"""
        ).fetchall()
    series = [
        {"ts": r["ts"], "cash": round(r["cash_usd"], 2),
         "exposure": round(r["open_exposure_usd"], 2),
         "equity": round(r["equity_usd"], 2)}
        for r in rows
    ]
    return {"series": series}


@app.get("/api/positions")
def positions(status: str = "open", result: str = "all", limit: int = 50):
    """
    status: 'open' | 'closed' | 'exited' | 'all_closed' (closed + exited combined)
    result: 'all' | 'won' | 'lost'
    """
    with db() as c:
        base = """
            SELECT pp.*,
                   s.conviction_count, s.signal_strength as sig_strength
              FROM paper_positions pp
              LEFT JOIN signals s ON pp.signal_id = s.id
        """
        if status == "open":
            rows = c.execute(base + "WHERE pp.status='open' ORDER BY pp.opened_ts DESC LIMIT ?", (limit,)).fetchall()
        elif status == "all_closed":
            # All resolved: closed (won+lost) + exited, filtered by result
            if result == "won":
                rows = c.execute(base + "WHERE pp.status='closed' AND pp.pnl_usd > 0 ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
            elif result == "lost":
                # Lost = closed losses + ALL exited (exited are always losses)
                rows = c.execute(base + "WHERE (pp.status='closed' AND pp.pnl_usd <= 0) OR pp.status='exited' ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
            else:
                # All: closed + exited
                rows = c.execute(base + "WHERE pp.status IN ('closed','exited') ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
        elif status == "closed":
            if result == "won":
                rows = c.execute(base + "WHERE pp.status='closed' AND pp.pnl_usd > 0 ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
            elif result == "lost":
                rows = c.execute(base + "WHERE pp.status='closed' AND pp.pnl_usd <= 0 ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = c.execute(base + "WHERE pp.status='closed' ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
        elif status == "exited":
            rows = c.execute(base + "WHERE pp.status='exited' ORDER BY pp.closed_ts DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = c.execute(base + "WHERE pp.status=? ORDER BY pp.opened_ts DESC LIMIT ?", (status, limit)).fetchall()
    return {"positions": [dict(r) for r in rows]}


@app.get("/api/bankroll")
def bankroll():
    with db() as c:
        cash = c.execute("SELECT cash_usd FROM paper_bankroll WHERE id=1").fetchone()
        exp  = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM paper_positions WHERE status='open'"
        ).fetchone()
        n_open = c.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0]
        n_closed = c.execute("SELECT COUNT(*) FROM paper_positions WHERE status='closed'").fetchone()[0]
        n_exited = c.execute("SELECT COUNT(*) FROM paper_positions WHERE status='exited'").fetchone()[0]
        total_pnl = c.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) FROM paper_positions WHERE status IN ('closed','exited')"
        ).fetchone()[0]
    cash_usd = cash[0] if cash else 1000.0
    exposure_usd = exp[0] if exp else 0.0
    return {
        "cash": round(cash_usd, 2),
        "exposure": round(exposure_usd, 2),
        "equity": round(cash_usd + exposure_usd, 2),
        "open_positions": n_open,
        "closed_positions": n_closed,
        "exited_positions": n_exited,
        "realized_pnl": round(total_pnl, 2),
    }


@app.get("/api/analytics")
def analytics():
    """Full trade analytics breakdown."""
    return _cached("analytics", CACHE_TTL, _analytics_query)

def _analytics_query():
    with db() as c:
        # Overall stats (closed only for WR, both for net PnL)
        row = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as won,
                   SUM(CASE WHEN pnl_usd <= 0 AND status='closed' THEN 1 ELSE 0 END) as lost,
                   SUM(CASE WHEN status='exited' THEN 1 ELSE 0 END) as exited,
                   SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END) as gross_won,
                   SUM(CASE WHEN pnl_usd < 0 THEN pnl_usd ELSE 0 END) as gross_lost,
                   AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END) as avg_win,
                   AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END) as avg_loss,
                   AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd/cost_usd*100 END) as avg_win_pct,
                   AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd/cost_usd*100 END) as avg_loss_pct,
                   MAX(pnl_usd) as best_trade,
                   MIN(pnl_usd) as worst_trade,
                   SUM(pnl_usd) as net_pnl,
                   AVG((closed_ts - opened_ts) / 3600.0) as avg_hold_h
              FROM paper_positions
             WHERE status IN ('closed','exited')
        """).fetchone()
        total      = row[0] or 0
        won        = row[1] or 0
        lost       = row[2] or 0
        exited     = row[3] or 0
        gross_won  = row[4] or 0
        gross_lost = row[5] or 0
        avg_win    = row[6] or 0
        avg_loss   = row[7] or 0
        avg_win_pct  = row[8] or 0
        avg_loss_pct = row[9] or 0
        best_trade   = row[10] or 0
        worst_trade  = row[11] or 0
        net_pnl      = row[12] or 0
        avg_hold_h   = row[13] or 0

        # By category
        cat_rows = c.execute("""
            SELECT COALESCE(category,'other') as cat,
                   COUNT(*) as n,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(pnl_usd),0) as pnl,
                   ROUND(AVG(CASE WHEN pnl_usd>0 THEN pnl_usd/cost_usd*100 END),1) as avg_win_pct
              FROM paper_positions WHERE status='closed'
          GROUP BY cat ORDER BY pnl DESC
        """).fetchall()

        # By signal strength
        str_rows = c.execute("""
            SELECT signal_strength,
                   COUNT(*) as n,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   COALESCE(SUM(pnl_usd),0) as pnl
              FROM paper_positions WHERE status='closed'
          GROUP BY signal_strength ORDER BY signal_strength
        """).fetchall()

        # By hold time bucket
        hold_rows = c.execute("""
            SELECT CASE
                     WHEN (closed_ts-opened_ts)/3600.0 < 1  THEN '<1h'
                     WHEN (closed_ts-opened_ts)/3600.0 < 6  THEN '1-6h'
                     WHEN (closed_ts-opened_ts)/3600.0 < 24 THEN '6-24h'
                     ELSE '24h+' END as bucket,
                   COUNT(*) as n,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(pnl_usd),2) as pnl,
                   ROUND(AVG(pnl_usd/cost_usd)*100,1) as avg_ret_pct
              FROM paper_positions WHERE status IN ('closed','exited') AND cost_usd > 0
          GROUP BY bucket ORDER BY pnl DESC
        """).fetchall()

        # By entry price bucket
        entry_rows = c.execute("""
            SELECT CASE
                     WHEN entry_price < 0.50 THEN '<0.50'
                     WHEN entry_price < 0.60 THEN '0.50-0.60'
                     WHEN entry_price < 0.70 THEN '0.60-0.70'
                     ELSE '0.70+' END as bucket,
                   COUNT(*) as n,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(pnl_usd),2) as pnl
              FROM paper_positions WHERE status='closed'
          GROUP BY bucket ORDER BY entry_price ASC
        """).fetchall()

        # Trade journal (no leader_accuracy — covered by leaderboard)
        journal = c.execute("""
            SELECT pp.id, pp.market_title, pp.market_slug, pp.outcome, pp.side,
                   pp.category, pp.entry_price, pp.close_price, pp.cost_usd,
                   pp.pnl_usd, pp.signal_strength, pp.opened_ts, pp.closed_ts,
                   pp.status, s.conviction_count, s.filter_reason
              FROM paper_positions pp
              LEFT JOIN signals s ON pp.signal_id = s.id
             WHERE pp.status IN ('closed', 'exited')
          ORDER BY pp.closed_ts DESC
        """).fetchall()

    return {
        "total_closed": total,
        "won": won, "lost": lost, "exited": exited,
        "win_rate": round(won / (won + lost) * 100, 1) if (won + lost) > 0 else 0,
        "net_pnl":    round(net_pnl, 2),
        "gross_won":  round(gross_won, 2),
        "gross_lost": round(gross_lost, 2),
        "avg_win":    round(avg_win, 2),
        "avg_loss":   round(avg_loss, 2),
        "avg_win_pct":  round(avg_win_pct, 1),
        "avg_loss_pct": round(avg_loss_pct, 1),
        "best_trade":   round(best_trade, 2),
        "worst_trade":  round(worst_trade, 2),
        "avg_hold_h":   round(avg_hold_h, 1),
        "profit_factor": round(abs(gross_won / gross_lost), 2) if gross_lost != 0 else 0,
        "by_category": [dict(r) for r in cat_rows],
        "by_strength": [dict(r) for r in str_rows],
        "by_hold":     [dict(r) for r in hold_rows],
        "by_entry":    [dict(r) for r in entry_rows],
        "journal":     [dict(r) for r in journal],
    }


@app.get("/api/shadow")
def shadow():
    """Shadow book — hypothetical win rate / PnL of bets we SKIP, sliced by skip
    reason and category. Validates every filter decision forward on new data."""
    def _agg(group_col: str):
        with db() as c:
            try:
                rows = c.execute(
                    f"""SELECT {group_col} AS k,
                              COUNT(*) AS n,
                              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                              COALESCE(SUM(pnl_usd), 0) AS pnl
                         FROM shadow_positions
                        WHERE status='closed'
                     GROUP BY {group_col}
                     ORDER BY pnl ASC"""
                ).fetchall()
            except Exception:
                return []
        out = []
        for r in rows:
            n = r["n"] or 0
            out.append({
                "key": r["k"] or "(none)",
                "resolved": n,
                "win_rate": round(100 * (r["wins"] or 0) / n, 1) if n else 0,
                "pnl_per_100": round((r["pnl"] or 0) / n, 2) if n else 0,
                "total_pnl": round(r["pnl"] or 0, 2),
            })
        return out

    def _entry_band():
        with db() as c:
            try:
                rows = c.execute(
                    """SELECT CASE
                                WHEN entry_price < 0.40 THEN '<0.40'
                                WHEN entry_price < 0.50 THEN '0.40-0.50'
                                WHEN entry_price < 0.60 THEN '0.50-0.60'
                                WHEN entry_price < 0.70 THEN '0.60-0.70'
                                WHEN entry_price < 0.80 THEN '0.70-0.80'
                                ELSE '0.80+' END AS k,
                              COUNT(*) AS n,
                              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                              COALESCE(SUM(pnl_usd), 0) AS pnl
                         FROM shadow_positions WHERE status='closed'
                     GROUP BY k ORDER BY k"""
                ).fetchall()
            except Exception:
                return []
        return [{"key": r["k"], "resolved": r["n"],
                 "win_rate": round(100 * (r["wins"] or 0) / r["n"], 1) if r["n"] else 0,
                 "total_pnl": round(r["pnl"] or 0, 2)} for r in rows]

    def _positions():
        with db() as c:
            try:
                rows = c.execute(
                    """SELECT market_title, market_slug, outcome, side, entry_price, category,
                              signal_strength, skip_reason, status, pnl_usd, close_price,
                              opened_ts, closed_ts
                         FROM shadow_positions
                     ORDER BY COALESCE(closed_ts, opened_ts) DESC
                        LIMIT 150"""
                ).fetchall()
            except Exception:
                return []
        return [{
            "market_title": r["market_title"] or "",
            "market_slug": r["market_slug"] or "",
            "outcome": r["outcome"] or "",
            "side": r["side"] or "",
            "entry_price": round(r["entry_price"] or 0, 3),
            "category": r["category"] or "",
            "strength": r["signal_strength"] or 0,
            "skip_reason": r["skip_reason"] or "",
            "status": r["status"] or "",
            "pnl": round(r["pnl_usd"], 2) if r["pnl_usd"] is not None else None,
            "close_price": r["close_price"],
            "opened_ts": r["opened_ts"],
        } for r in rows]

    with db() as c:
        try:
            open_n = c.execute("SELECT COUNT(*) FROM shadow_positions WHERE status='open'").fetchone()[0]
            row = c.execute(
                """SELECT COUNT(*) n, SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) wins,
                          COALESCE(SUM(pnl_usd),0) pnl FROM shadow_positions WHERE status='closed'"""
            ).fetchone()
            closed_n, wins, pnl = row["n"] or 0, row["wins"] or 0, row["pnl"] or 0
        except Exception:
            open_n = closed_n = wins = 0; pnl = 0
    return {
        "note": "Hypothetical bets we skipped — $100 flat stake each. No real money.",
        "open": open_n, "resolved": closed_n,
        "overall_win_rate": round(100 * wins / closed_n, 1) if closed_n else 0,
        "overall_pnl": round(pnl, 2),
        "by_skip_reason": _agg("skip_reason"),
        "by_category": _agg("category"),
        "by_entry_band": _entry_band(),
        "positions": _positions(),
    }


@app.get("/api/kelly_sim")
def kelly_sim():
    """Walk-forward simulation of the friend's price-aware fractional-Kelly sizer
    (copybot_sizing_strategy_v2.9) on real resolved positions. For each bet, the stake
    is computed from ONLY the win-rate data that had resolved before that bet opened
    (no lookahead) — Wilson lower bound, per price bucket, edge floor, half-Kelly, /C.
    Compared against flat sizing at the same reference bankroll. No real bets."""
    import math

    K = 0.5; EDGE_FLOOR = 0.02; C = 4
    MAX_SINGLE = 0.05; MAX_TOTAL = 0.20
    MIN_BUCKET_N = 50; PENALTY = 0.02; WINDOW = 400
    BANKROLL = 1000.0
    FLAT_FRAC = 0.01   # flat baseline: 1% of bankroll per bet

    def wilson_lower(wins, n, z=1.96):
        if n == 0:
            return 0.0
        q = wins / n
        denom = 1 + z * z / n
        center = q + z * z / (2 * n)
        margin = z * math.sqrt(q * (1 - q) / n + z * z / (4 * n * n))
        return (center - margin) / denom

    def pbucket(p):
        return round(math.floor(p / 0.05) * 0.05, 2)

    with db() as c:
        rows = c.execute(
            """SELECT entry_price AS p, cost_usd AS cost, pnl_usd AS pnl,
                      opened_ts, closed_ts, date(closed_ts,'unixepoch') AS day
                 FROM paper_positions
                WHERE status IN ('closed','exited') AND closed_ts IS NOT NULL
                  AND cost_usd > 0 AND entry_price > 0 AND entry_price < 1
             ORDER BY opened_ts ASC"""
        ).fetchall()

    # Resolved events (closed_ts, bucket, won) for the walk-forward window
    resolved = sorted(
        [(r["closed_ts"], pbucket(r["p"]), 1 if (r["pnl_usd"] if False else r["pnl"]) and r["pnl"] > 0 else 0)
         for r in rows],
        key=lambda x: x[0],
    )

    from collections import defaultdict
    kelly_pnl = kelly_staked = 0.0
    flat_pnl = flat_staked = 0.0
    bets_taken = bets_skipped = 0
    k_run = f_run = 0.0
    daily = defaultdict(lambda: [0.0, 0.0])  # day -> [kelly_pnl, flat_pnl]

    for r in rows:
        # win-rate history available BEFORE this bet opened (last WINDOW resolved)
        hist = [ev for ev in resolved if ev[0] <= r["opened_ts"]][-WINDOW:]
        b = pbucket(r["p"])
        bwins = sum(w for _, bk, w in hist if bk == b); bn = sum(1 for _, bk, _ in hist if bk == b)
        pwins = sum(w for _, _, w in hist); pn = len(hist)
        if bn >= MIN_BUCKET_N:
            q_lower = wilson_lower(bwins, bn)
        else:
            q_lower = max(0.0, wilson_lower(pwins, pn) - PENALTY)

        ret = r["pnl"] / r["cost"]   # realized per-$ return of this bet (outcome, stake-independent)

        # --- Kelly stake ---
        edge = (q_lower / r["p"] - 1.0) if r["p"] > 0 else -1
        stake = 0.0
        if edge >= EDGE_FLOOR and q_lower > r["p"]:
            kelly_f = (q_lower - r["p"]) / (1.0 - r["p"])
            stake = K * kelly_f * BANKROLL / C
            stake = min(stake, MAX_SINGLE * BANKROLL)
        if stake > 0:
            bets_taken += 1
            kelly_staked += stake; kelly_pnl += stake * ret; k_run += stake * ret
        else:
            bets_skipped += 1
        daily[r["day"]][0] += stake * ret

        # --- Flat baseline (1% of bankroll every bet) ---
        fstake = FLAT_FRAC * BANKROLL
        flat_staked += fstake; flat_pnl += fstake * ret; f_run += fstake * ret
        daily[r["day"]][1] += fstake * ret

    def maxdd(series):
        peak = series[0] if series else 0.0; dd = 0.0
        for v in series:
            peak = max(peak, v); dd = max(dd, peak - v)
        return dd

    days = sorted(daily)
    k_cum, f_cum = [], []; ka = fa = 0.0
    for d in days:
        ka += daily[d][0]; fa += daily[d][1]; k_cum.append(round(ka, 2)); f_cum.append(round(fa, 2))

    return {
        "note": "Walk-forward sim of the Kelly sizer (Wilson q_lower, edge floor 2%, half-Kelly, C=4) vs flat 1%, on a $1000 reference bankroll. Same real outcomes, different stakes. No real bets.",
        "resolved": len(rows),
        "kelly": {
            "pnl": round(kelly_pnl, 2), "staked": round(kelly_staked, 2),
            "roi_pct": round(100 * kelly_pnl / kelly_staked, 2) if kelly_staked else 0,
            "max_drawdown": round(maxdd(k_cum), 2),
            "bets_taken": bets_taken, "bets_skipped": bets_skipped,
        },
        "flat": {
            "pnl": round(flat_pnl, 2), "staked": round(flat_staked, 2),
            "roi_pct": round(100 * flat_pnl / flat_staked, 2) if flat_staked else 0,
            "max_drawdown": round(maxdd(f_cum), 2),
        },
        "curve": {"days": days, "kelly": k_cum, "flat": f_cum},
    }


@app.get("/api/merged_sim")
def merged_sim():
    """The merge: your friend's Kelly sizing engine, but q_lower stratified by
    CONVICTION tier (str<=4 / str5 / str6+) instead of price bucket — because our
    edge is conviction-conditional, not price-cheapness. Walk-forward, no lookahead.
    Compared to flat 1% and to the friend's price-bucket Kelly. No real bets."""
    import math

    K = 0.5; EDGE_FLOOR = 0.02; C = 4
    MAX_SINGLE = 0.05; MIN_BUCKET_N = 50; PENALTY = 0.02; WINDOW = 400
    BANKROLL = 1000.0; FLAT_FRAC = 0.01

    def wilson_lower(wins, n, z=1.96):
        if n == 0:
            return 0.0
        q = wins / n
        denom = 1 + z * z / n
        center = q + z * z / (2 * n)
        margin = z * math.sqrt(q * (1 - q) / n + z * z / (4 * n * n))
        return (center - margin) / denom

    def tier(s):
        s = s or 0
        return 4 if s <= 4 else (5 if s == 5 else 6)   # str6 = str6+

    with db() as c:
        rows = c.execute(
            """SELECT entry_price AS p, cost_usd AS cost, pnl_usd AS pnl,
                      signal_strength AS s, opened_ts, closed_ts, date(closed_ts,'unixepoch') AS day
                 FROM paper_positions
                WHERE status IN ('closed','exited') AND closed_ts IS NOT NULL
                  AND cost_usd > 0 AND entry_price > 0 AND entry_price < 1
             ORDER BY opened_ts ASC"""
        ).fetchall()

    resolved = sorted(
        [(r["closed_ts"], tier(r["s"]), 1 if r["pnl"] > 0 else 0) for r in rows],
        key=lambda x: x[0],
    )

    from collections import defaultdict
    m_pnl = m_staked = f_pnl = f_staked = 0.0
    taken = skipped = 0
    daily = defaultdict(lambda: [0.0, 0.0])
    by_tier = defaultdict(lambda: {"n": 0, "taken": 0, "pnl": 0.0, "staked": 0.0})

    for r in rows:
        hist = [ev for ev in resolved if ev[0] <= r["opened_ts"]][-WINDOW:]
        t = tier(r["s"])
        twins = sum(w for _, tk, w in hist if tk == t); tn = sum(1 for _, tk, _ in hist if tk == t)
        pwins = sum(w for _, _, w in hist); pn = len(hist)
        q_lower = wilson_lower(twins, tn) if tn >= MIN_BUCKET_N else max(0.0, wilson_lower(pwins, pn) - PENALTY)

        ret = r["pnl"] / r["cost"]
        edge = (q_lower / r["p"] - 1.0)
        stake = 0.0
        if edge >= EDGE_FLOOR and q_lower > r["p"]:
            kelly_f = (q_lower - r["p"]) / (1.0 - r["p"])
            stake = min(K * kelly_f * BANKROLL / C, MAX_SINGLE * BANKROLL)

        bt = by_tier[t]; bt["n"] += 1
        if stake > 0:
            taken += 1; m_staked += stake; m_pnl += stake * ret
            bt["taken"] += 1; bt["pnl"] += stake * ret; bt["staked"] += stake
        else:
            skipped += 1
        daily[r["day"]][0] += stake * ret

        fstake = FLAT_FRAC * BANKROLL
        f_staked += fstake; f_pnl += fstake * ret
        daily[r["day"]][1] += fstake * ret

    def maxdd(series):
        peak = series[0] if series else 0.0; dd = 0.0
        for v in series:
            peak = max(peak, v); dd = max(dd, peak - v)
        return dd

    days = sorted(daily)
    m_cum, f_cum = [], []; ma = fa = 0.0
    for d in days:
        ma += daily[d][0]; fa += daily[d][1]; m_cum.append(round(ma, 2)); f_cum.append(round(fa, 2))

    return {
        "note": "The merge: friend's Kelly engine + q_lower stratified by conviction tier (str<=4/5/6+). Walk-forward on real trades, $1000 bankroll. No real bets.",
        "resolved": len(rows),
        "merged": {
            "pnl": round(m_pnl, 2), "staked": round(m_staked, 2),
            "roi_pct": round(100 * m_pnl / m_staked, 2) if m_staked else 0,
            "max_drawdown": round(maxdd(m_cum), 2), "bets_taken": taken, "bets_skipped": skipped,
        },
        "flat": {
            "pnl": round(f_pnl, 2), "staked": round(f_staked, 2),
            "roi_pct": round(100 * f_pnl / f_staked, 2) if f_staked else 0,
            "max_drawdown": round(maxdd(f_cum), 2),
        },
        "by_tier": [
            {"tier": ("str6+" if k == 6 else f"str{k}"), "trades": by_tier[k]["n"],
             "taken": by_tier[k]["taken"], "pnl": round(by_tier[k]["pnl"], 2),
             "staked": round(by_tier[k]["staked"], 2)}
            for k in sorted(by_tier)
        ],
        "curve": {"days": days, "merged": m_cum, "flat": f_cum},
    }


@app.get("/merged")
def merged_dashboard():
    return FileResponse(BASE / "web" / "merged.html")


@app.get("/hybrid")
def hybrid_dashboard():
    return FileResponse(BASE / "web" / "hybrid.html")


@app.get("/api/hybrid_sim")
def hybrid_sim(days: int = 30, dd_half: float = 0.25, dd_pause: float = 0.35):
    """The hybrid: keep what's proven (conviction-weighted sizing) + add the friend's
    RISK controls (circuit breakers, exposure caps), drop the Kelly EV floor. Event-driven
    walk-forward with a compounding $1000 bankroll so drawdown-triggered breakers are real.
    Compared to flat sizing run through the same engine. Windowed to the last `days` so it
    tests the CURRENT strategy, not the pre-cleanup regime. No real bets."""
    BASE_FRAC = 0.015          # str4 bets ~1.5% of bankroll
    MAX_SINGLE = 0.05          # cap per bet
    MAX_TOTAL = 0.20           # cap total open exposure
    DD_HALF = dd_half          # drawdown -> halve sizing (default -15%)
    DD_PAUSE = dd_pause        # drawdown -> pause betting (default -25%)
    START = 1000.0

    def conv_mult(s):
        s = s or 0
        return 1.0 if s <= 4 else (1.5 if s == 5 else 2.0)

    cutoff = int(time.time()) - max(days, 1) * 86400
    with db() as c:
        rows = c.execute(
            """SELECT id, entry_price AS p, cost_usd AS cost, pnl_usd AS pnl,
                      signal_strength AS s, opened_ts, closed_ts, date(closed_ts,'unixepoch') AS day
                 FROM paper_positions
                WHERE status IN ('closed','exited') AND closed_ts IS NOT NULL
                  AND cost_usd > 0 AND entry_price > 0 AND closed_ts >= ?
             ORDER BY opened_ts ASC""", (cutoff,)
        ).fetchall()

    # Event stream: opens and closes interleaved by time (process closes first at ties)
    events = []
    for r in rows:
        events.append((r["opened_ts"], 1, r))    # 1 = open (after closes)
        events.append((r["closed_ts"], 0, r))    # 0 = close
    events.sort(key=lambda e: (e[0], e[1]))

    def new_book():
        return {"bank": START, "peak": START, "exp": 0.0, "open": {}, "maxdd": 0.0,
                "taken": 0, "skipped": 0, "trips": 0, "curve": {}}

    hy = new_book(); fl = new_book()

    def on_close(bk, r):
        st = bk["open"].pop(r["id"], None)
        if st is None:
            return
        bk["bank"] += st * (r["pnl"] / r["cost"])
        bk["exp"] -= st
        bk["peak"] = max(bk["peak"], bk["bank"])
        bk["maxdd"] = max(bk["maxdd"], (bk["peak"] - bk["bank"]) / bk["peak"])
        bk["curve"][r["day"]] = bk["bank"]

    for ts, typ, r in events:
        if typ == 0:
            on_close(hy, r); on_close(fl, r)
            continue
        # --- open: hybrid (conviction + breakers + caps) ---
        dd = (hy["peak"] - hy["bank"]) / hy["peak"]
        factor = 0.0 if dd >= DD_PAUSE else (0.5 if dd >= DD_HALF else 1.0)
        if factor < 1.0 and dd >= DD_HALF:
            hy["trips"] += 1
        stake = BASE_FRAC * conv_mult(r["s"]) * hy["bank"] * factor
        stake = min(stake, MAX_SINGLE * hy["bank"], max(0.0, MAX_TOTAL * hy["bank"] - hy["exp"]))
        if stake > 0:
            hy["open"][r["id"]] = stake; hy["exp"] += stake; hy["taken"] += 1
        else:
            hy["skipped"] += 1
        # --- open: flat baseline (same base frac, no conviction, no breakers) ---
        fstake = BASE_FRAC * fl["bank"]
        fstake = min(fstake, max(0.0, MAX_TOTAL * fl["bank"] - fl["exp"]))
        if fstake > 0:
            fl["open"][r["id"]] = fstake; fl["exp"] += fstake

    def summarize(bk):
        days = sorted(bk["curve"])
        return {
            "final_bankroll": round(bk["bank"], 2),
            "return_pct": round(100 * (bk["bank"] - START) / START, 2),
            "max_drawdown_pct": round(100 * bk["maxdd"], 2),
            "bets_taken": bk["taken"], "bets_skipped": bk["skipped"],
            "breaker_trips": bk["trips"],
            "curve_days": days,
            "curve": [round(bk["curve"][d], 2) for d in days],
        }

    return {
        "note": f"Hybrid: conviction-weighted sizing + circuit breakers (-{dd_half*100:.0f}% halve, -{dd_pause*100:.0f}% pause) + caps (5%/20%). Compounding $1000 bankroll, walk-forward on the last {days} days (current strategy). No real bets.",
        "window_days": days,
        "resolved": len(rows),
        "hybrid": summarize(hy),
        "flat": summarize(fl),
    }


@app.get("/latency")
def latency_dashboard():
    return FileResponse(BASE / "web" / "latency.html")


@app.get("/timing")
def timing_dashboard():
    return FileResponse(BASE / "web" / "timing.html")


@app.get("/api/timing")
def timing():
    """Pre-game vs in-play performance. A bet is IN-PLAY if we opened it after the
    scheduled game_start_ts. Discovered that ~2/3 of bets are in-play and far weaker."""
    from collections import defaultdict
    with db() as c:
        rows = c.execute(
            """SELECT opened_ts, game_start_ts, pnl_usd, cost_usd, signal_strength AS s, category,
                      (opened_ts - game_start_ts) AS lead
                 FROM paper_positions
                WHERE status IN ('closed','exited') AND COALESCE(game_start_ts,0) > 0 AND cost_usd > 0"""
        ).fetchall()
        total = c.execute("SELECT COUNT(*) FROM paper_positions WHERE status IN ('closed','exited')").fetchone()[0]

    def agg(items):
        n = len(items)
        if not n:
            return {"n": 0, "win_rate": 0, "pnl": 0, "roi_pct": 0}
        w = sum(1 for r in items if (r["pnl_usd"] or 0) > 0)
        pnl = sum(r["pnl_usd"] or 0 for r in items); cost = sum(r["cost_usd"] for r in items)
        return {"n": n, "win_rate": round(100 * w / n, 1), "pnl": round(pnl, 2),
                "roi_pct": round(100 * pnl / cost, 2) if cost else 0}

    pre = [r for r in rows if r["opened_ts"] <= r["game_start_ts"]]
    live = [r for r in rows if r["opened_ts"] > r["game_start_ts"]]

    # lead-time bands (how early pre-game / how late in-play)
    bands = [(-1e12, -3600, "3h+ in-play"), (-3600, -600, "10–60m in-play"),
             (-600, 0, "0–10m in-play"), (0, 600, "0–10m pre-game"),
             (600, 3600, "10–60m pre-game"), (3600, 1e12, "1h+ pre-game")]
    by_lead = []
    for lo, hi, lbl in bands:
        sub = [r for r in rows if lo <= r["lead"] < hi]
        by_lead.append({"band": lbl, **agg(sub)})

    return {
        "note": "A bet is IN-PLAY if opened after the scheduled kickoff. From game_start_ts (backfilled + now captured live).",
        "resolved_with_timing": len(rows), "resolved_total": total,
        "pre_game": agg(pre), "in_play": agg(live),
        "pct_in_play": round(100 * len(live) / len(rows), 1) if rows else 0,
        "by_lead": by_lead,
    }


@app.get("/clv")
def clv_dashboard():
    return FileResponse(BASE / "web" / "clv.html")


@app.get("/api/clv")
def clv():
    """Closing Line Value — did the price move toward us after entry (skill) or did
    we buy the top (exit liquidity)? Reads clv_scores (built by clv_backfill.py).
    'extreme' closes (market resolved before our date marker) are excluded as unreliable."""
    from collections import defaultdict
    import statistics as st

    with db() as c:
        try:
            reliable = c.execute("SELECT * FROM clv_scores WHERE flag=''").fetchall()
            total = c.execute("SELECT COUNT(*) FROM clv_scores").fetchone()[0]
            flagged = total - len(reliable)
        except Exception:
            return {"ready": False, "note": "CLV not computed yet — run clv_backfill.py."}

    if not reliable:
        return {"ready": False, "scored": total, "flagged": flagged,
                "note": "Backfill in progress or no reliable rows yet."}

    clvs = [r["clv"] for r in reliable]
    pos = [r for r in reliable if r["clv"] > 0]
    neg = [r for r in reliable if r["clv"] <= 0]

    def roi(items):
        cost = sum(abs(x["entry_price"]) for x in items) or 1  # proxy scale
        return round(100 * sum(x["pnl_usd"] or 0 for x in items) / (sum(1 for _ in items) or 1), 2)

    # Does CLV predict our PnL? avg PnL of positive-CLV vs negative-CLV bets
    avg_pnl_pos = round(sum(x["pnl_usd"] or 0 for x in pos) / len(pos), 2) if pos else 0
    avg_pnl_neg = round(sum(x["pnl_usd"] or 0 for x in neg) / len(neg), 2) if neg else 0

    # By conviction tier (out-of-sample check on the multipliers)
    tiers = defaultdict(list)
    for r in reliable:
        s = r["signal_strength"] or 0
        t = "str4-" if s <= 4 else ("str5" if s == 5 else "str6+")
        tiers[t].append(r["clv"])
    by_conv = [{"tier": t, "n": len(v), "avg_clv_cents": round(100 * st.mean(v), 2)}
               for t, v in sorted(tiers.items())]

    # By leader — skill classification
    leaders = defaultdict(list)
    for r in reliable:
        if r["leader_wallet"]:
            leaders[r["leader_wallet"]].append(r)
    leader_rows = []
    for w, items in leaders.items():
        if len(items) < 5:
            continue
        avg_clv = st.mean(x["clv"] for x in items)
        avg_pnl = sum(x["pnl_usd"] or 0 for x in items) / len(items)
        cls = "SKILLED" if avg_clv > 0.01 else ("HARMFUL" if avg_clv < -0.01 else "luck/neutral")
        leader_rows.append({"wallet": w[:10] + "…", "n": len(items),
                            "avg_clv_cents": round(100 * avg_clv, 2),
                            "avg_pnl": round(avg_pnl, 2), "class": cls})
    leader_rows.sort(key=lambda x: -x["avg_clv_cents"])

    # verification sample (mandatory hand-check)
    with db() as c:
        sample = c.execute(
            "SELECT position_id, entry_price, close_price, clv, market_title FROM clv_scores WHERE flag='' ORDER BY RANDOM() LIMIT 5"
        ).fetchall()

    return {
        "ready": True,
        "note": "CLV = price just before the event minus our entry. Positive = the market moved toward us (skill); negative = we were exit liquidity. 'Extreme' closes excluded.",
        "scored": total, "reliable": len(reliable), "flagged": flagged,
        "overall": {
            "avg_clv_cents": round(100 * st.mean(clvs), 2),
            "pct_positive": round(100 * len(pos) / len(reliable), 1),
            "median_clv_cents": round(100 * st.median(clvs), 2),
        },
        "clv_predicts_pnl": {"pos_clv_avg_pnl": avg_pnl_pos, "neg_clv_avg_pnl": avg_pnl_neg},
        "by_conviction": by_conv,
        "leaders_skilled": [r for r in leader_rows if r["class"] == "SKILLED"][:12],
        "leaders_harmful": [r for r in leader_rows if r["class"] == "HARMFUL"][-12:],
        "leader_counts": {
            "skilled": sum(1 for r in leader_rows if r["class"] == "SKILLED"),
            "harmful": sum(1 for r in leader_rows if r["class"] == "HARMFUL"),
            "neutral": sum(1 for r in leader_rows if r["class"] == "luck/neutral"),
        },
        "verify_sample": [dict(r) for r in sample],
    }


@app.get("/api/latency_decay")
def latency_decay():
    """Is the copy edge perishable? Joins each resolved copied bet's OUTCOME to the
    latency (seconds between leader's trade and our observation) and slippage (our fill
    vs the leader's price) we already store. Buckets PnL/win-rate by both to find the
    'cliff' — the threshold past which copying stops paying. All from existing data."""
    with db() as c:
        rows = c.execute(
            """SELECT s.leader_price AS lp, s.our_hypo_price AS fp,
                      s.our_hypo_slippage_bps AS slip, s.leader_ts AS lts, s.observed_ts AS ots,
                      pp.pnl_usd AS pnl, pp.cost_usd AS cost
                 FROM paper_positions pp
                 JOIN signals s ON pp.signal_id = s.id
                WHERE pp.status IN ('closed','exited') AND pp.closed_ts IS NOT NULL
                  AND pp.cost_usd > 0 AND s.leader_ts > 0 AND s.observed_ts > 0"""
        ).fetchall()

    def agg(items):
        n = len(items)
        if n == 0:
            return {"n": 0, "win_rate": 0, "avg_pnl": 0, "roi_pct": 0}
        wins = sum(1 for x in items if x["pnl"] > 0)
        pnl = sum(x["pnl"] for x in items); cost = sum(x["cost"] for x in items)
        return {"n": n, "win_rate": round(100 * wins / n, 1),
                "avg_pnl": round(pnl / n, 2), "roi_pct": round(100 * pnl / cost, 2) if cost else 0}

    from collections import defaultdict
    lat_bands = [(0, 30, "<30s"), (30, 60, "30–60s"), (60, 120, "1–2m"),
                 (120, 300, "2–5m"), (300, 1e12, "5m+")]
    slip_bands = [(-1e9, -50, "≤−0.5¢ (favorable)"), (-50, 0, "−0.5–0¢"), (0, 50, "0–0.5¢"),
                  (50, 100, "0.5–1¢"), (100, 200, "1–2¢"), (200, 1e9, "2¢+ (adverse)")]

    lat = defaultdict(list); slp = defaultdict(list)
    for r in rows:
        latency = r["ots"] - r["lts"]
        for lo, hi, lbl in lat_bands:
            if lo <= latency < hi:
                lat[lbl].append(r); break
        s = r["slip"] if r["slip"] is not None else 0
        for lo, hi, lbl in slip_bands:
            if lo <= s < hi:
                slp[lbl].append(r); break

    return {
        "note": "Copied bets scored by how stale the signal was (latency) and how far the price moved before our fill (slippage). Finds the point where copying stops paying. From existing data — no fetch.",
        "resolved": len(rows),
        "overall": agg(rows),
        "by_latency": [{"band": lbl, **agg(lat.get(lbl, []))} for _, _, lbl in lat_bands],
        "by_slippage": [{"band": lbl, **agg(slp.get(lbl, []))} for _, _, lbl in slip_bands],
    }


@app.get("/api/daily_pnl")
def daily_pnl():
    """Daily P&L breakdown for bar chart."""
    with db() as c:
        rows = c.execute(
            """SELECT date(closed_ts, 'unixepoch') as day,
                      COALESCE(SUM(pnl_usd), 0) as pnl,
                      COUNT(*) as trades,
                      SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
                 FROM paper_positions
                WHERE status IN ('closed', 'exited')
             GROUP BY day
             ORDER BY day ASC"""
        ).fetchall()
    return {"daily": [{"day": r[0], "pnl": round(r[1], 2), "trades": r[2], "wins": r[3]} for r in rows]}


@app.get("/api/live_positions")
async def live_positions():
    """Open positions enriched with current CLOB mid-price and unrealized P&L."""
    with db() as c:
        rows = c.execute(
            """SELECT id, market, market_title, market_slug, outcome, side,
                      entry_price, shares, cost_usd, signal_strength,
                      opened_ts, end_date, category, weighted_conviction
                 FROM paper_positions WHERE status='open'"""
        ).fetchall()

    positions = [dict(r) for r in rows]
    if not positions:
        return {"positions": []}

    async with httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "copy-bot/0.1"}) as client:
        for pos in positions:
            pos["current_price"] = None
            pos["unrealized_pnl"] = None
            try:
                mkt_r = await client.get(f"https://clob.polymarket.com/markets/{pos['market']}")
                if mkt_r.status_code != 200:
                    continue
                tokens = mkt_r.json().get("tokens", [])
                token_id = None
                for t in tokens:
                    if (t.get("outcome") or "").lower() == (pos["outcome"] or "").lower():
                        token_id = t.get("token_id")
                        break
                if not token_id and tokens:
                    token_id = tokens[0].get("token_id")
                if not token_id:
                    continue
                book_r = await client.get("https://clob.polymarket.com/book", params={"token_id": token_id})
                if book_r.status_code != 200:
                    continue
                book = book_r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 0.0
                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else (best_bid or best_ask)
                if mid > 0:
                    pos["current_price"] = round(mid, 4)
                    pos["unrealized_pnl"] = round(pos["shares"] * mid - pos["cost_usd"], 2)
            except Exception:
                pass

    return {"positions": positions}


@app.get("/api/buckets")
def buckets():
    """(liquidity, latency) -> mean slippage in bps and count."""
    with db() as c:
        rows = c.execute(
            """SELECT liquidity_bucket, latency_bucket,
                      COUNT(*) AS n,
                      AVG(our_hypo_slippage_bps) AS avg_slip
                 FROM signals
                WHERE our_hypo_price > 0
             GROUP BY liquidity_bucket, latency_bucket"""
        ).fetchall()
    return {"buckets": [dict(r) for r in rows]}


@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events: push new signal rows as they arrive."""
    async def gen():
        last_id = 0
        with db() as c:
            row = c.execute("SELECT MAX(id) FROM signals").fetchone()
            last_id = row[0] or 0
        while True:
            if await request.is_disconnected():
                break
            with db() as c:
                rows = c.execute(
                    "SELECT * FROM signals WHERE id > ? ORDER BY id ASC LIMIT 50",
                    (last_id,),
                ).fetchall()
            for r in rows:
                d = dict(r)
                last_id = d["id"]
                yield f"data: {json.dumps(d)}\n\n"
            # keep-alive
            yield ":\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/stats")
def stats():
    """Top-line KPIs for the header."""
    with db() as c:
        nlead = c.execute("SELECT COUNT(*) FROM leaders WHERE excluded_reason IS NULL").fetchone()[0]
        ntot  = c.execute("SELECT COUNT(*) FROM leaders").fetchone()[0]
        nsig  = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        ncopy = c.execute("SELECT COUNT(*) FROM signals WHERE filter_status='copy'").fetchone()[0]
        last_sig = c.execute("SELECT MAX(observed_ts) FROM signals").fetchone()[0] or 0
        cash_row = c.execute("SELECT cash_usd FROM paper_bankroll WHERE id=1").fetchone()
        cash = cash_row[0] if cash_row else 1000.0
        exp_row = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM paper_positions WHERE status='open'"
        ).fetchone()
        exposure = exp_row[0] if exp_row else 0.0
    return {
        "tracked_leaders": nlead,
        "total_seeds_scored": ntot,
        "signals_recorded": nsig,
        "copy_signals": ncopy,
        "last_signal_ts": last_sig,
        "bankroll_equity": round(cash + exposure, 2),
        "bankroll_cash": round(cash, 2),
    }


@app.get("/api/shadow_analytics")
def shadow_analytics():
    """Compare actual strategy vs shadow filters (min5, no-last-2h sports).
    Returns win rate, trade count, and estimated PnL for each strategy variant.
    Only uses trades where shadow columns are populated (v2.8+).
    """
    return _cached("shadow_analytics", CACHE_TTL, _shadow_analytics_query)

def _shadow_analytics_query():
    with db() as c:
        # Check if shadow columns exist
        cols = {r[1] for r in c.execute("PRAGMA table_info(signals)")}
        if "shadow_min5" not in cols:
            return {"available": False, "reason": "shadow columns not yet in DB — restart worker to migrate"}

        # All closed/exited positions joined to their signal's shadow flags
        rows = c.execute("""
            SELECT pp.pnl_usd, pp.cost_usd, pp.status,
                   s.conviction_count, s.category,
                   COALESCE(s.shadow_min5, 0)      AS shadow_min5,
                   COALESCE(s.shadow_no_last2h, 0) AS shadow_no_last2h
              FROM paper_positions pp
              JOIN signals s ON pp.signal_id = s.id
             WHERE pp.status IN ('closed', 'exited')
               AND s.shadow_min5 IS NOT NULL
        """).fetchall()

    if not rows:
        return {"available": False, "reason": "no shadow-tagged positions yet — data accumulates from next signal"}

    def _stats(subset):
        if not subset: return {"trades": 0, "win_rate": 0, "pnl": 0}
        wins = sum(1 for r in subset if r[0] > 0)
        pnl  = sum(r[0] for r in subset)
        return {
            "trades":   len(subset),
            "win_rate": round(wins / len(subset) * 100, 1),
            "pnl":      round(pnl, 2),
        }

    actual   = rows                                            # all copied trades
    min5     = [r for r in rows if r[5] == 1]                 # shadow: would min5 have copied?
    no_last2h = [r for r in rows if r[6] == 1]               # shadow: no-last-2h block
    both     = [r for r in rows if r[5] == 1 and r[6] == 1]  # both filters combined

    # Trades that would have been SKIPPED by each filter
    skipped_min5      = [r for r in rows if r[5] == 0]
    skipped_no_last2h = [r for r in rows if r[6] == 0]

    return {
        "available": True,
        "total_tagged": len(rows),
        "actual":         _stats(actual),
        "shadow_min5":    _stats(min5),
        "shadow_no_last2h": _stats(no_last2h),
        "shadow_both":    _stats(both),
        "skipped_by_min5":      _stats(skipped_min5),
        "skipped_by_no_last2h": _stats(skipped_no_last2h),
    }
