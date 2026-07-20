"""Enhanced strategy analysis — tracks performance by strategy version,
Sharpe ratio, drawdown, leader quality impact, and entry price buckets.

Run: python analyze_strategy.py
"""
import sqlite3
import math
from pathlib import Path
from collections import defaultdict

DB = Path(__file__).parent / "data" / "copybot.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

positions = conn.execute("""
    SELECT pp.*, s.leader_wallet, s.conviction_count, s.conviction_wallets,
           s.filter_reason, l.score as leader_score, l.best_seed_rank
      FROM paper_positions pp
      LEFT JOIN signals s ON pp.signal_id = s.id
      LEFT JOIN leaders l ON s.leader_wallet = l.wallet
     WHERE pp.status IN ('closed', 'exited')
  ORDER BY pp.closed_ts ASC
""").fetchall()

print("=" * 70)
print("POLYMARKET COPY BOT — ENHANCED STRATEGY ANALYSIS")
print("=" * 70)

if not positions:
    print("No closed positions yet.")
    exit()

# ---- Overall ----
total = len(positions)
won = [p for p in positions if (p["pnl_usd"] or 0) > 0]
lost = [p for p in positions if (p["pnl_usd"] or 0) <= 0]
total_pnl = sum(p["pnl_usd"] or 0 for p in positions)
total_cost = sum(p["cost_usd"] for p in positions)

print(f"\nOverall: {total} closed trades")
print(f"  Won: {len(won)} ({len(won)/total*100:.0f}%) | Lost: {len(lost)} ({len(lost)/total*100:.0f}%)")
print(f"  Total PnL: ${total_pnl:+,.2f}")
print(f"  Total wagered: ${total_cost:,.2f}")
print(f"  ROI: {total_pnl/total_cost*100:+.1f}%" if total_cost > 0 else "")

# ---- Sharpe Ratio ----
pnls = [p["pnl_usd"] or 0 for p in positions]
if len(pnls) > 1:
    avg_pnl = sum(pnls) / len(pnls)
    std_pnl = math.sqrt(sum((x - avg_pnl) ** 2 for x in pnls) / (len(pnls) - 1))
    sharpe = (avg_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0  # annualized
    print(f"  Avg PnL per trade: ${avg_pnl:+.2f}")
    print(f"  Std dev: ${std_pnl:.2f}")
    print(f"  Sharpe ratio (annualized): {sharpe:.2f}")

# ---- Max Drawdown ----
cumulative = 0
peak = 0
max_dd = 0
dd_trades = []
for p in positions:
    cumulative += (p["pnl_usd"] or 0)
    if cumulative > peak:
        peak = cumulative
    dd = peak - cumulative
    if dd > max_dd:
        max_dd = dd
    dd_trades.append((cumulative, peak, dd))

print(f"  Max drawdown: ${max_dd:.2f}")
print(f"  Final equity curve: ${cumulative:+,.2f}")

# ---- By Strategy Version ----
versions = defaultdict(list)
for p in positions:
    v = p["strategy_version"] if "strategy_version" in p.keys() and p["strategy_version"] else "v0"
    versions[v].append(p)

if len(versions) > 1 or (len(versions) == 1 and list(versions.keys())[0] != "v0"):
    print(f"\n{'─'*70}")
    print("BY STRATEGY VERSION (A/B comparison)")
    print(f"{'─'*70}")
    for v in sorted(versions.keys()):
        bucket = versions[v]
        bw = len([p for p in bucket if (p["pnl_usd"] or 0) > 0])
        bpnl = sum(p["pnl_usd"] or 0 for p in bucket)
        bcost = sum(p["cost_usd"] for p in bucket)
        wr = bw / len(bucket) * 100 if bucket else 0
        roi = bpnl / bcost * 100 if bcost > 0 else 0
        print(f"  {v}: {len(bucket)} trades | {wr:.0f}% win rate | PnL ${bpnl:+,.2f} | ROI {roi:+.1f}%")

# ---- By Conviction Level ----
print(f"\n{'─'*70}")
print("BY CONVICTION LEVEL")
print(f"{'─'*70}")
for strength in sorted({p["signal_strength"] for p in positions}):
    bucket = [p for p in positions if p["signal_strength"] == strength]
    bw = len([p for p in bucket if (p["pnl_usd"] or 0) > 0])
    bpnl = sum(p["pnl_usd"] or 0 for p in bucket)
    bcost = sum(p["cost_usd"] for p in bucket)
    wr = bw / len(bucket) * 100
    roi = bpnl / bcost * 100 if bcost > 0 else 0
    print(f"  ×{strength}: {len(bucket)} trades | {wr:.0f}% win rate | PnL ${bpnl:+,.2f} | ROI {roi:+.1f}%")

# ---- By Entry Price Bucket ----
print(f"\n{'─'*70}")
print("BY ENTRY PRICE")
print(f"{'─'*70}")
price_buckets = [
    ("0.00-0.25", 0.00, 0.25), ("0.25-0.40", 0.25, 0.40),
    ("0.40-0.60", 0.40, 0.60), ("0.60-0.80", 0.60, 0.80), ("0.80+", 0.80, 1.01),
]
for label, lo, hi in price_buckets:
    bucket = [p for p in positions if lo <= (p["entry_price"] or 0) < hi]
    if not bucket: continue
    bw = len([p for p in bucket if (p["pnl_usd"] or 0) > 0])
    bpnl = sum(p["pnl_usd"] or 0 for p in bucket)
    bcost = sum(p["cost_usd"] for p in bucket)
    wr = bw / len(bucket) * 100
    roi = bpnl / bcost * 100 if bcost > 0 else 0
    print(f"  {label}: {len(bucket)} trades | {wr:.0f}% win rate | PnL ${bpnl:+,.2f} | ROI {roi:+.1f}%")

# ---- By Category ----
print(f"\n{'─'*70}")
print("BY CATEGORY")
print(f"{'─'*70}")
cats = defaultdict(list)
for p in positions:
    cats[p["category"] or "unknown"].append(p)
for cat in sorted(cats, key=lambda c: -sum((p["pnl_usd"] or 0) for p in cats[c])):
    bucket = cats[cat]
    bw = len([p for p in bucket if (p["pnl_usd"] or 0) > 0])
    bpnl = sum(p["pnl_usd"] or 0 for p in bucket)
    bcost = sum(p["cost_usd"] for p in bucket)
    wr = bw / len(bucket) * 100
    roi = bpnl / bcost * 100 if bcost > 0 else 0
    print(f"  {cat}: {len(bucket)} trades | {wr:.0f}% win rate | PnL ${bpnl:+,.2f} | ROI {roi:+.1f}%")

# ---- Leader Accuracy Top/Bottom 5 ----
print(f"\n{'─'*70}")
print("LEADER ACCURACY (top 5 profitable, bottom 5)")
print(f"{'─'*70}")
leader_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0, "count": 0})
for p in positions:
    # Get all conviction wallets for this position
    sig = conn.execute("SELECT conviction_wallets FROM signals WHERE id=?", (p["signal_id"],)).fetchone()
    if sig and sig[0]:
        for w in sig[0].split(","):
            w = w.strip()
            if w:
                leader_stats[w]["count"] += 1
                leader_stats[w]["pnl"] += (p["pnl_usd"] or 0)
                if (p["pnl_usd"] or 0) > 0:
                    leader_stats[w]["wins"] += 1
                else:
                    leader_stats[w]["losses"] += 1

# Sort by PnL
sorted_leaders = sorted(leader_stats.items(), key=lambda x: -x[1]["pnl"])
min_trades = 3
filtered = [(w, s) for w, s in sorted_leaders if s["count"] >= min_trades]

if filtered:
    print(f"  TOP 5 (min {min_trades} trades):")
    for w, s in filtered[:5]:
        wr = s["wins"] / s["count"] * 100
        print(f"    {w[:16]}… | {s['count']} trades | {wr:.0f}% win | PnL ${s['pnl']:+,.2f}")
    print(f"  BOTTOM 5:")
    for w, s in filtered[-5:]:
        wr = s["wins"] / s["count"] * 100
        print(f"    {w[:16]}… | {s['count']} trades | {wr:.0f}% win | PnL ${s['pnl']:+,.2f}")

# ---- Win Streak / Loss Streak ----
print(f"\n{'─'*70}")
print("STREAKS")
print(f"{'─'*70}")
max_win_streak = 0
max_loss_streak = 0
current_streak = 0
streak_type = None
for p in positions:
    is_win = (p["pnl_usd"] or 0) > 0
    if streak_type == is_win:
        current_streak += 1
    else:
        current_streak = 1
        streak_type = is_win
    if is_win and current_streak > max_win_streak:
        max_win_streak = current_streak
    if not is_win and current_streak > max_loss_streak:
        max_loss_streak = current_streak

print(f"  Longest win streak: {max_win_streak}")
print(f"  Longest loss streak: {max_loss_streak}")

# ---- Weighted conviction analysis (if available) ----
weighted_pos = [p for p in positions if "weighted_conviction" in p.keys() and p["weighted_conviction"] and p["weighted_conviction"] > 0]
if weighted_pos:
    print(f"\n{'─'*70}")
    print("WEIGHTED CONVICTION (leader quality impact)")
    print(f"{'─'*70}")
    high_quality = [p for p in weighted_pos if p["weighted_conviction"] >= 4.5]
    low_quality = [p for p in weighted_pos if p["weighted_conviction"] < 4.5]
    if high_quality:
        hq_wr = len([p for p in high_quality if (p["pnl_usd"] or 0) > 0]) / len(high_quality) * 100
        hq_pnl = sum(p["pnl_usd"] or 0 for p in high_quality)
        print(f"  High quality (weighted >= 4.5): {len(high_quality)} trades | {hq_wr:.0f}% win | PnL ${hq_pnl:+,.2f}")
    if low_quality:
        lq_wr = len([p for p in low_quality if (p["pnl_usd"] or 0) > 0]) / len(low_quality) * 100
        lq_pnl = sum(p["pnl_usd"] or 0 for p in low_quality)
        print(f"  Low quality (weighted < 4.5):  {len(low_quality)} trades | {lq_wr:.0f}% win | PnL ${lq_pnl:+,.2f}")

print(f"\n{'='*70}")
print("CURRENT CONFIG")
print(f"{'='*70}")
try:
    from config import cfg
    print(f"  Strategy version: {cfg.get('strategy_version', '?')}")
    print(f"  Conviction min: {cfg['conviction']['min_leaders']}")
    print(f"  Leader weighting: {'ON' if cfg['leader_weighting']['enabled'] else 'OFF'}")
    print(f"  Entry price: {cfg['entry_price']['min']}-{cfg['entry_price']['max']}")
    print(f"  Blocked categories: {cfg['categories']['blocked']}")
    print(f"  Max per event: {cfg['concentration']['max_positions_per_event']}")
except:
    print("  Could not load config.yaml")

conn.close()
