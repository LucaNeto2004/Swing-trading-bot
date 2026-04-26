"""Replay the whale-swing strategy across the power-outage gap.

Gap: 2026-04-22 20:30 UTC (22:30 CEST) → 2026-04-23 04:40 UTC (06:40 CEST).
Missed 5m closes: 20:35 UTC Apr 22, 20:40, ..., 04:35 UTC Apr 23 (96 closes).

For each missed close, the script truncates pre-fetched 5m/1h/4h candles to
<= that timestamp and invokes DataManager.refresh + WhaleSwingStrategy.evaluate
exactly as main.py does. Reports any signal that would have fired.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config.settings import load_config, is_tradeable_now  # noqa: E402
from config.deployer import load_all  # noqa: E402
from core import data as data_mod  # noqa: E402
from core.data import DataManager, fetch_candles as real_fetch_candles  # noqa: E402
from strategies.whale_swing import WhaleSwingConfig, WhaleSwingStrategy  # noqa: E402


GAP_START_UTC = pd.Timestamp("2026-04-22 20:35:00", tz="UTC")
GAP_END_UTC = pd.Timestamp("2026-04-23 04:35:00", tz="UTC")


def pre_fetch(symbols: list[str], cfg) -> dict[str, dict[str, pd.DataFrame]]:
    """Fetch current 5m/1h/4h bars once per symbol and cache them."""
    cache: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in symbols:
        d5 = real_fetch_candles(sym, cfg.entry_tf, cfg.lookback_5m)
        d1 = real_fetch_candles(sym, cfg.trend_tf, cfg.lookback_1h)
        d4 = real_fetch_candles(sym, "4h", 300)
        cache[sym] = {"5m": d5, "1h": d1, "4h": d4}
        print(f"  cached {sym}: 5m={len(d5)} 1h={len(d1)} 4h={len(d4)}")
    return cache


def make_truncated_fetcher(cache: dict[str, dict[str, pd.DataFrame]], cutoff_ts: pd.Timestamp):
    """Return a fetch_candles replacement that serves cached bars <= cutoff."""

    def _fetch(symbol: str, interval: str, bars: int, retries: int = 3):
        df = cache.get(symbol, {}).get(interval)
        if df is None or df.empty:
            return df if df is not None else pd.DataFrame()
        # Keep only bars that closed at or before the cutoff. Bar timestamp is
        # the OPEN time, so a 5m bar with ts=cutoff-5m closed at cutoff.
        if interval == "5m":
            close_ts = df["timestamp"] + pd.Timedelta(minutes=5)
        elif interval == "1h":
            close_ts = df["timestamp"] + pd.Timedelta(hours=1)
        elif interval == "4h":
            close_ts = df["timestamp"] + pd.Timedelta(hours=4)
        else:
            close_ts = df["timestamp"]
        mask = close_ts <= cutoff_ts
        return df.loc[mask].tail(bars).reset_index(drop=True).copy()

    return _fetch


def replay() -> list[dict]:
    cfg = load_config()
    deployed = load_all()
    # Same symbol list as main.py uses (intersection of configured instruments
    # with what's deployed). We skip xyz:SILVER / xyz:CL etc for this replay
    # if they aren't in the active whale-swing cohort.
    symbols = [s for s in cfg.instruments.keys() if s in deployed]
    print(f"Symbols: {symbols}")

    # Build strategies
    strategies: dict[str, WhaleSwingStrategy] = {}
    for sym in symbols:
        strategies[sym] = WhaleSwingStrategy(WhaleSwingConfig.from_json(deployed[sym]))

    # Pre-fetch once
    print("Pre-fetching candles…")
    cache = pre_fetch(symbols, cfg)

    # Build the list of target 5m-close timestamps in the gap.
    closes = pd.date_range(GAP_START_UTC, GAP_END_UTC, freq="5min")
    print(f"Replaying {len(closes)} missed closes from {GAP_START_UTC} to {GAP_END_UTC}")

    signals: list[dict] = []
    for ts in closes:
        # Patch fetch_candles to truncate to this cutoff, then refresh + evaluate
        data_mod.fetch_candles = make_truncated_fetcher(cache, ts)
        dm = DataManager(cfg)
        # Refresh BTC first so btc_1h_dir is populated for BTC-gated symbols
        if "BTC" in strategies:
            dm.refresh("BTC")
        for sym, strat in strategies.items():
            try:
                dm.refresh(sym)
                df5 = dm.df_5m.get(sym)
                if df5 is None or df5.empty or len(df5) < 55:
                    continue

                filter_variant = getattr(strat.cfg, "trend_filter_1h", "ema_cross")
                up_1h, dn_1h = dm.latest_1h_regime(sym, filter_variant)
                if getattr(strat.cfg, "require_4h_agreement", False):
                    up_4h, dn_4h = dm.latest_4h_regime(sym)
                    up_1h = up_1h and up_4h
                    dn_1h = dn_1h and dn_4h
                ph_arr = dm.last_pivot_h_1h.get(sym)
                pl_arr = dm.last_pivot_l_1h.get(sym)
                last_ph = float(ph_arr[-1]) if ph_arr is not None and len(ph_arr) and not (ph_arr[-1] != ph_arr[-1]) else None
                last_pl = float(pl_arr[-1]) if pl_arr is not None and len(pl_arr) and not (pl_arr[-1] != pl_arr[-1]) else None

                up_1h_prev = dn_1h_prev = None
                if len(df5) >= 2:
                    def _prev(arr):
                        return bool(arr[-2]) if arr is not None and len(arr) >= 2 else None
                    if filter_variant == "structure":
                        up_1h_prev = _prev(dm.up_struct_1h.get(sym))
                        dn_1h_prev = _prev(dm.dn_struct_1h.get(sym))
                    elif filter_variant == "both_agree":
                        u_e = _prev(dm.up_1h.get(sym)); u_s = _prev(dm.up_struct_1h.get(sym))
                        d_e = _prev(dm.dn_1h.get(sym)); d_s = _prev(dm.dn_struct_1h.get(sym))
                        up_1h_prev = (u_e and u_s) if (u_e is not None and u_s is not None) else None
                        dn_1h_prev = (d_e and d_s) if (d_e is not None and d_s is not None) else None
                    elif filter_variant == "hma_slope":
                        up_1h_prev = _prev(dm.up_hma_1h.get(sym))
                        dn_1h_prev = _prev(dm.dn_hma_1h.get(sym))
                    elif filter_variant == "sjm":
                        up_1h_prev = _prev(dm.up_sjm_1h.get(sym))
                        dn_1h_prev = _prev(dm.dn_sjm_1h.get(sym))
                    elif filter_variant == "kalman":
                        up_1h_prev = _prev(dm.up_kalman_1h.get(sym))
                        dn_1h_prev = _prev(dm.dn_kalman_1h.get(sym))
                    else:
                        up_1h_prev = _prev(dm.up_1h.get(sym))
                        dn_1h_prev = _prev(dm.dn_1h.get(sym))

                ens_up_cnt, ens_dn_cnt, ens_up_prev, ens_dn_prev = dm.latest_ensemble_counts(sym)
                reg_label = dm.latest_regime_label(sym)
                piv_h_evt, piv_l_evt = dm.latest_pivot_event(sym)

                # trading-hours window (xyz:SILVER etc are calendar-restricted)
                tradeable, _reason = is_tradeable_now(sym)
                if not tradeable:
                    continue

                sig = strat.evaluate(
                    sym, df5, up_1h, dn_1h,
                    last_pivot_h=last_ph, last_pivot_l=last_pl,
                    up_1h_prev=up_1h_prev, dn_1h_prev=dn_1h_prev,
                    ens_up_cnt=ens_up_cnt, ens_dn_cnt=ens_dn_cnt,
                    ens_up_cnt_prev=ens_up_prev, ens_dn_cnt_prev=ens_dn_prev,
                    regime_label=reg_label,
                    pivot_h_event=piv_h_evt, pivot_l_event=piv_l_evt,
                )
                if sig is None:
                    continue

                # BTC 1h-confirm gate
                if getattr(strat.cfg, "require_btc_1h_confirm", False) and sym != "BTC":
                    want = 1 if sig.signal_type.value == "long" else -1
                    if dm.btc_1h_dir != want:
                        continue

                signals.append({
                    "ts_utc": str(ts),
                    "ts_sast": str(ts.tz_convert("Africa/Johannesburg")),
                    "symbol": sym,
                    "side": sig.signal_type.value,
                    "entry_price": float(sig.entry_price),
                    "entry_type": getattr(sig, "entry_type", ""),
                    "reason": getattr(sig, "reason", ""),
                })
                print(f"  [{ts}] {sym} {sig.signal_type.value} @ {sig.entry_price:.4f} — {getattr(sig, 'reason', '')}")
            except Exception as e:
                print(f"  [{ts}] {sym}: error {e}")

    # restore
    data_mod.fetch_candles = real_fetch_candles
    return signals


if __name__ == "__main__":
    results = replay()
    print("\n" + "=" * 60)
    if not results:
        print("No signals would have fired during the 22:30–06:40 CEST outage.")
    else:
        print(f"{len(results)} signal(s) would have fired in the gap:")
        for r in results:
            print(f"  {r['ts_sast']} | {r['symbol']} {r['side']} @ {r['entry_price']:.4f}  ({r['entry_type']}) — {r['reason']}")
