# Changelog

Strategy changes are versioned as `strategy_version` in `config.yaml` and tagged
here. Infrastructure changes are grouped by date. Detailed strategy rationale,
including ideas that were **tested and rejected**, lives in
[`docs/STRATEGY.md`](docs/STRATEGY.md).

The bar for shipping a strategy change is **out-of-sample validation** — it must
hold on data it was not derived from, and ideally have a mechanical explanation
rather than just a favourable number.

---

## v3.4 — 2026-08-06

### Added
- **Exit-hold instrumentation.** Adverse exits were the single largest unmeasured
  drag on the strategy: 160 exits at a 6.9% win rate cost **−$6,387**, consuming
  ~70% of the +$9,044 earned by the other 1,131 positions. Whether that is money
  *saved* (positions were doomed) or *destroyed* (recoveries cut short) was
  unanswerable. `shadow_book.open_exit_hold_shadow()` now records what holding to
  resolution would have paid, priced at the original entry.
- **`GET /api/exit_analysis`** — reports `exit_pnl − hold_pnl` per position and in
  aggregate, with a verdict on whether exits add or destroy value.

### Changed
- **`resolution.max_hours` 48 → 24.** Long holds lose in both out-of-sample
  halves (>48h: −$40 and −$52 per trade; 24–48h entry→end: −$11 per trade).
  Capping at 24h gained +$206 and +$894 respectively. Samples are thin (n=20–52),
  so the `too_far` shadow bucket validates it forward.

### Deliberately not changed
- **The Over/Under block stays.** 239 resolved shadow bets showed +$4.83/$100 at
  72.8%, which superficially argues for unblocking — but the effect decays
  monotonically (+$7.07 → +$2.60 → **−$9.08** in August), the original block rested
  on real losses (114 trades, 50.9% WR, −$722), and unblocking a derivative market
  contradicts the thesis behind every win since v2.8.

---

## v3.3 — 2026-08-01

### Changed
- **`entry_price.dead_zone: [0.70, 0.75]`.** The only band where the break-even
  win rate (67.9%) exceeds the realised one (66.7%). As entry price rises the
  payoff ratio degrades, but the realised win rate plateaus at ~66–67% across
  0.65–0.75 before jumping at 0.75+ — leaving a hole in the middle.

  Validated out-of-sample: negative edge in **both** halves (−3.3pp, −6.1pp), the
  only band for which that is true. Excluding it moved P&L from **+$1,347 →
  +$1,862 (+38%)** on the half it was not derived from, retaining 73% of volume.

  *Early forward validation:* 56 resolved shadow bets in the blocked band are
  running −$6.65/$100, confirming the block within days of shipping.

---

## Infrastructure — 2026-07-28 → 08-01

### Added
- **Full Sentry integration** across both processes (asyncio worker + FastAPI
  dashboard), tagged by component: errors, logs (warning+ only, to protect quota),
  custom business metrics, performance tracing, profiling, session replay, and
  cron check-in monitoring for the four scheduled jobs.
- **`release.sh`** — one-command deploy that creates a Sentry release from the git
  commit, links commits via the GitHub integration, deploys, restarts, and records
  a production deploy. Every error is attributable to the exact commit it ran on.

### Fixed
- **OOM risk in the leader median cache.** `_build_size_cache` loaded ~968k signal
  rows into Python every 5 minutes to derive one median for each of ~123 wallets —
  **182MB peak allocation on a 454MB box with 229MB available**, plus a 5.6s stall
  of the poll loop. This was the likely root cause of the historical crash-loops
  previously papered over with a swapfile. Replaced with per-wallet indexed median
  seeks: **5.39s → 1.34s (4×), 182MB → ~0MB**, verified byte-identical results.
- **`seeds.json` corruption.** A crash mid-write had truncated the file, failing
  every hourly leaderboard refresh since Jul 11. Writes are now atomic
  (temp + fsync + `os.replace`) with a rolling backup; loads recover from backup
  rather than crashing. Salvaged 4,504 wallets from the corrupt file.
- **Ranker performance.** `rank_seeds` fetched ~4,600 wallets sequentially, taking
  **2–4 hours** and overrunning its monitor. Parallelised across a 12-worker pool
  (tuned empirically — the API rate-limits hard above ~12): **~4h → ~37min**,
  confirmed over three consecutive clean runs.
- **Cron false alarms.** The hourly leaderboard loop was triggering a full
  ~3h re-rank inline, blocking itself and causing missed check-ins. Removed the
  redundant re-rank; scoring happens on the 6h ranker pass as designed.

---

## Earlier strategy versions

| Version | Change | Rationale |
|---|---|---|
| v3.2 | Block halftime markets | Derivative of "who wins"; precautionary |
| v3.1 | Block exact-score markets | −$419 all-time |
| v3.0 | Block spreads/handicaps | **−$2,380 all-time** — largest single leak found |
| v2.9 | Entry floor 0.40 → 0.60 | 0.50–0.60 band lost −$1,762 |
| v2.8 | 48h resolution cap, leader-confirmation exit gate, streak block, block O/U | Long-dated volatility; 90% of adverse exits fired while leaders were still buying |
| v2.7 | Tighter adverse exit, 1 position per event, block opposing sides | Correlation control |
