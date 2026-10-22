# Strategy changelog

Every change is tagged with `strategy_version` in `config.yaml` so results stay
attributable. The bar for shipping a change is **out-of-sample validation** — a
finding must hold on data it was not derived from, and ideally have a mechanical
explanation, not just a favourable number.

---

## v3.5 — 2026-10-22 — conviction band, conviction exits off, crypto blocked

### The diagnosis this came from

Four months of flat equity ($12,101 → $12,052 → $11,518 → $11,751) on ~1,700
trades. The cause, measured as `edge = realised win% − implied probability paid`:

| Month | n | Win% | Implied% | **Edge** | P&L |
|---|---|---|---|---|---|
| Jul | 383 | 69.2% | 69.4% | −0.2 | +$1,375 |
| Aug | 433 | 68.1% | 68.8% | −0.6 | −$49 |
| Sep | 424 | 59.2% | 68.0% | **−8.8** | −$534 |
| Oct | 462 | 63.0% | 68.5% | **−5.5** | +$233 |

**Negative edge every month.** The strategy wins about as often as the price says
it should, so equity random-walks. The fix is concentration — cutting the
negative-edge majority, not adding volume.

### 1. Bet only conviction 5–6 (`bet_min: 5`, `bet_max: 6`)

| Conviction | Jul | Aug | Sep | Oct | 120d | Edge |
|---|---|---|---|---|---|---|
| 4 (56% of volume) | +$1,333 | −$1,642 | −$693 | +$455 | **−$470** | −7.7 |
| **5** | +$292 | +$726 | +$231 | +$7 | **+$1,220** | **+0.3** |
| **6** | +$25 | +$91 | +$286 | +$887 | **+$1,361** | **+3.4** |
| 7+ | −$463 | +$326 | −$367 | −$992 | **−$1,496** | −10.2 |

5 and 6 are the only buckets positive in **all four months**. Keeping just them
would have turned +$328 into +$2,581 over 120 days, on a third of the volume.

**This reverses the v3.4-era conclusion.** In August the `shadow_min5` flag said
raising the bar would destroy value — that test ran on 384 trades from Jul–Aug,
and July was the single month conviction-4 worked. More data inverted it.

Both bounds are enforced in `paper_book` as **skips**, not upstream rejections, so
declined signals still reach the shadow book (`conviction_too_low` /
`conviction_too_high`) and the band stays reversible on forward evidence. The
`min_leaders` gate stays at 4 for exactly that reason.

### 2. Conviction exits disabled (`conviction_exit.enabled: false`)

The v3.4 exit-hold instrumentation settled it — 122 resolved shadows:

| Exit type | n | Hold would've won | Hold $/100 | Exit $/100 | Verdict |
|---|---|---|---|---|---|
| adverse price | 10 | 20.0% | −$67.26 | −$57.69 | ✅ correct, kept |
| **conviction** | **112** | **69.6%** | **+$3.76** | **$0.00** | ❌ **off** |

Conviction exits threw away ~$420 of expected value. They also priced the exit
from `signals.our_hypo_price` — the **entry** price — so every one closed at
fabricated break-even and booked exactly $0.00. That bug is fixed (live market
price now), but the behaviour stays off pending evidence it helps.

Exits lost money in every month: −$74, −$179, −$263, −$260.

### 3. Crypto blocked

Never actually in `categories.blocked` despite the project notes saying to avoid
it. 32 positions, **50.0% win rate, −$367** (−$11.46/trade). "Will BTC be above
$86,000 on Oct 7" is a coin flip; leader consensus carries no information on it.

### Examined, not changed

- **Pre-game vs in-play flipped in September.** In-play: +$1,339 / +$459 / −$1,235
  / −$1,211. Pre-game: +$606 / −$77 / +$1,018 / +$1,520. That tracks the World Cup
  ending (~Jul 19) — a regime change, not a stable edge. Shadow-test before acting.
- **The v3.3 dead zone now looks wrong.** 946 resolved shadows in the blocked
  0.70–0.75 band: 74.5% win rate, **+$3.18/$100**. The forward data contradicts the
  out-of-sample test it shipped on. Left in place for now so v3.5's effect can be
  measured on its own; revert candidate for v3.6.

---

## v3.4 — 2026-08-06 — exit instrumentation + 24h resolution cap

### 1. Exit-hold shadow tracking (instrumentation, no behaviour change)

**The problem.** Adverse exits were the single largest unmeasured drag on the
strategy:

| | n | Win% | P&L |
|---|---|---|---|
| Resolved normally | 1,131 | 66.8% | **+$9,044** |
| Adverse-exited | 160 | 6.9% | **−$6,387** |
| **Net** | 1,291 | 59.3% | **+$2,657** |

Exits consume **70% of gross profit**. The defence is that they truncate losses
(−35% average vs −100% at zero), but that only holds if the exited positions were
doomed. Nothing measured the counterfactual.

**The fix.** `shadow_book.open_exit_hold_shadow()` — when an adverse exit fires,
record what **holding to resolution** would have paid, priced at the original
entry so the two are directly comparable. Read via `GET /api/exit_analysis`,
which reports `exit_pnl - hold_pnl` per position and in aggregate.

This changes no trading behaviour. It answers a question that was previously
unanswerable: is that −$6,387 money saved, or money destroyed?

### 2. `resolution.max_hours` 48 → 24

Long holds lose consistently, in both out-of-sample halves:

| Hold time | First half | Second half |
|---|---|---|
| <6h | +$0.94/trade | +$2.91/trade |
| 12–24h | +$3.62/trade | +$24.20/trade |
| 24–48h | +$11.28/trade | **−$40.82/trade** |
| >48h | **−$40.22/trade** | **−$51.89/trade** |

Capping holds at 24h gained **+$206 (first half)** and **+$894 (second half)**.
On the directly filterable dimension (entry → market end_date), the 24–48h
bucket runs **−$11.01/trade**. Same reasoning as v2.8's 168→48 cut.

**Caveat:** the samples are thin (n=20 on the filterable dimension, n=52 across
both halves for the hold-time counterfactual). The `too_far` shadow bucket
validates this forward.

### Also examined, and deliberately NOT changed

**The Over/Under block stays.** 239 resolved shadow bets showed +$4.83/$100 at a
72.8% win rate, which superficially argues for unblocking. Rejected because:

| Period | n | P&L/$100 |
|---|---|---|
| First half | 119 | +$7.07 |
| Second half | 120 | +$2.60 |
| **2026-08 (most recent)** | 24 | **−$9.08** |

The effect is **decaying monotonically** (+$7.07 → +$2.60 → −$9.08), the original
block was based on real losses (114 trades, 50.9% WR, −$722), and unblocking a
derivative market contradicts the thesis that has produced every win since v2.8:
**the edge is "who wins," not "by how much."** Acting on a decaying signal is the
trap this project exists to avoid.

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
