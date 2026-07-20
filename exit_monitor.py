"""Exit monitor — watches whether the leaders we copied are still holding.

For each open paper position:
  1. Conviction exit: if majority of leaders have placed SELL trades, we follow them out.
  2. Adverse price exit: if price drops below entry × threshold AND no conviction leader
     has been actively BUYING since we opened — leaders still buying = normal volatility,
     don't panic-exit.

Data shows 90% of our adverse exits happened while leaders were still buying or holding.
The leader-confirmation gate suppresses those false exits.

Position status flow:
  'open' → 'closed'   (market resolved, via resolver.py)
  'open' → 'exited'   (leaders bailed or price collapsed with no leader support)
  'open' → 'cancelled' (dedup cleanup, via cleanup_duplicates.py)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import httpx

import notify
from config import cfg
from poly_client import PolyClient, CLOB_API

log = logging.getLogger(__name__)

EXIT_CHECK_INTERVAL = 300  # 5 minutes
EXIT_THRESHOLD = 0.5       # fraction of conviction leaders that must have sold to trigger conviction exit


def _get_conviction_wallets(conn: sqlite3.Connection, signal_id: int) -> list[str]:
    """Get the wallets that formed the conviction for a signal."""
    row = conn.execute(
        "SELECT conviction_wallets FROM signals WHERE id=?", (signal_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        # Fallback: comma-separated string
        return [w.strip() for w in (row[0] or "").split(",") if w.strip()]


def _leader_has_sold(poly: PolyClient, wallet: str, market: str, since_ts: int) -> bool:
    """Check if a leader placed any SELL trades on this market since a timestamp. (API call)"""
    try:
        trades = poly.get_trades(wallet, limit=50, offset=0)
    except Exception as e:
        log.warning("trade check failed for %s: %s", wallet[:10], e)
        return False
    for t in trades:
        if t.market == market and t.side == "SELL" and t.timestamp >= since_ts:
            return True
    return False


def _any_leader_still_buying(
    conn: sqlite3.Connection,
    wallets: list[str],
    market: str,
    since_ts: int,
    window_hours: float = 2.0,
) -> bool:
    """Check the signals table: has any conviction leader placed a BUY on this market
    since we opened the position?

    Uses the local DB — no API call needed. If leaders are continuing to buy,
    the price dip is treated as normal volatility and we suppress the adverse exit.
    """
    if not wallets:
        return False
    cutoff = since_ts  # any buy since position opened
    placeholders = ",".join("?" * len(wallets))
    count = conn.execute(
        f"""SELECT COUNT(*) FROM signals
            WHERE market = ?
              AND side = 'BUY'
              AND leader_wallet IN ({placeholders})
              AND observed_ts > ?""",
        [market] + wallets + [cutoff],
    ).fetchone()[0]
    return count > 0


def _get_token_for_market(http: httpx.Client, market: str, outcome: str) -> str | None:
    """Fetch the token_id for a specific outcome of a market from CLOB."""
    try:
        r = http.get(f"{CLOB_API}/markets/{market}", timeout=5.0)
        r.raise_for_status()
        tokens = r.json().get("tokens", [])
        for tok in tokens:
            if tok.get("outcome", "").lower() == (outcome or "").lower():
                return tok.get("token_id") or tok.get("id")
        if tokens:
            return tokens[0].get("token_id") or tokens[0].get("id")
    except Exception:
        pass
    return None


def _get_current_price(http: httpx.Client, token_id: str) -> float | None:
    """Get the current mid-price from the order book."""
    try:
        r = http.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5.0)
        r.raise_for_status()
        book = r.json()
    except Exception:
        return None

    best_bid = 0.0
    best_ask = 1.0
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if bids:
        best_bid = max(float(b["price"]) for b in bids)
    if asks:
        best_ask = min(float(a["price"]) for a in asks)
    if best_bid > 0 and best_ask < 1:
        return (best_bid + best_ask) / 2
    if best_bid > 0:
        return best_bid
    if best_ask < 1:
        return best_ask
    return None


def _do_exit(conn: sqlite3.Connection, pos_id: int, shares: float, cost_usd: float,
             exit_price: float, reason: str) -> None:
    """Apply an exit to a paper position and update bankroll."""
    payout = shares * exit_price
    pnl = payout - cost_usd
    now = int(time.time())

    conn.execute(
        """UPDATE paper_positions
              SET status='exited', closed_ts=?, close_price=?, payout_usd=?, pnl_usd=?
            WHERE id=?""",
        (now, exit_price, payout, pnl, pos_id),
    )

    cash_row = conn.execute("SELECT cash_usd FROM paper_bankroll WHERE id=1").fetchone()
    if cash_row:
        conn.execute(
            "UPDATE paper_bankroll SET cash_usd=cash_usd+?, updated_at=? WHERE id=1",
            (payout, now),
        )

    cash = conn.execute("SELECT cash_usd FROM paper_bankroll WHERE id=1").fetchone()[0]
    exposure = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM paper_positions WHERE status='open'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO paper_bankroll_history(ts, cash_usd, open_exposure_usd, equity_usd) VALUES(?,?,?,?)",
        (now, cash, exposure, cash + exposure),
    )
    conn.commit()
    log.info("  pos #%d exited [%s]: close=%.3f payout=$%.2f pnl=$%+.2f",
             pos_id, reason, exit_price, payout, pnl)


def exit_check_once(conn: sqlite3.Connection, poly: PolyClient, http: httpx.Client) -> int:
    """One exit-monitoring pass. Returns # of positions exited."""
    rows = conn.execute(
        """SELECT pp.id, pp.signal_id, pp.market, pp.outcome, pp.side,
                  pp.opened_ts, pp.shares, pp.cost_usd, pp.entry_price
             FROM paper_positions pp
            WHERE pp.status='open'"""
    ).fetchall()
    if not rows:
        return 0

    adv_cfg = cfg.get("adverse_exit", {})
    adv_enabled   = adv_cfg.get("enabled", False)
    adv_threshold = adv_cfg.get("price_drop_threshold", 0.60)
    adv_min_age_h = adv_cfg.get("min_age_hours", 1.0)
    # Leader-confirmation gate: suppress adverse exit if leaders are still buying
    leader_confirm = adv_cfg.get("leader_confirmation", True)

    exited = 0
    for pos_id, signal_id, market, outcome, side, opened_ts, shares, cost_usd, entry_price in rows:
        exit_reason    = None
        exit_price_val = None

        conviction_wallets = _get_conviction_wallets(conn, signal_id)

        # ----------------------------------------------------------------
        # 1. Conviction exit: majority of leaders have sold → follow them
        # ----------------------------------------------------------------
        if len(conviction_wallets) >= 2:
            sellers = 0
            for wallet in conviction_wallets:
                if _leader_has_sold(poly, wallet, market, opened_ts):
                    sellers += 1
                time.sleep(0.1)

            fraction_sold = sellers / len(conviction_wallets)
            if fraction_sold >= EXIT_THRESHOLD:
                log.info("exit monitor: pos #%d — %d/%d leaders sold (%s)",
                         pos_id, sellers, len(conviction_wallets), market[:16])
                asset_row = conn.execute(
                    "SELECT our_hypo_price FROM signals WHERE id=?", (signal_id,)
                ).fetchone()
                exit_price_val = asset_row[0] if asset_row else 0.5
                exit_reason = f"conviction:{sellers}/{len(conviction_wallets)}_sold"

        # ----------------------------------------------------------------
        # 2. Adverse price exit: price collapsed AND leaders have gone quiet
        # ----------------------------------------------------------------
        if exit_reason is None and adv_enabled and entry_price:
            age_hours = (time.time() - opened_ts) / 3600
            if age_hours >= adv_min_age_h:
                token_id = _get_token_for_market(http, market, outcome)
                if token_id:
                    current_price = _get_current_price(http, token_id)
                    if current_price is not None and current_price < entry_price * adv_threshold:

                        # ---- Leader-confirmation gate ----
                        # If conviction leaders are still buying this market, the drop
                        # is normal volatility — they know something we don't. Hold.
                        if leader_confirm and conviction_wallets:
                            still_buying = _any_leader_still_buying(
                                conn, conviction_wallets, market, opened_ts
                            )
                            if still_buying:
                                log.info(
                                    "exit monitor: pos #%d price %.3f < %.3f×%.2f BUT "
                                    "leaders still buying — suppressing adverse exit (%s)",
                                    pos_id, current_price, entry_price, adv_threshold,
                                    market[:16],
                                )
                                continue  # skip this position — leaders say hold

                        log.info(
                            "exit monitor: pos #%d adverse price %.3f < %.3f×%.2f, "
                            "leaders not buying — exiting (%s)",
                            pos_id, current_price, entry_price, adv_threshold, market[:16],
                        )
                        title_row = conn.execute(
                            "SELECT market_title FROM paper_positions WHERE id=?", (pos_id,)
                        ).fetchone()
                        pnl_est = shares * current_price - cost_usd
                        notify.alert_adverse_exit(
                            market_title=title_row[0] if title_row else market[:40],
                            outcome=outcome, entry_price=entry_price,
                            current_price=current_price, pnl_usd=pnl_est,
                        )
                        exit_price_val = current_price
                        exit_reason = (
                            f"adverse_price:{current_price:.3f}<{entry_price:.3f}"
                            f"x{adv_threshold}_leaders_silent"
                        )

        if exit_reason is None:
            continue

        _do_exit(conn, pos_id, shares, cost_usd, exit_price_val, exit_reason)
        exited += 1

    return exited
