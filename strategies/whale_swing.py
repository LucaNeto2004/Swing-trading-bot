"""
Whale swing strategy — evaluates a single bar against a deployed config and
returns an EntrySignal if an entry condition fires. No exit signals here —
exits are managed by core/execution.py (SL, TP1 partial, trail, max_hold).

Based on 58bro.eth + nervousdegen.eth wallet patterns. Per-symbol configs
are loaded from config/deployed/whale_<SYMBOL>.json.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import EntrySignal, SignalType


@dataclass
class WhaleSwingConfig:
    trend_filter: str           # 'ema_cross' | 'ema_slope' | 'ema200' | 'none'
    entry_type: str             # 'rsi_bounce' | 'bb_touch' | 'ema_bounce' | 'swing_pivot'
    rsi_oversold: float
    rsi_overbought: float
    sl_atr: float
    tp1_atr: float              # 0 = no partial TP
    tp1_pct: float              # e.g. 0.5 = close 50% at TP1
    trail_atr: float            # 0 = no trailing stop
    max_hold_bars: int          # 5m bars (288 = 1 day, 1440 = 5 days)
    direction: str              # 'long_only' | 'short_only' | 'both'
    use_1h_filter: bool

    @classmethod
    def from_json(cls, d: dict) -> "WhaleSwingConfig":
        return cls(
            trend_filter=d["trend_filter"],
            entry_type=d["entry_type"],
            rsi_oversold=float(d["rsi_oversold"]),
            rsi_overbought=float(d["rsi_overbought"]),
            sl_atr=float(d["sl_atr"]),
            tp1_atr=float(d["tp1_atr"]),
            tp1_pct=float(d["tp1_pct"]),
            trail_atr=float(d["trail_atr"]),
            max_hold_bars=int(d["max_hold_bars"]),
            direction=d["direction"],
            use_1h_filter=bool(d["use_1h_filter"]),
        )


class WhaleSwingStrategy:
    """Stateless per-bar evaluator. Call evaluate() with the latest closed bar."""

    def __init__(self, cfg: WhaleSwingConfig):
        self.cfg = cfg

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        up_1h_latest: bool,
        dn_1h_latest: bool,
    ) -> Optional[EntrySignal]:
        """Evaluate the latest closed 5m bar. Returns EntrySignal or None."""
        if len(df) < 55:
            return None
        cfg = self.cfg

        i = len(df) - 1  # latest closed bar
        bar = df.iloc[i]
        prev = df.iloc[i - 1]

        price = float(bar['close'])
        hi = float(bar['high']); lo = float(bar['low'])
        atr = float(bar['atr']); rsi = float(bar['rsi'])
        if pd.isna(atr) or atr <= 0 or pd.isna(rsi):
            return None
        r_prev = float(prev['rsi'])
        e21 = float(bar['ema_21']); e50 = float(bar['ema_50'])
        e200 = float(bar.get('ema_200', 0) or 0)
        slope = float(bar.get('ema_50_slope', 0) or 0)
        bb_lower = float(bar.get('bb_lower', 0) or 0)
        bb_upper = float(bar.get('bb_upper', 0) or 0)

        # 5m trend filter
        if cfg.trend_filter == 'ema_cross':
            up_ok, dn_ok = e21 > e50, e21 < e50
        elif cfg.trend_filter == 'ema_slope':
            up_ok, dn_ok = slope > 0, slope < 0
        elif cfg.trend_filter == 'ema200':
            up_ok = price > e200 if e200 > 0 else True
            dn_ok = price < e200 if e200 > 0 else True
        else:
            up_ok, dn_ok = True, True

        # 1h trend confirmation
        if cfg.use_1h_filter:
            if not up_1h_latest: up_ok = False
            if not dn_1h_latest: dn_ok = False

        # Direction filter
        if cfg.direction == 'long_only': dn_ok = False
        elif cfg.direction == 'short_only': up_ok = False

        long_trig = short_trig = False
        et = cfg.entry_type

        if et == 'rsi_bounce':
            long_trig = up_ok and r_prev < cfg.rsi_oversold and rsi >= cfg.rsi_oversold
            short_trig = dn_ok and r_prev > cfg.rsi_overbought and rsi <= cfg.rsi_overbought
        elif et == 'bb_touch':
            long_trig = up_ok and float(prev['low']) <= bb_lower and price > float(prev['high'])
            short_trig = dn_ok and float(prev['high']) >= bb_upper and price < float(prev['low'])
        elif et == 'ema_bounce':
            long_trig = up_ok and float(prev['low']) <= e21 and price > e21 and r_prev < 45
            short_trig = dn_ok and float(prev['high']) >= e21 and price < e21 and r_prev > 55
        elif et == 'swing_pivot':
            if i >= 4:
                p2 = df.iloc[i - 2]; p3 = df.iloc[i - 3]; p4 = df.iloc[i - 4]
                if up_ok and float(p3['low']) < float(p4['low']) and float(p3['low']) < float(p2['low']):
                    if price > float(prev['high']):
                        long_trig = True
                if dn_ok and float(p3['high']) > float(p4['high']) and float(p3['high']) > float(p2['high']):
                    if price < float(prev['low']):
                        short_trig = True

        if not (long_trig or short_trig):
            return None
        return EntrySignal(
            symbol=symbol,
            signal_type=SignalType.LONG if long_trig else SignalType.SHORT,
            entry_price=price,
            atr=atr,
            timestamp=bar['timestamp'].to_pydatetime() if hasattr(bar['timestamp'], 'to_pydatetime') else datetime.utcnow(),
            reason=f"{et} {'long' if long_trig else 'short'}",
            metadata={"rsi": rsi, "trend_filter": cfg.trend_filter},
        )
