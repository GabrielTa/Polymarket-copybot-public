# Automation (n8n)

Five importable workflows in [`n8n/`](../n8n). They are **read-only** against the
bot — nothing here can place a trade or change config, by design. A workflow tool
with a bug should not be able to touch a trading system.

## Architecture

```
        ┌────────────────────┐
        │  bot  /api/digest  │  equity, win rate vs break-even,
        │   (single call)    │  exit quality, filter validation, alerts
        └─────────┬──────────┘
                  │
   ┌──────────────┴──────────────┐
   │            n8n              │  schedule / webhook / merge
   └──────────────┬──────────────┘
                  │
      ┌───────────┴───────────┐
      │  Sentry   │  Telegram │  system health   ·   delivery
      └───────────────────────┘
```

The split matters: **Sentry answers "is it running?", the digest answers "is it
working?"** Sentry already covers crashes, cron overruns and errors — don't
rebuild that in n8n. The digest covers the thing nothing else watches: the edge
quietly decaying.

## `GET /api/digest?window=24h|48h|72h|7d|30d`

One call returns everything, with the arithmetic already done:

| Field | What it tells you |
|---|---|
| `equity` | current, change over window, peak, drawdown % |
| `trading` | trades, win rate, P&L, payoff ratio, **break-even win rate**, `margin_pp` |
| `open_positions` | count and exposure |
| `exits` | exits fired, and (v3.4) whether exiting beat holding |
| `filters_blocked` | what each filter rejected in the window |
| `forward_validation` | per-filter shadow verdict: is each block still correct? |
| **`alerts`** | computed conditions worth acting on |
| `status` | `ok` / `warning` / `critical` |

**Automation should branch on `alert_count` or `status` only.** No client-side
arithmetic — that logic belongs in Python where it is testable.

`margin_pp` is the number that matters: realised win rate minus the break-even
rate implied by the current payoff ratio. Negative means you are losing money
even while winning most bets.

## The workflows

| File | Trigger | Purpose |
|---|---|---|
| `01-daily-digest.json` | daily 09:07 | Morning summary + Sentry issue count → Telegram |
| `02-health-alert.json` | every 6h | Fires **only** when `alert_count > 0` |
| `03-weekly-validation.json` | Mondays 09:23 | Are v3.3/v3.4 blocks still correct? Exit verdict |
| `04-sentry-enricher.json` | Sentry webhook | Turns "cron failed" into "cron failed **and** here is the bot's state" |
| `05-job-tracker.json` | 2×/day | Keyword-filtered job feed → deduped → Google Sheet |

Schedules use off-round minutes deliberately (`:07`, `:13`, `:23`, `:41`) so they
don't pile onto the same instant as every other cron on the planet.

## Setup

1. **Import** each JSON: n8n → *Workflows* → *Import from File*.
2. **Replace placeholders** (they are intentionally not real values):
   - `YOUR_SERVER_IP` → your dashboard host
   - `YOUR_TELEGRAM_CHAT_ID` → the chat id already in your `.env`
   - `YOUR_SHEET_ID` → only for the job tracker
3. **Add credentials** in n8n: Telegram bot token, Sentry API token
   (`org:read`, `project:read`, `event:read`), Google Sheets (job tracker only).
4. **For the Sentry enricher:** copy the webhook's Production URL, then in Sentry
   go to *Settings → Integrations → WebHooks*, paste it, and enable the alerts you
   want forwarded. Also add it as an action on your cron monitor alerts.
5. **Activate** each workflow.

### Do not self-host n8n on the bot's droplet

It has 454MB RAM; the worker uses ~200MB and an OOM caused by a 182MB spike was
only fixed in the 2026-08 infrastructure work. Use n8n cloud (or any other host)
and call the API over the network.

## Security

The dashboard currently serves `/api/*` with **no authentication**. That is
pre-existing, but wiring automation to it increases exposure. If you add an API
key, every workflow needs one extra header — n8n supports this natively via
*Header Auth* credentials on the HTTP Request node.
