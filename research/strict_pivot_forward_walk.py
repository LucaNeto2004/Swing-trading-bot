"""Forward-walk validation for the strict_pivot strategy.

Per gate.json v1: 5 rolling 30-day OOS windows. Each window must satisfy:
  - n_trades >= 15
  - PF >= 1.5
  - PnL > 0
  - 0 negative quartiles within the window
  - PF beats random by >= 0.6

Plus bootstrap CI on aggregate WR — 95% CI lower bound must exceed 50%.

If all 5 windows pass AND bootstrap floor passes → candidate is forward-walk
validated and READY for shadow paper deployment.
"""
import os, sys, json
sys.path.insert(0, '/Users/lucaneto/swing-trading-bot')
import numpy as np
import pandas as pd
from datetime import timedelta
from collections import defaultdict, deque

from core.data import fetch_candles
from core.features import add_features
from core.quant_filters import combined_pivots_1h
from research.intensive_grid import hl_max_leverage
from research.commod_backtest import (trend_lookup_1h, structure_lookup_1h,
                                       hma_slope_lookup_1h, sjm_lookup_1h, kalman_slope_lookup_1h)
from scripts.chart_server import _active_filter_for
from research.forward_walk import bootstrap_ci, load_gate
from strategies.strict_pivot import (
    DEPLOY_WHITELIST, RSI_HIGH_THRESHOLD, RSI_LOW_THRESHOLD,
    PIVOT_LOOKBACK_BARS, ATR_MIN_MOVE, VOL_SPIKE, RSI_VALIDATOR,
)

COMMISSION = 0.00030
MARGIN_PCT = 0.05
MAX_CONCURRENT = 4
PER_SYMBOL_24H_CAP = 0.02
START_BALANCE = 10000.0
SL_ATR = 2.0
TP_ATR = 3.0
MAX_HOLD_1H = 24


def gather_strict(symbol, n_bars=4000):
    """Return list of strict pivot signals for a symbol's 1h history."""
    df_raw = fetch_candles(symbol, '1h', n_bars)
    if len(df_raw) < 200:
        return None, None
    df = add_features(df_raw)
    valid_h, valid_l = combined_pivots_1h(
        df, fractal_lookback=PIVOT_LOOKBACK_BARS, smoothed_lookback=2,
        atr_min_move=ATR_MIN_MOVE, vol_spike=VOL_SPIKE, rsi_extreme=RSI_VALIDATOR,
    )
    fv, _ = _active_filter_for(symbol)
    if fv == 'sjm': up, dn = sjm_lookup_1h(df, df)
    elif fv == 'hma_slope': up, dn = hma_slope_lookup_1h(df, df)
    elif fv == 'structure': up, dn = structure_lookup_1h(df, df)
    elif fv == 'kalman': up, dn = kalman_slope_lookup_1h(df, df)
    else: up, dn = trend_lookup_1h(df, df)

    rsi = df['rsi'].to_numpy(); high = df['high'].to_numpy(); low = df['low'].to_numpy()
    bb_u = df['bb_upper'].to_numpy(); bb_l = df['bb_lower'].to_numpy()
    atr = df['atr'].to_numpy(); close = df['close'].to_numpy()
    events = []
    for confirm_ts, pivot_bar, level, _ in valid_h:
        cb = pivot_bar + PIVOT_LOOKBACK_BARS
        if cb >= len(df) or atr[cb] <= 0: continue
        if not (rsi[pivot_bar] > RSI_HIGH_THRESHOLD): continue
        if np.isnan(bb_u[pivot_bar]) or high[pivot_bar] < bb_u[pivot_bar]: continue
        if not dn[cb]: continue
        events.append({
            'ts': pd.Timestamp(df['timestamp'].iloc[cb]),
            'sym': symbol, 'side': 'short',
            'entry_price': close[cb], 'entry_atr': atr[cb], 'entry_idx': cb,
        })
    for confirm_ts, pivot_bar, level, _ in valid_l:
        cb = pivot_bar + PIVOT_LOOKBACK_BARS
        if cb >= len(df) or atr[cb] <= 0: continue
        if not (rsi[pivot_bar] < RSI_LOW_THRESHOLD): continue
        if np.isnan(bb_l[pivot_bar]) or low[pivot_bar] > bb_l[pivot_bar]: continue
        if not up[cb]: continue
        events.append({
            'ts': pd.Timestamp(df['timestamp'].iloc[cb]),
            'sym': symbol, 'side': 'long',
            'entry_price': close[cb], 'entry_atr': atr[cb], 'entry_idx': cb,
        })
    return events, df


def simulate_window(signals, sym_dfs, window_start, window_end):
    """Simulate trades for signals within [window_start, window_end].
    Returns list of closed trade dicts."""
    in_window = [s for s in signals if window_start <= s['ts'] <= window_end]
    balance = START_BALANCE; peak = balance
    open_pos = []; closed = []
    sym_window = defaultdict(deque)

    def update_to(now_ts):
        nonlocal balance, peak
        still = []
        for pos in open_pos:
            df = sym_dfs[pos['sym']]
            close_arr = df['close'].to_numpy(); high = df['high'].to_numpy(); low = df['low'].to_numpy()
            ts = df['timestamp']
            ex=None; ep=None; rs=None
            mx = min(pos['entry_idx'] + 1 + MAX_HOLD_1H, len(df))
            for j in range(pos['entry_idx'] + 1, mx):
                bts = pd.Timestamp(ts.iloc[j])
                if bts > now_ts: break
                if pos['side']=='short':
                    if high[j] >= pos['sl']: ex,ep,rs = j,pos['sl'],'sl'; break
                    if low[j] <= pos['tp']:  ex,ep,rs = j,pos['tp'],'tp'; break
                else:
                    if low[j] <= pos['sl']:  ex,ep,rs = j,pos['sl'],'sl'; break
                    if high[j] >= pos['tp']: ex,ep,rs = j,pos['tp'],'tp'; break
            if ex is None and now_ts >= pd.Timestamp(ts.iloc[mx-1]):
                ex = mx-1; ep = close_arr[ex]; rs = 'max_hold'
            if ex is not None:
                size = pos['notional'] / pos['entry_price']
                pnl = (pos['entry_price']-ep)*size if pos['side']=='short' else (ep-pos['entry_price'])*size
                pnl -= pos['notional'] * COMMISSION * 2
                balance += pnl
                if balance > peak: peak = balance
                exit_ts = pd.Timestamp(ts.iloc[ex])
                closed.append({**pos, 'exit_price':ep,'pnl':pnl,'reason':rs,'exit_ts':exit_ts})
                sym_window[pos['sym']].append((exit_ts, pnl))
            else:
                still.append(pos)
        return still

    def is_capped(sym, now_ts):
        win = sym_window[sym]
        cutoff = now_ts - timedelta(hours=24)
        while win and win[0][0] < cutoff: win.popleft()
        return sum(p for _,p in win) <= -peak * PER_SYMBOL_24H_CAP

    for sig in in_window:
        open_pos = update_to(sig['ts'])
        if len(open_pos) >= MAX_CONCURRENT: continue
        if is_capped(sig['sym'], sig['ts']): continue
        sym = sig['sym']
        notional = balance * MARGIN_PCT * hl_max_leverage(sym)
        ep = sig['entry_price']; a = sig['entry_atr']
        sl = ep + SL_ATR*a if sig['side']=='short' else ep - SL_ATR*a
        tp = ep - TP_ATR*a if sig['side']=='short' else ep + TP_ATR*a
        open_pos.append({**sig, 'sl':sl, 'tp':tp, 'notional':notional})
    if open_pos:
        last_ts = window_end + timedelta(hours=MAX_HOLD_1H + 1)
        open_pos = update_to(last_ts)
    return closed


def quartile_split(trades):
    """Split trades into 4 chronological quartiles, return list of dicts."""
    if not trades: return []
    n = len(trades); k = n // 4
    if k == 0: return []
    quartiles = []
    for i in range(4):
        lo = i * k; hi = (i + 1) * k if i < 3 else n
        sub = trades[lo:hi]
        wins = sum(1 for t in sub if t['pnl'] > 0)
        losses = sum(1 for t in sub if t['pnl'] < 0)
        gross_w = sum(t['pnl'] for t in sub if t['pnl'] > 0)
        gross_l = -sum(t['pnl'] for t in sub if t['pnl'] < 0)
        pf = gross_w / gross_l if gross_l > 0 else None
        quartiles.append({'q': i+1, 'n': len(sub), 'pnl': sum(t['pnl'] for t in sub),
                          'pf': pf, 'wr': wins/(wins+losses)*100 if (wins+losses) else 0})
    return quartiles


def random_benchmark(signals, sym_dfs, window_start, window_end, n_runs=5):
    """Random-entry benchmark — same window, random side selection."""
    np.random.seed(42)
    pf_list = []
    for run in range(n_runs):
        rand_signals = []
        for s in signals:
            if window_start <= s['ts'] <= window_end:
                # Randomize side
                rs = dict(s)
                rs['side'] = np.random.choice(['long', 'short'])
                rand_signals.append(rs)
        trades = simulate_window(rand_signals, sym_dfs, window_start, window_end)
        gross_w = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        gross_l = -sum(t['pnl'] for t in trades if t['pnl'] < 0)
        pf_list.append(gross_w / gross_l if gross_l > 0 else 0)
    return float(np.mean(pf_list)) if pf_list else 0


def main():
    gate = load_gate()
    g = gate['deployment_gate']
    print(f"Strict Pivot — Forward-Walk Validation (gate v{gate['_meta']['version']})")
    print(f"Whitelist: {sorted(DEPLOY_WHITELIST)}")
    print(f"Gate: n>={g['min_oos_n_trades']}, PF>={g['min_oos_pf']}, "
          f"all-quartiles-positive, beats-random-by-{g['min_pf_above_random']}\n")

    print("Fetching 1h × 4000 bars per symbol...")
    all_signals = []; sym_dfs = {}
    for sym in sorted(DEPLOY_WHITELIST):
        evts, df = gather_strict(sym, 4000)
        if evts is None: continue
        sym_dfs[sym] = df
        all_signals.extend(evts)
        print(f"  {sym}: {len(evts)} signals over {(df['timestamp'].iloc[-1]-df['timestamp'].iloc[0]).days}d")
    all_signals.sort(key=lambda e: e['ts'])

    if not all_signals:
        print("No signals — abort"); return

    # Build 5 rolling 30-day windows
    last_ts = max(s['ts'] for s in all_signals)
    n_windows = gate['rolling_validation_windows']
    window_days = gate['rolling_window_days']
    step_days = gate['rolling_window_step_days']

    windows = []
    for i in range(n_windows):
        end_ts = last_ts - timedelta(days=i * step_days)
        start_ts = end_ts - timedelta(days=window_days)
        windows.append((start_ts, end_ts))
    windows.reverse()  # oldest first

    print(f"\n{'='*80}")
    print(f"Running {n_windows} rolling {window_days}d windows (step {step_days}d)")
    print(f"{'='*80}")

    all_passed = True
    all_window_results = []
    aggregate_wr_list = []

    for i, (ws, we) in enumerate(windows):
        print(f"\n[Window {i+1}] {ws.date()} → {we.date()}")
        trades = simulate_window(all_signals, sym_dfs, ws, we)
        if not trades:
            print("  no trades — FAIL")
            all_passed = False
            all_window_results.append({'pass': False, 'reason': 'no trades'})
            continue
        wins = [t for t in trades if t['pnl']>0]
        losses = [t for t in trades if t['pnl']<0]
        gross_w = sum(t['pnl'] for t in wins)
        gross_l = -sum(t['pnl'] for t in losses)
        pf = gross_w / gross_l if gross_l > 0 else None
        pnl = sum(t['pnl'] for t in trades)
        wr = len(wins) / (len(wins)+len(losses)) * 100 if (wins or losses) else 0
        quartiles = quartile_split(trades)
        rand_pf = random_benchmark(all_signals, sym_dfs, ws, we)

        # Gate checks
        reasons = []
        if len(trades) < g['min_oos_n_trades']:
            reasons.append(f"n {len(trades)} < {g['min_oos_n_trades']}")
        if (pf or 0) < g['min_oos_pf']:
            reasons.append(f"PF {pf} < {g['min_oos_pf']}")
        if pnl < g['min_oos_pnl_dollars']:
            reasons.append(f"pnl ${pnl:.0f} <= 0")
        neg_q = sum(1 for q in quartiles if q['pnl'] < 0)
        if neg_q > g['max_neg_quartiles']:
            reasons.append(f"{neg_q} negative quartiles > {g['max_neg_quartiles']}")
        if (pf or 0) - rand_pf < g['min_pf_above_random']:
            reasons.append(f"PF over random {(pf or 0) - rand_pf:.2f} < {g['min_pf_above_random']}")

        passed = len(reasons) == 0
        all_passed = all_passed and passed
        pf_s = f"{pf:.2f}" if pf else "—"
        print(f"  n={len(trades)}  PF={pf_s}  WR={wr:.0f}%  $={pnl:+.0f}  rand_pf={rand_pf:.2f}")
        print(f"  quartiles: " + " ".join(f"${q['pnl']:+.0f}" for q in quartiles))
        print(f"  → {'PASS ✓' if passed else 'FAIL ✗ — ' + ', '.join(reasons)}")
        aggregate_wr_list.extend([wr])
        all_window_results.append({
            'window': f"{ws.date()}→{we.date()}", 'n': len(trades), 'pnl': pnl, 'pf': pf,
            'wr': wr, 'quartiles': quartiles, 'rand_pf': rand_pf,
            'pass': passed, 'reasons': reasons,
        })

    # Bootstrap CI on aggregate WR
    bs_mean, bs_lo, bs_hi = bootstrap_ci(aggregate_wr_list)
    print(f"\n{'='*80}")
    print(f"FINAL VERDICT")
    print(f"{'='*80}")
    print(f"Windows passed: {sum(1 for r in all_window_results if r['pass'])}/{n_windows}")
    if bs_mean is not None:
        print(f"Bootstrap WR mean: {bs_mean:.1f}%  95% CI [{bs_lo:.1f}%, {bs_hi:.1f}%]")
        ci_passes = bs_lo > 50.0
        print(f"  CI lower > 50%? {'YES ✓' if ci_passes else 'NO ✗'}")
    else:
        ci_passes = False
        print(f"  Insufficient data for bootstrap")

    final_pass = all_passed and ci_passes
    print(f"\n{'✓ FORWARD-WALK PASS — strategy is candidate-ready' if final_pass else '✗ FORWARD-WALK FAIL — DO NOT DEPLOY'}")

    out = '/tmp/strict_pivot_forward_walk.json'
    with open(out, 'w') as f:
        json.dump({
            'verdict': 'PASS' if final_pass else 'FAIL',
            'windows': all_window_results,
            'bootstrap_wr_ci': [bs_mean, bs_lo, bs_hi],
            'whitelist': sorted(DEPLOY_WHITELIST),
        }, f, indent=2, default=str)
    print(f"\nFull → {out}")


if __name__ == "__main__":
    main()
