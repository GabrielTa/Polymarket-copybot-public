"""Analyze closed positions to understand why we're losing money.

Run: python analyze_losses.py
"""
import sqlite3
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
  ORDER BY pp.closed_ts DESC
""").fetchall()

open_pos = conn.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall()
bankroll = conn.execute("SELECT cash_usd FROM paper_bankroll WHERE id=1").fetchone()

print("=" * 70)
print("POLYMARKET COPY BOT — LOSS ANALYSIS")
print("=" * 70)

# ---- Overall ----
total = len(positions)
won = [p for p in positions if p["pnl_usd"] > 0]
lost = [p for p in positions if p["pnl_usd"] <= 0]
total_pnl = sum(p["pnl_usd"] for p in positions)
total_cost = sum(p["cost_usd"] for p in positions)
total_won_pnl = sum(p["pnl_usd"] for p in won)
total_lost_pnl = sum(p["pnl_usd"] for p in lost)

print(f"\nOverall: {total} closed trades")
print(f"  Won:  {len(won)} ({len(won)/total*100:.0f}%)")
print(f"  Lost: {len(lost)} ({len(lost)/total*100:.0f}%)")
print(f"  Total PnL:      ${total_pnl:+.2f}")
print(f"  Total wagered:  ${total_cost:.2f}")
print(f"  ROI:            {total_pnl/total_cost*100:+.1f}%" if total_cost > 0 else "")
print(f"  Avg win:        ${sum(p['pnl_usd'] for p in won)/len(won):.2f}" if won else "  No wins")
print(f"  Avg loss:       ${sum(p['pnl_usd'] for p in lost)/len(lost):.2f}" if lost else "  No losses")
print(f"  Cash remaining: ${bankroll[0]:.2f}" if bankroll else "")
print(f"  Open positions: {len(open_pos)}")

# ---- By entry price bucket ----
print(f"\n{'─'*70}")
print("BY ENTRY PRICE (where are we losing?)")
print(f"{'─'*70}")
price_buckets = [
    ("0.00-0.20 (longshots)", 0.00, 0.20),
    ("0.20-0.40 (underdogs)", 0.20, 0.40),
    ("0.40-0.60 (toss-ups)", 0.40, 0.60),
    ("0.60-0.80 (favorites)", 0.60, 0.80),
    ("0.80-1.00 (heavy favorites)", 0.80, 1.00),
]
for label, lo, hi in price_buckets:
    bucket = [p for p in positions if lo <= p["entry_price"] < hi]
    if not bucket:
        continue
    bw = [p for p in bucket if p["pnl_usd"] > 0]
    bl = [p for p in bucket if p["pnl_usd"] <= 0]
    bpnl = sum(p["pnl_usd"] for p in bucket)
    wr = len(bw) / len(bucket) * 100 if bucket else 0
    bcost = sum(p["cost_usd"] for p in bucket)
    print(f"  {label}")
    print(f"    {len(bucket)} trades | {wr:.0f}% win rate | PnL: ${bpnl:+.2f} | Wagered: ${bcost:.2f}")

# ---- By signal strength ----
print(f"\n{'─'*70}")
print("BY SIGNAL STRENGTH (do more leaders = better results?)")
print(f"{'─'*70}")
for strength in sorted({p["signal_strength"] for p in positions}):
    bucket = [p for p in positions if p["signal_strength"] == strength]
    bw = [p for p in bucket if p["pnl_usd"] > 0]
    bpnl = sum(p["pnl_usd"] for p in bucket)
    bcost = sum(p["cost_usd"] for p in bucket)
    wr = len(bw) / len(bucket) * 100
    print(f"  ×{strength}: {len(bucket)} trades | {wr:.0f}% win rate | PnL: ${bpnl:+.2f} | Wagered: ${bcost:.2f}")

# ---- By category ----
print(f"\n{'─'*70}")
print("BY CATEGORY (which markets are we bad at?)")
print(f"{'─'*70}")
cats = defaultdict(list)
for p in positions:
    cats[p["category"] or "unknown"].append(p)
for cat in sorted(cats, key=lambda c: sum(p["pnl_usd"] for p in cats[c])):
    bucket = cats[cat]
    bw = [p for p in bucket if p["pnl_usd"] > 0]
    bpnl = sum(p["pnl_usd"] for p in bucket)
    bcost = sum(p["cost_usd"] for p in bucket)
    wr = len(bw) / len(bucket) * 100
    print(f"  {cat}: {len(bucket)} trades | {wr:.0f}% win rate | PnL: ${bpnl:+.2f} | Wagered: ${bcost:.2f}")

# ---- By side (BUY vs SELL) ----
print(f"\n{'─'*70}")
print("BY SIDE (are we better at buying or selling?)")
print(f"{'─'*70}")
for side in ["BUY", "SELL"]:
    bucket = [p for p in positions if p["side"] == side]
    if not bucket:
        continue
    bw = [p for p in bucket if p["pnl_usd"] > 0]
    bpnl = sum(p["pnl_usd"] for p in bucket)
    wr = len(bw) / len(bucket) * 100
    print(f"  {side}: {len(bucket)} trades | {wr:.0f}% win rate | PnL: ${bpnl:+.2f}")

# ---- Biggest losses ----
print(f"\n{'─'*70}")
print("TOP 10 BIGGEST LOSSES")
print(f"{'─'*70}")
worst = sorted(positions, key=lambda p: p["pnl_usd"])[:10]
for p in worst:
    title = p["market_title"] or "?"
    print(f"  ${p['pnl_usd']:+7.2f} | entry={p['entry_price']:.2f} close={p['close_price']:.2f} "
          f"| ×{p['signal_strength']} | {title[:45]}")

# ---- Biggest wins ----
print(f"\n{'─'*70}")
print("TOP 10 BIGGEST WINS")
print(f"{'─'*70}")
best = sorted(positions, key=lambda p: -p["pnl_usd"])[:10]
for p in best:
    title = p["market_title"] or "?"
    print(f"  ${p['pnl_usd']:+7.2f} | entry={p['entry_price']:.2f} close={p['close_price']:.2f} "
          f"| ×{p['signal_strength']} | {title[:45]}")

# ---- Opposite-side bets on same market ----
print(f"\n{'─'*70}")
print("OPPOSITE-SIDE BETS (hedged ourselves accidentally?)")
print(f"{'─'*70}")
market_bets = defaultdict(list)
for p in positions:
    market_bets[p["market"]].append(p)
hedged = {m: ps for m, ps in market_bets.items() if len(ps) > 1}
if hedged:
    for m, ps in hedged.items():
        net = sum(p["pnl_usd"] for p in ps)
        cost = sum(p["cost_usd"] for p in ps)
        title = ps[0]["market_title"] or m[:20]
        outcomes = ", ".join(f"{p['outcome']}({'W' if p['pnl_usd']>0 else 'L'})" for p in ps)
        print(f"  {title[:40]} | net=${net:+.2f} cost=${cost:.2f} | {outcomes}")
else:
    print("  None found.")

# ---- High-price bets (the ones we should have skipped) ----
print(f"\n{'─'*70}")
print("BETS AT ENTRY PRICE > 0.80 (should have been filtered)")
print(f"{'─'*70}")
expensive = [p for p in positions if p["entry_price"] > 0.80]
if expensive:
    exp_pnl = sum(p["pnl_usd"] for p in expensive)
    exp_won = len([p for p in expensive if p["pnl_usd"] > 0])
    print(f"  {len(expensive)} trades | {exp_won} won | PnL: ${exp_pnl:+.2f}")
    print(f"  → If we had NOT made these bets, our PnL would be ${total_pnl - exp_pnl:+.2f}")
    for p in sorted(expensive, key=lambda p: p["pnl_usd"]):
        title = p["market_title"] or "?"
        print(f"    ${p['pnl_usd']:+7.2f} | entry={p['entry_price']:.2f} | {title[:45]}")
else:
    print("  None (filter is working).")

print(f"\n{'='*70}")
print("RECOMMENDATIONS")
print(f"{'='*70}")

# Auto-generate recommendations
if expensive:
    exp_pnl = sum(p["pnl_usd"] for p in expensive)
    exp_won = len([p for p in expensive if p["pnl_usd"] > 0])
    exp_wr = exp_won / len(expensive) * 100
    print(f"• HIGH-PRICE FILTER: {len(expensive)} bets above 0.80 entry, win rate {exp_wr:.0f}%, PnL ${exp_pnl:+.2f}")
    print(f"  → The 0.80 max entry price filter will prevent these going forward.")

for cat, bucket in sorted(cats.items(), key=lambda x: sum(p["pnl_usd"] for p in x[1])):
    bpnl = sum(p["pnl_usd"] for p in bucket)
    wr = len([p for p in bucket if p["pnl_usd"] > 0]) / len(bucket) * 100
    if bpnl < -20 and wr < 40:
        print(f"• CATEGORY '{cat}': {len(bucket)} trades, {wr:.0f}% win rate, ${bpnl:+.2f}")
        print(f"  → Consider excluding {cat} from copy signals.")

for strength in sorted({p["signal_strength"] for p in positions}):
    bucket = [p for p in positions if p["signal_strength"] == strength]
    bpnl = sum(p["pnl_usd"] for p in bucket)
    wr = len([p for p in bucket if p["pnl_usd"] > 0]) / len(bucket) * 100
    if bpnl < -15 and wr < 40:
        print(f"• STRENGTH ×{strength}: {len(bucket)} trades, {wr:.0f}% win rate, ${bpnl:+.2f}")
        if strength == 1:
            print(f"  → Consider disabling solo elite bets entirely.")
        else:
            print(f"  → Consider raising conviction threshold.")

conn.close()
