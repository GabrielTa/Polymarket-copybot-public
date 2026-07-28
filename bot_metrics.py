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

from contextlib import contextmanager

try:
    from sentry_sdk import metrics as _m
    _HAVE = hasattr(_m, "count")
except Exception:  # sentry-sdk missing or too old
    _HAVE = False

try:
    import sentry_sdk as _sentry
    _HAVE_SDK = True
except Exception:
    _HAVE_SDK = False


@contextmanager
def cron(slug: str, interval_minutes: int):
    """Report a scheduled job to Sentry Crons as a check-in.

    Emits in_progress on entry and ok/error on exit, and auto-creates the monitor
    in Sentry with the given interval schedule. No-op if the SDK/crons API is
    unavailable. Alerts you if a job stops running or overruns.
    """
    if not _HAVE_SDK:
        yield
        return
    try:
        from sentry_sdk.crons import capture_checkin
    except Exception:
        yield
        return
    config = {
        "schedule": {"type": "interval", "value": interval_minutes, "unit": "minute"},
        "checkin_margin": 5,
        "max_runtime": 30,
        "timezone": "UTC",
    }
    try:
        cid = capture_checkin(monitor_slug=slug, status="in_progress", monitor_config=config)
    except Exception:
        yield
        return
    try:
        yield
    except Exception:
        try:
            capture_checkin(monitor_slug=slug, check_in_id=cid, status="error", monitor_config=config)
        except Exception:
            pass
        raise
    else:
        try:
            capture_checkin(monitor_slug=slug, check_in_id=cid, status="ok", monitor_config=config)
        except Exception:
            pass


@contextmanager
def trace(name: str, op: str = "task"):
    """Open a Sentry transaction so metrics emitted inside are trace-connected.

    Trace-connected metrics are only reliably ingested when emitted within a
    sampled transaction; the worker loops have no web request to ride on, so we
    start one explicitly. No-op if the SDK is unavailable.
    """
    if not _HAVE_SDK:
        yield
        return
    try:
        txn_cm = _sentry.start_transaction(op=op, name=name)
    except Exception:
        # Couldn't start a transaction — run the body untraced rather than break.
        yield
        return
    with txn_cm:
        yield


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
