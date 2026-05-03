"""Forward-walk validation — replaces single IS/OOS split with rolling windows.

The 2026-04-29 lesson: TIA passed a single OOS window with n=10 trades, then
failed the same gate 24h later when the window shifted by 96 bars. Single-window
results have a noise floor that exceeds the signal we're trying to detect.

This module runs N rolling out-of-sample windows of size W bars each, stepped
S bars apart. A config is only considered validated if it passes the gate
across ALL N windows — eliminating single-window luck.

Inputs:
  - cfg: research.commod_backtest.Cfg
  - arr: precomputed feature array
  - lev: effective leverage
  - n_windows: number of rolling OOS windows (default from gate.json)
  - window_days: size of each OOS window in days
  - step_days: step between consecutive windows

Output:
  - dict with per-window stats AND aggregate verdict
  - bootstrap CIs on the aggregate
  - PASS only if every window meets the gate
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

import research.commod_backtest as cb
import research.commod_oos as oos
from research.commod_backtest import Cfg, backtest, stats

GATE_PATH = Path(__file__).parent / "gate.json"


def load_gate():
    return json.load(open(GATE_PATH))


def bars_per_day(arr) -> float:
    """Estimate bars/day from the timestamp array."""
    ts = arr["timestamp"]
    if len(ts) < 2:
        return 96  # default 15m → 96/day
    diff = ts[-1] - ts[0]
    # Convert to seconds robustly across numpy/pandas types
    try:
        span_s = diff.total_seconds()  # pandas Timedelta
    except AttributeError:
        try:
            span_s = float(diff / np.timedelta64(1, "s"))  # np.timedelta64
        except Exception:
            return 96
    return len(ts) / max(1.0, span_s / 86400.0)


def bootstrap_ci(values: list[float], n_boot: int = 1000, ci: float = 0.95):
    """Bootstrap percentile CI for the mean of `values`."""
    if not values:
        return (None, None, None)
    arr = np.array(values, dtype=float)
    if len(arr) == 1:
        return (float(arr[0]), float(arr[0]), float(arr[0]))
    boot_means = []
    rng = np.random.default_rng(42)
    for _ in range(n_boot):
        sample = rng.choice(arr, size=len(arr), replace=True)
        boot_means.append(sample.mean())
    boot = np.array(boot_means)
    lo = np.percentile(boot, (1 - ci) / 2 * 100)
    hi = np.percentile(boot, (1 + ci) / 2 * 100)
    return (float(arr.mean()), float(lo), float(hi))


def window_check(window_stats, gate):
    """Check a single window against gate criteria. Returns (pass_bool, reasons)."""
    g = gate["deployment_gate"]
    reasons = []
    s = window_stats
    if s["n"] < g["min_oos_n_trades"]:
        reasons.append(f"n {s['n']} < {g['min_oos_n_trades']}")
    if (s["pf"] or 0) < g["min_oos_pf"]:
        reasons.append(f"PF {s['pf']} < {g['min_oos_pf']}")
    if s["pnl"] < g["min_oos_pnl_dollars"]:
        reasons.append(f"pnl ${s['pnl']:.0f} < ${g['min_oos_pnl_dollars']:.2f}")
    return len(reasons) == 0, reasons


def forward_walk(cfg: Cfg, arr, lev: float,
                 n_windows: int | None = None,
                 window_days: int | None = None,
                 step_days: int | None = None,
                 verbose: bool = False) -> dict:
    """Run forward-walk validation. Returns per-window + aggregate results."""
    gate = load_gate()
    n_windows = n_windows or gate["rolling_validation_windows"]
    window_days = window_days or gate["rolling_window_days"]
    step_days = step_days or gate["rolling_window_step_days"]

    bpd = bars_per_day(arr)
    win_bars = int(window_days * bpd)
    step_bars = int(step_days * bpd)
    n = len(arr["close"])

    # Place windows ending at: n, n - step, n - 2*step, ...
    windows = []
    for i in range(n_windows):
        end_idx = n - i * step_bars
        start_idx = end_idx - win_bars
        if start_idx < 52:
            break
        windows.append((start_idx, end_idx))
    windows.reverse()  # oldest first

    if verbose:
        print(f"  forward_walk: {len(windows)} windows of {window_days}d, step {step_days}d")

    # Run full backtest once, then slice per window
    all_trades = backtest(arr, cfg, lev)
    ts_arr = arr["timestamp"]

    per_window = []
    pass_count = 0
    pf_list = []
    pnl_list = []
    n_list = []
    for start_idx, end_idx in windows:
        t_start = ts_arr[start_idx]
        t_end = ts_arr[min(end_idx, n - 1)]
        # Trades that ENTERED in this window
        win_trades = [t for t in all_trades
                      if t.get("ts") is not None and t_start <= t["ts"] < t_end]
        win_s = stats(win_trades)
        passed, reasons = window_check(win_s, gate)
        if passed:
            pass_count += 1
        per_window.append({
            "start": str(t_start)[:10], "end": str(t_end)[:10],
            "n": win_s["n"], "pnl": win_s["pnl"], "pf": win_s["pf"],
            "wr": win_s.get("wr"), "pass": passed, "fail_reasons": reasons,
        })
        if win_s["n"] > 0:
            pf_list.append(win_s["pf"] or 0.0)
            pnl_list.append(win_s["pnl"])
            n_list.append(win_s["n"])

    # Aggregate
    pf_mean, pf_lo, pf_hi = bootstrap_ci(pf_list)
    pnl_mean, pnl_lo, pnl_hi = bootstrap_ci(pnl_list)
    n_total = sum(n_list)

    full_s = stats(all_trades)
    out = {
        "n_windows": len(windows),
        "windows_passed": pass_count,
        "all_windows_passed": pass_count == len(windows) and len(windows) >= n_windows,
        "per_window": per_window,
        "aggregate": {
            "total_n": n_total,
            "pf_mean": pf_mean, "pf_ci_95": [pf_lo, pf_hi],
            "pnl_mean": pnl_mean, "pnl_ci_95": [pnl_lo, pnl_hi],
        },
        "full_sample": {
            "n": full_s["n"], "pnl": full_s["pnl"], "pf": full_s["pf"],
        },
        "verdict": {
            "pass": pass_count == len(windows) and len(windows) >= n_windows,
            "summary": (f"Passed {pass_count}/{len(windows)} windows" +
                        ("" if pass_count == len(windows) else " — does NOT meet forward-walk standard")),
        },
    }
    if verbose:
        print(f"  → {out['verdict']['summary']}")
        for w in per_window:
            mark = "✓" if w["pass"] else "✗"
            pf_s = f"{w['pf']:.2f}" if w['pf'] else "—"
            print(f"    {mark} {w['start']}→{w['end']}  n={w['n']:>2}  $={w['pnl']:>+5.0f}  PF={pf_s}")
        if pf_mean is not None:
            print(f"    bootstrap PF mean {pf_mean:.2f} 95%CI [{pf_lo:.2f}, {pf_hi:.2f}]")
            print(f"    bootstrap PnL mean ${pnl_mean:+.0f} 95%CI [${pnl_lo:+.0f}, ${pnl_hi:+.0f}]")
    return out


# ============================================================
# CLI: validate a single deployed config
# ============================================================
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="symbol whose deployed config to validate")
    args = ap.parse_args()

    from config.deployer import load_all
    from research.commod_backtest import fetch_hl, add_features, precompute
    from research.intensive_grid import hl_max_leverage

    cb.TIME_STOP_ENABLED = False  # use config-as-deployed; time-stop is currently OFF

    deployed = load_all()
    if args.symbol not in deployed:
        print(f"No deployed config for {args.symbol}")
        sys.exit(1)
    d = deployed[args.symbol]
    cfg = Cfg(
        trend_filter=d.get("trend_filter","ema_slope"),
        entry_type=d["entry_type"],
        rsi_oversold=float(d.get("rsi_oversold",30)), rsi_overbought=float(d.get("rsi_overbought",70)),
        sl_atr=float(d.get("sl_atr",2.0)),
        tp1_atr=float(d.get("tp1_atr",0)), tp1_pct=float(d.get("tp1_pct",0)),
        tp2_atr=float(d.get("tp2_atr",0)), tp2_pct=float(d.get("tp2_pct",0)),
        tp3_atr=float(d.get("tp3_atr",0)), tp3_pct=float(d.get("tp3_pct",0)),
        trail_atr=float(d.get("trail_atr",0)),
        max_hold_bars=int(d.get("max_hold_bars",1000)),
        direction=d.get("direction","both"),
        use_1h_filter=bool(d.get("use_1h_filter",False)),
        trend_filter_1h=d.get("trend_filter_1h","ema_cross"),
        require_4h_agreement=bool(d.get("require_4h_agreement",False)),
        ensemble_k=int(d.get("ensemble_k",4)),
        require_bos_confirm=bool(d.get("require_bos_confirm",False)),
        exit_type=d.get("exit_type","standard"),
    )

    print(f"Forward-walk validation: {args.symbol} (config: {d.get('config','?')})")
    print(f"Fetching data...")
    d15 = add_features(fetch_hl(args.symbol, "15m", 4000))
    d1h = add_features(fetch_hl(args.symbol, "1h", 2000))
    d4h = add_features(fetch_hl(args.symbol, "4h", 1000))
    arr = precompute(d15, d1h, d4h)
    if not args.symbol.startswith("xyz:"):
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
    lev = hl_max_leverage(args.symbol) * 0.15

    result = forward_walk(cfg, arr, lev, verbose=True)

    out_path = f"/tmp/forward_walk_{args.symbol.replace(':','_')}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nFull result → {out_path}")


if __name__ == "__main__":
    main()
