"""Load strategy configuration from config.yaml.

All tunable parameters are centralized in config.yaml.
Import `cfg` from this module to access them anywhere.

Usage:
    from config import cfg
    if price > cfg["entry_price"]["max"]:
        skip()
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"

_defaults = {
    "strategy_version": "v0",
    "conviction": {"min_leaders": 4, "window_hours": 6, "solo_elite_enabled": False},
    "leader_weighting": {"enabled": False, "min_trades_for_weight": 5, "default_weight": 1.0, "weighted_min": 4.0},
    "entry_price": {"min": 0.25, "max": 0.80},
    "sizing": {
        "starting_bankroll": 10000.0, "base_risk_frac": 0.01,
        "max_per_trade_frac": 0.05, "max_total_exposure": 0.70,
        "min_trade_usd": 1.0, "conviction_multiplier": 2.0,
    },
    "resolution": {"max_hours": 24},
    "categories": {"blocked": ["unknown"]},
    "concentration": {"max_positions_per_event": 2},
    "liquidity": {"min_usd": 5000, "max_slippage_bps": 300},
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user = yaml.safe_load(f) or {}
        merged = _deep_merge(_defaults, user)
        log.info("config loaded: strategy %s", merged.get("strategy_version", "?"))
        return merged
    else:
        log.warning("config.yaml not found, using defaults")
        return _defaults.copy()


cfg = load_config()
