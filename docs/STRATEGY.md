# Strategy changelog

Every change is tagged with `strategy_version` in `config.yaml` so results stay
attributable. The bar for shipping a change is **out-of-sample validation** — a
finding must hold on data it was not derived from, and ideally have a mechanical
explanation, not just a favourable number.

---

## v3.3 — 2026-08-01 — entry-price dead zone

**Change:** `entry_price.dead_zone: [0.70, 0.75]` — signals priced in that slice
are never bet. Everything else in the 0.60–0.80 band is unchanged.

**Why.** As entry price rises the payoff ratio degrades, so the win rate you need
just to break even rises with it. Our realised win rate does *not* rise smoothly —
it plateaus around 66–67% across 0.65–0.75, then jumps at 0.75+. That leaves a
hole in the middle where the bar has moved above our hit rate:

| Band | Payoff ratio | Break-even WR | Realised WR | Gap |
|---|---|---|---|---|
| 0.60–0.65 | 1.32:1 | 56.9% | 62.1% | **+5.3** |
| 0.65–0.70 | 1.90:1 | 65.6% | 66.5% | +0.9 |
| **0.70–0.75** | 2.11:1 | **67.9%** | 66.7% | **−1.2** |
| 0.75–0.80 | 2.41:1 | 70.7% | 76.0% | **+5.3** |

It is the only band with a negative gap — i.e. the only slice where the
arithmetic does not work.

**Validation.** Resolved positions were split in half by time; the rule was
derived on the first half and scored on the second, which it had never seen.

- Edge vs implied probability was negative in **both** halves (−3.3pp, −6.1pp) —
  the only band for which that is true.
- Out-of-sample, excluding the band moved P&L from **+$1,347 → +$1,862 (+38%)**
  while retaining 73% of trade volume (+$2.21 → +$4.17 per trade).

**Expected benefit:** roughly **+$100 to +$300/month** at current (0.5%) sizing.
The lower bound is the all-time average, the upper bound assumes recent
behaviour persists. Downside if the effect is spurious is about −$150/month in
forgone profit, so the asymmetry is favourable.

**Caveats, honestly.** One out-of-sample test over ~30 days in a single regime.
The band's *edge* was negative in both halves, but its *P&L* was mildly positive
(+$144) in the first half — so the P&L signal is weaker than the edge signal.
Band boundaries were chosen at 0.05 increments, which is a forking-paths degree
of freedom.

**Safety net.** Blocked signals are still logged to the live feed and recorded in
the **shadow book** as hypothetical $100 bets, so the block is validated forward
automatically. If it is wrong, the shadow book will show it as forgone profit
within 2–3 weeks. Revert by deleting the `dead_zone` line.

---

## Ideas tested and **rejected** in the same round

Recording these matters as much as the change that shipped — each looked
plausible and cost real money in simulation.

| Idea | Test | Result |
|---|---|---|
| Select leaders by realised copy performance | Walk-forward, no lookahead | **Rejected.** Every variant lost. Skipping "underperforming" leaders removed **+$687 to +$1,125** of profit — past leader performance mean-reverts at these sample sizes. |
| Raise `min_leaders` 4 → 5 | `shadow_min5` flag, incl. out-of-sample half | **Rejected.** Would drop 66% of volume, and the dropped trades were the profitable ones (+$1,537). OOS: +$394 → −$784. |
| Improve the ranker score to pick better leaders | Correlation of score vs realised P&L | **Rejected.** Score contains zero skill signal by construction (`hit_rate`/`avg_edge` are hardcoded 0) and saturates at 0.938–0.992 inside the polled top-100. An apparent `r=+0.849` collapsed to **−0.463** once a single outlier was removed. |
| Per-category leader definitions | Leader × category breakdown | **Not answerable.** Only 1 leader has ≥15 copies across ≥2 categories; 96% of all copies are sports. No cross-category variation exists to measure yet. |

### Assumption audited and confirmed

The conviction premise requires that 4+ agreeing leaders are *independent*
opinions, not one opinion counted four times. Across 57,089 rosters / 103
wallets: **0 pairs** with Jaccard ≥ 0.70, and a conviction inflation factor of
**1.00×**. Some pairs show partial correlation (top pair Jaccard 0.31, lift 79×),
consistent with shared league/time specialisation rather than duplication.

---

## Earlier versions

| Version | Change | Rationale |
|---|---|---|
| v3.2 | Block halftime markets | Derivative of "who wins"; precautionary (breakeven but fragile) |
| v3.1 | Block exact-score markets | −$419 all-time; predicting a scoreline is unrelated to who wins |
| v3.0 | Block spreads/handicaps | **−$2,380 all-time** — the single largest leak found |
| v2.9 | Entry floor 0.40 → 0.60 | 0.50–0.60 band lost −$1,762; low entries = betting against market consensus |
| v2.8 | 48h resolution cap, leader-confirmation exit gate, streak block, block O/U | Long-dated volatility; 90% of adverse exits happened while leaders were still buying |
| v2.7 | Tighter adverse exit, 1 position per event, block opposing sides | Correlation control |

The common thread from v2.8 through v3.3: the edge is **"who wins."** Every
market type that asks something else — by how much, exact score, the total,
at halftime — has been removed. v3.3 extends the same reasoning to *price*:
remove the slice where the required win rate exceeds the achievable one.
