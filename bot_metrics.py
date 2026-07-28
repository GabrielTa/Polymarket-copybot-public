"""Business metrics emitted to Sentry (trace-connected Metrics API, sentry-sdk >= 2.44).

Thin wrapper so call sites stay clean and the bot never breaks on a metrics
failure. Every function is a silent no-op when:
  - sentry-sdk isn't installed, or
  - SENTRY_DSN is unset (no active client, so the SDK drops the metric), or
  - the metrics submodule isn't present in the installed SDK version.

Metric taxonomy for this bot:
  Counters      copy.position_opened / closed / exited, signal.skipped, poll.cycle
  Gauges        bankroll.equity_usd / cash_usd / open_exposure_usd, positions.open_count
  Distributions position.pnl_usd, position.entry_price, position.hold_hours, poll.cycle_seconds
"""
from __future__ import annotations

try:
    from sentry_sdk import metrics as _m
    _HAVE = hasattr(_m, "count")
except Exception:  # sentry-sdk missing or too old
    _HAVE = False


def _clean(attrs: dict | None) -> dict | None:
    if not attrs:
        return None
    # Sentry attributes must be str/num/bool; coerce None away, stringify the rest lightly.
    return {k: v for k, v in attrs.items() if v is not None}


def count(key: str, value: float = 1, **attrs) -> None:
    if _HAVE:
        try:
            _m.count(key, value, attributes=_clean(attrs))
        except Exception:
            pass


def gauge(key: str, value: float, **attrs) -> None:
    if _HAVE:
        try:
            _m.gauge(key, value, attributes=_clean(attrs))
        except Exception:
            pass


def distribution(key: str, value: float, **attrs) -> None:
    if _HAVE:
        try:
            _m.distribution(key, value, attributes=_clean(attrs))
        except Exception:
            pass


def entry_band(price: float) -> str:
    """Bucket an entry price into the 0.05 band used across the strategy analysis."""
    try:
        lo = int(price * 20) / 20.0
        return f"{lo:.2f}-{lo + 0.05:.2f}"
    except Exception:
        return "?"
