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
    tp1_pct: float              # e.g. 0.3 = close 30% at TP1 (multi-tier default)
    trail_atr: float            # 0 = no trailing stop
    max_hold_bars: int          # 5m bars (288 = 1 day, 1440 = 5 days)
    direction: str              # 'long_only' | 'short_only' | 'both'
    use_1h_filter: bool
    # Multi-tier scale-out — optional, 0 = disabled. Default recipe is
    #   TP1 30% at tp1_atr, TP2 30% at tp2_atr, TP3 20% at tp3_atr,
    #   remaining 20% rides the structural SL. Matches whale behavior
    #   observed in 58bro / nervousdegen fill patterns (many small closes).
    tp2_atr: float = 0.0
    tp2_pct: float = 0.0
    tp3_atr: float = 0.0
    tp3_pct: float = 0.0
    # 1h filter variant. Default stays 'ema_cross' for backwards-compat with
    # existing deployed configs that don't set this field.
    #   'ema_cross'  — EMA21(1h) vs EMA50(1h) crossover (lagging)
    #   'structure'  — ICT state machine (BOS / CHoCH on confirmed 1h pivots)
    #   'both_agree' — AND gate: BOTH ema_cross and structure must agree on
    #                  direction before entry is allowed. Highest quality,
    #                  fewest trades.
    trend_filter_1h: str = "ema_cross"
    # If True, 4h ICT structure must ALSO agree on direction. Adds the HTF
    # macro-regime gate on top of the 1h filter. Per-symbol — only use on
    # symbols where backtest confirmed 4h filter improves PF.
    require_4h_agreement: bool = False
    # If True, require that BTC's last closed 1h bar moved in the same direction
    # as the intended entry (log-return sign). Derived from ADT 2026-04-20 on 166
    # days of 1h returns — BTC shows 1h leadership over SOL (+0.04), ETH (+0.04),
    # ENA (+0.03). Enable per symbol where the backtest confirmed PF lift.
    require_btc_1h_confirm: bool = False
    # exit_type: "standard" (SL + TP ladder + trail + max_hold — default),
    # "bos_structural" (exit on opposing-pivot break, no TP/SL),
    # "bos_hybrid" (TP1 partial at tp1_atr, rest rides to structural exit),
    # "regime_flip" (exit when filter turns off the trade's direction),
    # "ensemble_hybrid" (TP1 partial + exit when 5-filter consensus drops
    #  below K-1 for the trade's direction).
    exit_type: str = "standard"
    # ensemble_regime entry / ensemble_hybrid exit parameters. Active only
    # when entry_type=="ensemble_regime" or exit_type=="ensemble_hybrid".
    # 5 filters: ema_cross, structure, hma_slope, sjm, kalman.
    # K=threshold number of agreeing filters to fire an entry (crosses-up
    # consensus). require_bos_confirm=True → also need a pivot break.
    ensemble_k: int = 4
    require_bos_confirm: bool = False
    # Regime gate for test_bounce (fade) strategy. When the 5-filter ensemble
    # has >= test_regime_max_opp filters agreeing AGAINST the fade direction,
    # skip the entry. Default 4 means: don't short at pivot_H if 4+ filters
    # say UP (strong trend — fading it is dumb). Don't long at pivot_L if 4+
    # filters say DN. Prevents the "short in a green regime" disaster.
    test_regime_max_opp: int = 4

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
            trend_filter_1h=d.get("trend_filter_1h", "ema_cross"),
            require_4h_agreement=bool(d.get("require_4h_agreement", False)),
            require_btc_1h_confirm=bool(d.get("require_btc_1h_confirm", False)),
            tp2_atr=float(d.get("tp2_atr", 0.0)),
            tp2_pct=float(d.get("tp2_pct", 0.0)),
            tp3_atr=float(d.get("tp3_atr", 0.0)),
            tp3_pct=float(d.get("tp3_pct", 0.0)),
            exit_type=d.get("exit_type", "standard"),
            ensemble_k=int(d.get("ensemble_k", 4)),
            require_bos_confirm=bool(d.get("require_bos_confirm", False)),
            test_regime_max_opp=int(d.get("test_regime_max_opp", 4)),
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
        last_pivot_h: Optional[float] = None,
        last_pivot_l: Optional[float] = None,
        up_1h_prev: Optional[bool] = None,
        dn_1h_prev: Optional[bool] = None,
        ens_up_cnt: Optional[int] = None,
        ens_dn_cnt: Optional[int] = None,
        ens_up_cnt_prev: Optional[int] = None,
        ens_dn_cnt_prev: Optional[int] = None,
        regime_label: Optional[str] = None,
        pivot_h_event: Optional[bool] = None,
        pivot_l_event: Optional[bool] = None,
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
        elif et == 'bos_structural':
            # BOS: close > most-recent confirmed 1h pivot H → long (need up regime).
            # Mirror for short. Trigger only on the crossing bar (prev close
            # must have been on the other side of the pivot).
            prev_close = float(prev['close'])
            if up_ok and last_pivot_h is not None and prev_close <= last_pivot_h and price > last_pivot_h:
                long_trig = True
            if dn_ok and last_pivot_l is not None and prev_close >= last_pivot_l and price < last_pivot_l:
                short_trig = True
        elif et == 'regime_flip':
            # Enter on fresh transition into UP or DN regime (the filter just
            # turned on for the trade's direction). prev_up/prev_dn are the
            # filter states from the PREVIOUS 5m bar.
            if up_ok and up_1h_prev is False:
                long_trig = True
            if dn_ok and dn_1h_prev is False:
                short_trig = True
        elif et == 'test_bounce':
            # Fade pattern — buy a bounce off a confirmed 1h pivot L, short a
            # rejection at a confirmed 1h pivot H. REGIME-GATED: skip the
            # entry when the 5-filter ensemble shows >= test_regime_max_opp
            # filters agreeing AGAINST the fade direction. Prevents "short
            # in a green regime" catastrophes.
            TOL = 0.003
            MAX_OPP = cfg.test_regime_max_opp
            if (last_pivot_l is not None
                    and lo <= last_pivot_l * (1 + TOL)
                    and lo >= last_pivot_l * (1 - TOL)
                    and price > last_pivot_l
                    and cfg.direction != 'short_only'):
                # Regime gate: don't buy at support if strong downtrend (>= MAX_OPP
                # filters say DN). Unknown counts (None) = pass-through.
                if ens_dn_cnt is None or ens_dn_cnt < MAX_OPP:
                    long_trig = True
            if (last_pivot_h is not None
                    and hi >= last_pivot_h * (1 - TOL)
                    and hi <= last_pivot_h * (1 + TOL)
                    and price < last_pivot_h
                    and cfg.direction != 'long_only'):
                # Regime gate: don't short at resistance if strong uptrend.
                if ens_up_cnt is None or ens_up_cnt < MAX_OPP:
                    short_trig = True
        elif et == 'pullback_in_regime':
            # Regime-aligned pivot pullback — buy-low-sell-high done right.
            #   trend_up + validated pivot_L → LONG (buy the dip)
            #   trend_down + validated pivot_H → SHORT (sell the rally)
            #   range + pivot_L → LONG · range + pivot_H → SHORT (fade both)
            #   chop → NO trade
            # By construction impossible to short in green or long in red.
            if pivot_l_event and regime_label in ("trend_up", "range"):
                if cfg.direction != 'short_only':
                    long_trig = True
            if pivot_h_event and regime_label in ("trend_down", "range"):
                if cfg.direction != 'long_only':
                    short_trig = True
        elif et == 'ensemble_regime':
            # 5-filter consensus — ema_cross, structure, hma_slope, sjm, kalman.
            # Enter when the count of agreeing filters crosses UP through K
            # (prev_count < K AND cur_count >= K). Direction-aware: up-count
            # for longs, dn-count for shorts. If require_bos_confirm=True, the
            # most-recent 1h pivot must also be broken in the trade direction.
            K = cfg.ensemble_k
            if (ens_up_cnt is not None and ens_up_cnt_prev is not None
                    and up_ok and ens_up_cnt >= K and ens_up_cnt_prev < K):
                long_trig = True
            if (ens_dn_cnt is not None and ens_dn_cnt_prev is not None
                    and dn_ok and ens_dn_cnt >= K and ens_dn_cnt_prev < K):
                short_trig = True
            if cfg.require_bos_confirm:
                if long_trig and (last_pivot_h is None or price <= last_pivot_h):
                    long_trig = False
                if short_trig and (last_pivot_l is None or price >= last_pivot_l):
                    short_trig = False

        if not (long_trig or short_trig):
            return None
        return EntrySignal(
            symbol=symbol,
            signal_type=SignalType.LONG if long_trig else SignalType.SHORT,
            entry_price=price,
            atr=atr,
            timestamp=bar['timestamp'].to_pydatetime() if hasattr(bar['timestamp'], 'to_pydatetime') else datetime.utcnow(),
            reason=f"{et} {'long' if long_trig else 'short'}",
            metadata={"rsi": rsi, "trend_filter": cfg.trend_filter,
                      "last_pivot_h": last_pivot_h, "last_pivot_l": last_pivot_l},
        )
