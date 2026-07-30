"""Worker process — runs all loops concurrently.

Run with: python worker.py
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time

import httpx

import bot_metrics
import notify
from exit_monitor import exit_check_once, EXIT_CHECK_INTERVAL
from leader_ranker import ensure_db, rank_seeds
from leaderboard_refresh import refresh_once, REFRESH_INTERVAL as LB_REFRESH_INTERVAL
from paper_book import ensure_paper_tables
from poly_client import PolyClient
from resolver import resolve_once
from watcher import poll_once

log = logging.getLogger(__name__)

POLL_INTERVAL = 15            # seconds
RESOLVER_INTERVAL = 900       # 15 minutes
RANKER_INTERVAL = 6 * 3600    # 6 hours
MAINTENANCE_INTERVAL = 6 * 3600   # prune + disk check every 6h
SIGNAL_RETENTION_DAYS = 30    # signals older than this are pruned (conviction only needs 6h)
DISK_WARN_PCT = 85            # Telegram alert when disk exceeds this


def _poll_pass():
    """Run one poll cycle — creates all objects in the thread to avoid SQLite thread-safety issues."""
    conn = ensure_db()
    ensure_paper_tables(conn)
    # Pool sized for the parallel poll (POLL_WORKERS concurrent requests per client).
    _limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    http = httpx.Client(timeout=10.0, limits=_limits, headers={"User-Agent": "copy-bot/0.1"})
    poly = PolyClient(limits=_limits)
    try:
        with bot_metrics.trace("poll.cycle"):
            return poll_once(conn, http, poly)
    finally:
        http.close()
        poly.close()
        conn.close()


async def fast_loop():
    """Wallet polling every 15s — poll runs in a thread, objects created per-thread."""
    while True:
        start = time.monotonic()
        ok = True
        try:
            n = await asyncio.to_thread(_poll_pass)
            if n:
                log.info("poll: %d new signal(s)", n)
                bot_metrics.distribution("signal.new_per_cycle", n)
        except Exception as e:
            ok = False
            log.exception("poll cycle failed: %s", e)
        elapsed = time.monotonic() - start
        bot_metrics.count("poll.cycle", ok=str(ok))
        bot_metrics.distribution("poll.cycle_seconds", elapsed)
        await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))


def _resolver_pass():
    """Run one resolver cycle with its own DB connection (thread-safe).
    Resolves both the real paper book and the shadow book."""
    conn = ensure_db()
    http = httpx.Client(headers={"User-Agent": "copy-bot/0.1"})
    try:
        with bot_metrics.trace("resolver.cycle"):
            n = resolve_once(conn, http)
            try:
                from shadow_book import ensure_shadow_table, resolve_shadow_once
                ensure_shadow_table(conn)
                ns = resolve_shadow_once(conn, http)
                if ns:
                    log.info("shadow resolver: closed %d hypothetical position(s)", ns)
            except Exception as e:
                log.warning("shadow resolver failed: %s", e)
            return n
    finally:
        http.close()
        conn.close()


async def resolver_loop():
    """Check open paper positions for resolution every 15min."""
    await asyncio.sleep(60)
    while True:
        try:
            with bot_metrics.cron("resolver", 15):
                n = await asyncio.to_thread(_resolver_pass)
            if n:
                log.info("resolver: closed %d position(s)", n)
        except Exception as e:
            log.exception("resolver cycle failed: %s", e)
        await asyncio.sleep(RESOLVER_INTERVAL)


def _exit_monitor_pass():
    """Run one exit monitor cycle with its own DB connection (thread-safe)."""
    conn = ensure_db()
    poly = PolyClient()
    http = httpx.Client(headers={"User-Agent": "copy-bot/0.1"})
    try:
        with bot_metrics.trace("exit_monitor.cycle"):
            return exit_check_once(conn, poly, http)
    finally:
        poly.close()
        http.close()
        conn.close()


async def exit_monitor_loop():
    """Check if conviction leaders have bailed — every 5min."""
    await asyncio.sleep(120)
    while True:
        try:
            with bot_metrics.cron("exit-monitor", 5):
                n = await asyncio.to_thread(_exit_monitor_pass)
            if n:
                log.info("exit monitor: exited %d position(s)", n)
        except Exception as e:
            log.exception("exit monitor failed: %s", e)
        await asyncio.sleep(EXIT_CHECK_INTERVAL)


async def leaderboard_refresh_loop():
    """Pull fresh leaderboard data every hour and re-rank new wallets."""
    await asyncio.sleep(300)
    while True:
        try:
            with bot_metrics.cron("leaderboard-refresh", 60, max_runtime=15):
                summary = await asyncio.to_thread(refresh_once)
            # New wallets are merged into seeds.json here; scoring is handled by the
            # 6h ranker (slow_loop). We intentionally do NOT re-rank all ~4600 seeds
            # inline — that took ~2-3h and blocked this hourly loop (missed check-ins).
            log.info("leaderboard: refreshed, %d new wallets merged (total=%d) — scored on next ranker pass",
                     summary["new_wallets"], summary["total"])
        except Exception as e:
            log.exception("leaderboard refresh failed: %s", e)
        await asyncio.sleep(LB_REFRESH_INTERVAL)


async def daily_summary_loop():
    """Send a daily PnL summary via Telegram at midnight UTC."""
    import time
    while True:
        # Sleep until next midnight UTC
        now = time.time()
        next_midnight = (now // 86400 + 1) * 86400
        await asyncio.sleep(next_midnight - now)
        try:
            conn = ensure_db()
            ensure_paper_tables(conn)
            eq = conn.execute(
                "SELECT cash_usd FROM paper_bankroll WHERE id=1"
            ).fetchone()
            cash = eq[0] if eq else 0
            exposure = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM paper_positions WHERE status='open'"
            ).fetchone()[0]
            equity = cash + exposure
            now_ts = int(time.time())
            since_day  = now_ts - 86400
            since_week = now_ts - 7 * 86400
            daily_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl_usd),0) FROM paper_positions WHERE status IN ('closed','exited') AND closed_ts >= ?",
                (since_day,)
            ).fetchone()[0]
            week_row = conn.execute(
                """SELECT COALESCE(SUM(pnl_usd),0),
                          COUNT(*),
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
                     FROM paper_positions
                    WHERE status IN ('closed','exited') AND closed_ts >= ?""",
                (since_week,)
            ).fetchone()
            week_pnl, week_trades, week_wins = week_row[0], week_row[1] or 0, week_row[2] or 0
            open_pos = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status='open'"
            ).fetchone()[0]
            total_trades = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status IN ('closed','exited')"
            ).fetchone()[0]
            all_wins = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status IN ('closed','exited') AND pnl_usd > 0"
            ).fetchone()[0]
            all_time_wr = (all_wins / total_trades * 100) if total_trades else 0.0
            # Best/worst open positions
            best_row = conn.execute(
                """SELECT market_title, entry_price, cost_usd
                     FROM paper_positions WHERE status='open'
                  ORDER BY entry_price DESC LIMIT 1"""
            ).fetchone()
            worst_row = conn.execute(
                """SELECT market_title, entry_price, cost_usd
                     FROM paper_positions WHERE status='open'
                  ORDER BY entry_price ASC LIMIT 1"""
            ).fetchone()
            best_open  = f"{(best_row[0] or '')[:30]} @{best_row[1]:.2f}" if best_row else ""
            worst_open = f"{(worst_row[0] or '')[:30]} @{worst_row[1]:.2f}" if worst_row else ""
            conn.close()
            notify.alert_daily_summary(
                equity, daily_pnl, open_pos, total_trades,
                week_pnl=week_pnl, week_trades=week_trades, week_wins=week_wins,
                all_time_win_rate=all_time_wr,
                best_open=best_open, worst_open=worst_open,
            )
            log.info("daily summary sent: equity=$%.2f daily_pnl=$%+.2f", equity, daily_pnl)
        except Exception as e:
            log.exception("daily summary failed: %s", e)


async def slow_loop():
    """Re-rank leaders every 6 hours."""
    conn = ensure_db()
    need_first_run = conn.execute("SELECT COUNT(*) FROM leaders").fetchone()[0] == 0
    conn.close()
    if need_first_run:
        log.info("leaders table empty — running initial ranker pass")
        await asyncio.to_thread(rank_seeds)
    while True:
        await asyncio.sleep(RANKER_INTERVAL)
        log.info("scheduled ranker refresh")
        try:
            # rank_seeds now fetches all ~4600 seeds in parallel (~30min); allow 60.
            with bot_metrics.cron("ranker", 360, max_runtime=60, margin=15):
                await asyncio.to_thread(rank_seeds)
        except Exception as e:
            log.exception("ranker refresh failed: %s", e)


def _maintenance_pass():
    """Prune old signals (keep positions forever) and report disk usage.
    signals only matter for the 6h conviction window, so >30d rows are dead weight —
    2M rows had grown the DB to 1.3GB and (via log flood) filled the disk."""
    conn = ensure_db()
    freed = 0
    try:
        cutoff = int(time.time()) - SIGNAL_RETENTION_DAYS * 86400
        cur = conn.execute("DELETE FROM signals WHERE observed_ts < ?", (cutoff,))
        conn.commit()
        freed = cur.rowcount
    finally:
        conn.close()
    total, used, free = shutil.disk_usage("/")
    return freed, 100 * used / total, free / 1e9


async def maintenance_loop():
    """Every 6h: prune old signals, check disk, Telegram-alert if disk is filling."""
    await asyncio.sleep(300)  # let the rest boot first
    while True:
        try:
            freed, pct, free_gb = await asyncio.to_thread(_maintenance_pass)
            if freed:
                log.info("maintenance: pruned %d signals >%dd old", freed, SIGNAL_RETENTION_DAYS)
            log.info("maintenance: disk %.0f%% used, %.1fGB free", pct, free_gb)
            if pct >= DISK_WARN_PCT:
                try:
                    notify.alert_system(
                        f"⚠️ DISK {pct:.0f}% FULL on polybot server "
                        f"({free_gb:.1f}GB free). DB writes will fail if it hits 100%."
                    )
                except Exception as e:
                    log.warning("disk alert send failed: %s", e)
        except Exception as e:
            log.exception("maintenance failed: %s", e)
        await asyncio.sleep(MAINTENANCE_INTERVAL)


async def main():
    await asyncio.gather(
        fast_loop(),
        resolver_loop(),
        exit_monitor_loop(),
        leaderboard_refresh_loop(),
        slow_loop(),
        daily_summary_loop(),
        maintenance_loop(),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [worker] %(message)s",
    )
    # Silence per-request HTTP logging — httpx logs a line for EVERY request (~600/min),
    # which flooded /var/log/syslog to 2GB+ and filled the disk (Jul 12 2026 outage).
    for _noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    # Error monitoring — no-op if SENTRY_DSN unset or sentry-sdk missing.
    from sentry_setup import init_sentry
    init_sentry("worker")

    log.info("starting worker (poll=%ds, resolver=%dm, exit=%dm, leaderboard=%dm, ranker=%dh)",
             POLL_INTERVAL, RESOLVER_INTERVAL // 60, EXIT_CHECK_INTERVAL // 60,
             LB_REFRESH_INTERVAL // 60, RANKER_INTERVAL // 3600)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker stopped")
    except Exception:
        # Ensure the fatal error reaches Sentry before the process exits.
        try:
            import sentry_sdk
            sentry_sdk.capture_exception()
            sentry_sdk.flush(timeout=5)
        except Exception:
            pass
        raise
