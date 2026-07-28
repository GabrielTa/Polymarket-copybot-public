"""Sentry error monitoring + performance tracing.

Centralised init used by both the worker (worker.py) and the dashboard (server.py).

Reads SENTRY_DSN from .env (same manual loader as notify.py — no python-dotenv
dependency). If the DSN is missing or the sentry_sdk package isn't installed,
init_sentry() is a silent no-op so the bot never crashes on a monitoring failure.

Set these in .env:
    SENTRY_DSN=https://<key>@<org>.ingest.sentry.io/<project>
    SENTRY_ENVIRONMENT=production      # optional, defaults to "production"
    SENTRY_TRACES_SAMPLE_RATE=0.1      # optional, fraction of transactions traced
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# ---- Load .env manually (mirrors notify.py) ----
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


def init_sentry(component: str) -> bool:
    """Initialise Sentry for a process. `component` is 'worker' or 'dashboard'.

    Returns True if Sentry was activated, False if skipped (no DSN / SDK).
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        log.info("Sentry disabled (no SENTRY_DSN in environment)")
        return False

    try:
        import sentry_sdk
    except ImportError:
        log.warning("SENTRY_DSN set but sentry-sdk not installed — run: pip install sentry-sdk")
        return False

    try:
        traces_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    except ValueError:
        traces_rate = 0.1

    try:
        profile_rate = float(os.environ.get("SENTRY_PROFILE_SESSION_SAMPLE_RATE", "1.0"))
    except ValueError:
        profile_rate = 1.0

    integrations = []
    if component == "dashboard":
        # FastAPI/Starlette integration auto-instruments request handling.
        try:
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.starlette import StarletteIntegration
            integrations = [StarletteIntegration(), FastApiIntegration()]
        except Exception:
            integrations = []

    # Forward Python logging to Sentry Logs, but only warning+ — the bot emits INFO
    # constantly (every poll cycle, every position), which would flood the logs quota.
    _DROP = {"trace", "debug", "info"}

    def _before_send_log(record, _hint):
        try:
            if record.get("severity_text", "").lower() in _DROP:
                return None
        except Exception:
            pass
        return record

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=traces_rate,
        integrations=integrations,
        # Capture warning/error/fatal logs as Sentry Logs (INFO/debug dropped below).
        enable_logs=True,
        before_send_log=_before_send_log,
        # Continuous profiling, "trace" lifecycle: the profiler runs automatically
        # whenever a transaction is active (poll/resolver/exit cycles, dashboard
        # requests), so profiles are tied to real work. Only sampled transactions
        # are profiled, so effective profiling volume = traces_rate × profile_rate.
        profile_session_sample_rate=profile_rate,
        profile_lifecycle="trace",
        # Attach which process the event came from so worker vs dashboard errors are filterable.
        release=os.environ.get("SENTRY_RELEASE") or None,
    )
    sentry_sdk.set_tag("component", component)
    log.info("Sentry enabled for %s (traces=%.2f, profile_session=%.2f, lifecycle=trace, logs=warning+)",
             component, traces_rate, profile_rate)
    return True
