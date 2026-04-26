"""Strategy-distance telemetry for the signals feed.

Given a symbol's deployed config + current data state, returns a short
readable label telling the user what the SYMBOL'S actual strategy is waiting
for — distinct from the scan's generic "this bar is interesting" score.

Three kinds of answers:
  - `armed` — would fire on this bar close
  - `N away` / `awaiting X` — conditions partially met, could fire soon
  - `dormant` — strategy cannot fire from current state (e.g. ensemble edge
     already passed, now sitting at 5→5 with no transition)
  - `already open` — symbol has a live position, strategy won't re-enter

Kept deliberately narrow — reads the same state variables the scan emit
block already collects (ens counts, regime, pivot events, side constraints).
No re-computation.
"""
from __future__ import annotations


def _ensemble_distance(
    side: str, K: int,
    ens_up: int | None, ens_dn: int | None,
    ens_up_prev: int | None, ens_dn_prev: int | None,
) -> str:
    cur = (ens_up if side == "LONG" else ens_dn) or 0
    prev = (ens_up_prev if side == "LONG" else ens_dn_prev) or 0
    if cur >= K and prev < K:
        return f"armed (K={K} transition this bar)"
    if cur >= K and prev >= K:
        return f"dormant (K={K} transition already passed, {cur}→{cur})"
    if cur < K:
        gap = K - cur
        return f"{gap} filter{'s' if gap != 1 else ''} away (cnt {cur}, need {K})"
    return "—"


def _pullback_distance(side: str, regime: str | None, direction: str) -> str:
    if direction == "long_only" and side == "SHORT":
        return "blocked (long_only config)"
    if direction == "short_only" and side == "LONG":
        return "blocked (short_only config)"
    # Valid side + regime combinations that allow firing
    want_pivot = "pivot_L" if side == "LONG" else "pivot_H"
    valid_regimes = ("trend_up", "range") if side == "LONG" else ("trend_down", "range")
    if regime not in valid_regimes:
        return f"dormant (regime {regime or '?'} — needs {' or '.join(valid_regimes)})"
    return f"awaiting validated {want_pivot}"


def _bos_distance(side: str, regime: str | None) -> str:
    want_break = "pivot_H ↑" if side == "LONG" else "pivot_L ↓"
    want_regime = "trend_up" if side == "LONG" else "trend_down"
    if regime and regime not in (want_regime, "range"):
        return f"dormant (regime {regime})"
    return f"awaiting close through {want_break}"


def _swing_pivot_distance(side: str) -> str:
    return f"awaiting 1h swing-{'low' if side == 'LONG' else 'high'} confirmation"


def _bb_touch_distance(side: str) -> str:
    return f"awaiting BB {'lower' if side == 'LONG' else 'upper'} touch + reversal"


def strategy_distance(
    entry_type: str,
    side: str,
    cfg: dict | object,
    ens_up: int | None = None,
    ens_dn: int | None = None,
    ens_up_prev: int | None = None,
    ens_dn_prev: int | None = None,
    regime: str | None = None,
    has_open_position: bool = False,
) -> str:
    """Return a short human-readable label describing strategy proximity."""
    if has_open_position:
        return "position open"

    # cfg can be a dict (loaded JSON) or a dataclass instance; normalise access.
    def _g(key, default=None):
        if isinstance(cfg, dict):
            return cfg.get(key, default)
        return getattr(cfg, key, default)

    direction = _g("direction", "both")
    if direction == "long_only" and side == "SHORT":
        return "blocked (long_only)"
    if direction == "short_only" and side == "LONG":
        return "blocked (short_only)"

    et = (entry_type or "").strip()
    if et == "ensemble_regime":
        K = int(_g("ensemble_k", 4))
        return _ensemble_distance(side, K, ens_up, ens_dn, ens_up_prev, ens_dn_prev)
    if et == "pullback_in_regime":
        return _pullback_distance(side, regime, direction)
    if et == "bos_structural":
        return _bos_distance(side, regime)
    if et == "swing_pivot":
        return _swing_pivot_distance(side)
    if et == "bb_touch":
        return _bb_touch_distance(side)
    if et == "test_bounce":
        return f"awaiting pivot_{'L' if side == 'LONG' else 'H'} touch (fade)"
    if et == "rsi_bounce":
        return f"awaiting RSI cross-back ({'from oversold' if side == 'LONG' else 'from overbought'})"
    if et == "ema_bounce":
        return f"awaiting EMA21 {'bounce' if side == 'LONG' else 'rejection'}"
    if et == "regime_flip":
        return "awaiting 1h regime flip"
    return f"{et or '?'}"
