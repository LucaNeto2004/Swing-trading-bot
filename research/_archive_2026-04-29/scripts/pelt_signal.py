"""Causal PELT (Pruned Exact Linear Time) changepoint signal for the
ensemble-regime strategy.

PELT detects structural breaks in the 1h close series. We convert each
detected changepoint into a directional vote:
  - At bar t, find the most-recent changepoint cp that is "detectable" by t
    (i.e. cp + MIN_SEG <= t, so PELT has enough post-cp data to call it).
  - Vote UP if mean(close[cp:t+1]) > mean(close[cp_prev:cp]), else DN.
  - Before any detectable changepoint, both votes are False (neutral).

Then align to the entry-timeframe bars (5m live / 15m backtest) the same
causal way as our other 1h lookups: use the PREVIOUS CLOSED 1h bar.

Adds one vote to the ensemble. Total filter count becomes 6.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import ruptures as rpt
    HAS_RUPTURES = True
except ImportError:
    HAS_RUPTURES = False


# Defaults — keep these fixed to avoid per-asset overfit.
PELT_MODEL   = "rbf"     # scale-agnostic kernel; no assumption on noise dist
PELT_PEN     = 10.0      # BIC-like penalty; larger → fewer changepoints
PELT_MIN_SEG = 20        # minimum segment length (1h bars)


def _causal_pelt_direction_1h(closes_1h: np.ndarray,
                              penalty: float = PELT_PEN,
                              min_seg: int = PELT_MIN_SEG) -> tuple[np.ndarray, np.ndarray]:
    """Return (up_1h, dn_1h) boolean arrays, same length as closes_1h.

    Each bar's vote is determined by the direction of the mean shift at the
    most-recent PELT changepoint whose DETECTION time (cp + min_seg) is <=
    the current bar — this is the causality safeguard. Before any detectable
    changepoint, both votes are False.
    """
    n = len(closes_1h)
    up = np.zeros(n, dtype=bool)
    dn = np.zeros(n, dtype=bool)
    if not HAS_RUPTURES or n < 3 * min_seg:
        return up, dn

    try:
        algo = rpt.Pelt(model=PELT_MODEL, min_size=min_seg, jump=1).fit(closes_1h)
        bkps = algo.predict(pen=penalty)  # list of 1-indexed breakpoints ending at n
    except Exception:
        return up, dn

    # Convert ruptures bkps to 0-indexed segment-start list.
    # bkps[-1] == n (by convention). A changepoint at index c means segment
    # [prev_c, c) and [c, next_c).
    cps = [0] + [b for b in bkps[:-1]]  # segment starts

    # For each bar t, find the MOST-RECENT cp whose detection (cp + min_seg)
    # has already happened by t, i.e. cp + min_seg <= t.
    # Walk cps in order; at each t, advance the cursor while next cp's
    # detection time <= t.
    cursor = 0  # index into cps
    for t in range(n):
        # Advance cursor to the latest cp whose detection time <= t.
        while cursor + 1 < len(cps) and cps[cursor + 1] + min_seg <= t:
            cursor += 1
        cp = cps[cursor]
        # Need a previous segment to compare against. If cursor == 0, we only
        # have one segment — no direction yet.
        if cursor == 0:
            continue
        prev_cp = cps[cursor - 1]
        if t - cp < 1 or cp - prev_cp < 1:
            continue
        mean_cur  = float(np.mean(closes_1h[cp:t + 1]))
        mean_prev = float(np.mean(closes_1h[prev_cp:cp]))
        if mean_cur > mean_prev:
            up[t] = True
        elif mean_cur < mean_prev:
            dn[t] = True
        # mean_cur == mean_prev → neutral (both False)
    return up, dn


def pelt_lookup_1h(df_entry: pd.DataFrame, df_1h: pd.DataFrame,
                   penalty: float = PELT_PEN,
                   min_seg: int = PELT_MIN_SEG) -> tuple[np.ndarray, np.ndarray]:
    """Per-entry-bar PELT direction votes.

    Computes 1h-level PELT votes, then aligns each entry-timeframe bar to the
    PREVIOUS CLOSED 1h bar (same causal alignment as other *_lookup_1h
    functions — no look-ahead into the 1h bar that contains the current
    entry bar)."""
    n = len(df_entry)
    up = np.zeros(n, dtype=bool)
    dn = np.zeros(n, dtype=bool)
    if df_1h is None or df_1h.empty or n < 2:
        return up, dn

    closes_1h = df_1h["close"].to_numpy(dtype=np.float64)
    up_1h, dn_1h = _causal_pelt_direction_1h(closes_1h, penalty=penalty, min_seg=min_seg)

    ts_1h = df_1h["timestamp"].to_numpy()
    ts_5m = df_entry["timestamp"].to_numpy()
    raw = np.searchsorted(ts_1h, ts_5m, side="right")
    idx = np.clip(raw - 2, 0, len(ts_1h) - 1)
    before = raw <= 1
    up = up_1h[idx].copy()
    dn = dn_1h[idx].copy()
    up[before] = False
    dn[before] = False
    return up, dn
