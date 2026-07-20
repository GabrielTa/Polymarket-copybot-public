"""Telegram notifications for the copy bot.

Sends alerts for:
  - Position opened (copy signal fired)
  - Position closed (won/lost with PnL)
  - Adverse price exit triggered
  - Leader auto-excluded
  - Daily PnL summary

Credentials loaded from .env (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID).
Silently no-ops if credentials are missing so the bot never crashes on notify failure.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Load .env manually (no dependency on python-dotenv)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
# TELEGRAM_CHAT_ID may be a single id or a comma-separated list — every id gets the alert.
TELEGRAM_CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
_API      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
_PHOTO_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"


def _send(text: str) -> None:
    """Fire-and-forget Telegram message to every configured chat id. Never raises."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            httpx.post(_API, json={"chat_id": chat_id, "text": text,
                                   "parse_mode": "HTML"}, timeout=5.0)
        except Exception as e:
            log.warning("telegram send to %s failed: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Alert helpers
# ---------------------------------------------------------------------------

def alert_system(text: str) -> None:
    """Operational/infra alert (disk full, etc.) — broadcasts to all chat ids."""
    _send(text)


def alert_position_opened(
    market_title: str,
    side: str,
    outcome: str,
    entry_price: float,
    size_usd: float,
    conviction: int,
    weighted_score: float,
    category: str,
) -> None:
    text = (
        f"📈 <b>POSITION OPENED</b>\n"
        f"{market_title[:60]}\n"
        f"  {side} {outcome} @ {entry_price:.3f}  ${size_usd:.2f}\n"
        f"  conviction: {conviction}×  score: {weighted_score:.2f}  [{category}]"
    )
    _send(text)


def alert_position_closed(
    market_title: str,
    outcome: str,
    entry_price: float,
    close_price: float,
    pnl_usd: float,
    cost_usd: float,
) -> None:
    result = "✅ WON" if pnl_usd > 0 else "❌ LOST"
    roi = (pnl_usd / cost_usd * 100) if cost_usd else 0
    text = (
        f"{result}  <b>{market_title[:60]}</b>\n"
        f"  {outcome}  entry {entry_price:.3f} → close {close_price:.3f}\n"
        f"  PnL: <b>${pnl_usd:+.2f}</b>  ({roi:+.1f}%)"
    )
    _send(text)


def alert_adverse_exit(
    market_title: str,
    outcome: str,
    entry_price: float,
    current_price: float,
    pnl_usd: float,
) -> None:
    drop_pct = ((current_price - entry_price) / entry_price * 100)
    text = (
        f"⚠️ <b>ADVERSE EXIT</b>\n"
        f"{market_title[:60]}\n"
        f"  {outcome}  entry {entry_price:.3f} → now {current_price:.3f}  ({drop_pct:+.1f}%)\n"
        f"  PnL: <b>${pnl_usd:+.2f}</b>"
    )
    _send(text)


def alert_leader_excluded(wallet: str, win_rate: float, roi: float, n_trades: int) -> None:
    text = (
        f"🚫 <b>LEADER AUTO-EXCLUDED</b>\n"
        f"  {wallet[:20]}…\n"
        f"  win rate: {win_rate:.1%}  ROI: {roi:+.1f}%  ({n_trades} trades)"
    )
    _send(text)


def alert_daily_summary(
    equity: float,
    daily_pnl: float,
    open_positions: int,
    total_trades: int,
    week_pnl: float = 0.0,
    week_trades: int = 0,
    week_wins: int = 0,
    all_time_win_rate: float = 0.0,
    best_open: str = "",
    worst_open: str = "",
) -> None:
    arrow = "📈" if daily_pnl >= 0 else "📉"
    week_arrow = "📈" if week_pnl >= 0 else "📉"
    week_wr = f"{week_wins}/{week_trades} ({round(week_wins/week_trades*100) if week_trades else 0}%)" if week_trades else "—"
    lines = [
        f"{arrow} <b>DAILY SUMMARY</b>",
        f"  Equity: <b>${equity:,.2f}</b>  (today: <b>{daily_pnl:+.2f}</b>)",
        f"  {week_arrow} This week: <b>{week_pnl:+.2f}</b>  |  W/L: {week_wr}",
        f"  All-time win rate: <b>{all_time_win_rate:.1f}%</b>  |  Total trades: {total_trades}",
        f"  Open positions: {open_positions}",
    ]
    if best_open:
        lines.append(f"  🟢 Best open: {best_open}")
    if worst_open:
        lines.append(f"  🔴 Worst open: {worst_open}")
    _send("\n".join(lines))
