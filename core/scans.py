"""Lightweight scan triggers for the signals feed.

These are TELEMETRY triggers — not trade entries. Their job is to populate
the dashboard's "signals feed" with interesting-bar markers (bb_touch,
ema_cross, vol_spike, etc.) so we can see what the bot is watching in real
time, even when nothing fires as a trade.

Emission is edge-based: a trigger only fires on the bar where the condition
first becomes true (e.g. RSI just dipped below 30, EMA9 just crossed EMA21).
That keeps the feed sparse — the mockup's ~15–20 events over 6 hours, not
180 per hour.

Designed to be called once per symbol per new 5m bar close from main.py.
"""
from __future__ import annotations

import pandas as pd


def detect_scan_triggers(df5: pd.DataFrame) -> list[tuple[str, str]]:
    """Return a list of (trigger_type, side) for edge events on the last bar.

    Side is "LONG" or "SHORT". An empty list means the bar wasn't interesting.
    Requires 5m dataframe with: close, open, high, low, volume (optional),
    bb_upper, bb_mid, bb_lower, ema_9, ema_21, rsi. Missing columns are
    tolerated — the relevant trigger just skips.
    """
    if df5 is None or len(df5) < 21:
        return []
    last = df5.iloc[-1]
    prev = df5.iloc[-2]
    close = float(last["close"])
    if close <= 0:
        return []
    out: list[tuple[str, str]] = []

    # bb_touch — last close within 0.3% of a Bollinger band
    bu = last.get("bb_upper"); bl = last.get("bb_lower")
    if bl and bl == bl and abs(close - float(bl)) / close < 0.003:
        out.append(("bb_touch", "LONG"))
    if bu and bu == bu and abs(close - float(bu)) / close < 0.003:
        out.append(("bb_touch", "SHORT"))

    # ema_cross — EMA9 just crossed EMA21 between prev and last bar
    pe9 = prev.get("ema_9"); pe21 = prev.get("ema_21")
    le9 = last.get("ema_9"); le21 = last.get("ema_21")
    if (pe9 is not None and pe21 is not None and le9 is not None and le21 is not None
            and pe9 == pe9 and pe21 == pe21 and le9 == le9 and le21 == le21):
        if float(pe9) <= float(pe21) and float(le9) > float(le21):
            out.append(("ema_cross", "LONG"))
        elif float(pe9) >= float(pe21) and float(le9) < float(le21):
            out.append(("ema_cross", "SHORT"))

    # bb_squeeze — current BB width at 20-bar minimum (±5% tolerance)
    if bu is not None and bl is not None and bu == bu and bl == bl:
        bbw = (df5["bb_upper"] - df5["bb_lower"]).tail(20).dropna()
        if len(bbw) >= 15:
            cur = float(bu) - float(bl)
            if cur > 0 and cur <= bbw.min() * 1.05:
                bm = last.get("bb_mid")
                side = "LONG" if (bm is not None and close >= float(bm)) else "SHORT"
                out.append(("bb_squeeze", side))

    # vol_spike — last volume > 2.0× 20-bar mean (if volume is present)
    if "volume" in df5.columns:
        vol_tail = df5["volume"].tail(20).dropna()
        if len(vol_tail) >= 10:
            vmean = float(vol_tail.mean())
            vlast = float(last.get("volume") or 0)
            if vmean > 0 and vlast > 2.0 * vmean:
                side = "LONG" if close >= float(last.get("open", close)) else "SHORT"
                out.append(("vol_spike", side))

    # rsi_oversld / rsi_overbt — edge transition only
    rnow = last.get("rsi"); rprev = prev.get("rsi")
    if rnow is not None and rprev is not None and rnow == rnow and rprev == rprev:
        rnow = float(rnow); rprev = float(rprev)
        if rnow <= 30 and rprev > 30:
            out.append(("rsi_oversld", "LONG"))
        if rnow >= 70 and rprev < 70:
            out.append(("rsi_overbt", "SHORT"))

    # breakout — close punches through prior 20-bar high/low (excluding current bar)
    prior = df5.iloc[-21:-1]
    if len(prior) >= 20:
        ph = float(prior["high"].max())
        pl = float(prior["low"].min())
        if close > ph:
            out.append(("breakout", "LONG"))
        elif close < pl:
            out.append(("breakout", "SHORT"))

    return out


# ------------------------------------------------------------------
# Strength score
# ------------------------------------------------------------------
# 0.0–1.0 composite — what the dashboard renders as the 0–100 strength bar.

_TRIGGER_BONUS = {
    "breakout": 10,
    "vol_spike": 8,
    "ema_cross": 7,
    "bb_touch": 6,
    "rsi_oversld": 6,
    "rsi_overbt": 6,
    "bb_squeeze": 5,
}


def score_trigger(
    trigger: str,
    side: str,
    ens_up: int | None,
    ens_dn: int | None,
    regime: str | None,
    rsi: float | None,
) -> float:
    """Return a 0.0–1.0 confidence score for a scan event."""
    match = (ens_up if side == "LONG" else ens_dn) or 0
    opp = (ens_dn if side == "LONG" else ens_up) or 0
    # 40 pts for 1h ensemble agreement (5/5 = full)
    score = 40.0 * (max(match, 0) / 5.0)
    # 20 pts for matching trend regime, 10 for range
    want_trend = "trend_up" if side == "LONG" else "trend_down"
    if regime == want_trend:
        score += 20
    elif regime == "range":
        score += 10 if trigger in ("bb_touch", "bb_squeeze", "rsi_oversld", "rsi_overbt") else 5
    # Opposing consensus penalty
    if opp >= 3:
        score -= 15
    # RSI zone bonus (12 if at the extreme that matches the side, 10 if in mid zone)
    if rsi is not None and rsi == rsi:
        r = float(rsi)
        if side == "LONG":
            if r <= 30: score += 12
            elif 30 < r <= 55: score += 10
        else:
            if r >= 70: score += 12
            elif 45 <= r < 70: score += 10
    # Trigger-specific bonus
    score += _TRIGGER_BONUS.get(trigger, 0)
    score = max(0.0, min(100.0, score))
    return round(score / 100.0, 3)
