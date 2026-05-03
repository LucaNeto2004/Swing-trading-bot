"""Counterfactual replay of every choch_exit in active-universe trade history.

For each historical choch_exit trade:
  - Estimate the entry timestamp (exit_ts - held_bars * 5min)
  - Fetch 5m bars from exit_ts forward (up to max_hold_remaining bars)
  - Simulate three variants:
      A) MFE gate: only honor choch if max_favorable_atr >= 0.5 * sl_atr_mult
      B) Confirmation bar: only honor choch if next bar closes beyond the new
         swing direction (proxy: first post-exit bar continues in choch direction)
      C) Min-displacement: SKIPPED (needs structure detector — re-run is heavy)

Counterfactual: if a variant would BLOCK the exit, simulate forward to either
SL hit, TP1 (2*entry-SL distance), or max_hold_remaining bars elapsing.
"""
from __future__ import annotations
import os, sys, json
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + "/../Trading/swing-trading-bot"))
sys.path.insert(0, "/Users/lucaneto/swing-trading-bot")

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

from core.data import fetch_candles

ACTIVE = {'BTC','ENA','ETH','FARTCOIN','HYPE','PENDLE','TIA','XRP','ZEC'}
COMMISSION = 0.0003   # per side

ps = json.load(open('/Users/lucaneto/swing-trading-bot/data/paper_state.json'))
ch = [t for t in ps['trade_history']
      if t.get('exit_reason') == 'choch_exit' and t.get('symbol') in ACTIVE]

print(f"choch_exit trades to evaluate: {len(ch)}\n")

# Per-symbol config to know SL multiple + TP1 multiple
CONFIGS = {}
for sym in ACTIVE:
    p = f'/Users/lucaneto/swing-trading-bot/config/deployed/whale_{sym}.json'
    if os.path.exists(p):
        CONFIGS[sym] = json.load(open(p))


def parse_ts(s):
    return pd.Timestamp(str(s).replace('Z','+00:00').split('+')[0]).tz_localize('UTC')


def simulate_forward(symbol: str, exit_ts: pd.Timestamp,
                     entry_price: float, initial_sl: float, side: str,
                     max_bars: int = 120) -> dict:
    """Replay 5m bars from exit_ts forward. Returns the counterfactual outcome.

    Stops on first of: SL hit, TP1 hit (=2x entry-SL distance), or max_bars elapsed.
    R-distance = |entry - initial_sl|. TP1 = entry +/- 2*R for ladder configs.
    """
    cfg = CONFIGS.get(symbol, {})
    sl_atr = float(cfg.get('sl_atr', 2.0))
    tp1_atr = float(cfg.get('tp1_atr', 2.0))
    tp1_pct = float(cfg.get('tp1_pct', 0.3))

    if initial_sl <= 0 or entry_price <= 0:
        return {'reason': 'no_sl', 'pnl_per_unit': 0.0}

    # Fetch enough 5m candles - we need bars AFTER exit_ts
    # Typical lookback gives us 1000 bars; we need to make sure exit_ts isn't
    # too old (HL retains many days at 5m).
    df = fetch_candles(symbol, '5m', 1000)
    df['ts'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.sort_values('ts').reset_index(drop=True)

    # First bar starting AFTER exit_ts
    after = df[df['ts'] > exit_ts].head(max_bars)
    if len(after) == 0:
        return {'reason': 'no_data', 'pnl_per_unit': 0.0, 'bars_avail': 0}

    r_dist = abs(entry_price - initial_sl)  # 1R distance
    # TP1 price using cfg's tp1_atr / sl_atr ratio scaled to r_dist
    tp1_dist = r_dist * (tp1_atr / sl_atr) if sl_atr > 0 else r_dist
    if side == 'long':
        sl_px = entry_price - r_dist
        tp1_px = entry_price + tp1_dist
    else:
        sl_px = entry_price + r_dist
        tp1_px = entry_price - tp1_dist

    tp1_hit_pct = 0.0
    sl_hit = False
    last_close = None
    bars_to = 0
    for _, bar in after.iterrows():
        bars_to += 1
        hi, lo, cl = bar['high'], bar['low'], bar['close']
        last_close = cl
        # TP1 first if both could hit in same bar (optimistic)
        if not tp1_hit_pct and tp1_pct > 0:
            if (side == 'long' and hi >= tp1_px) or (side == 'short' and lo <= tp1_px):
                tp1_hit_pct = tp1_pct  # close 30% at TP1 (per cfg)
        # SL
        if (side == 'long' and lo <= sl_px) or (side == 'short' and hi >= sl_px):
            sl_hit = True
            break
    # PnL per unit (size = 1)
    if sl_hit:
        if tp1_hit_pct > 0:
            # Took partial profit then runner stopped at BE-or-SL.
            # After TP1, SL moves to BE in execution.py. So runner: 0R.
            tp1_r = tp1_dist
            pnl_pu = (tp1_r * tp1_hit_pct) + 0.0 * (1 - tp1_hit_pct)
            reason = 'tp1_then_be'
        else:
            pnl_pu = -r_dist
            reason = 'sl'
    else:
        # Closed at last close after max_bars
        if side == 'long':
            move = last_close - entry_price
        else:
            move = entry_price - last_close
        if tp1_hit_pct > 0:
            tp1_r = tp1_dist
            # After TP1, SL → BE. If we didn't hit BE-stop, runner pnl =
            # remaining_pct * move. Otherwise BE = 0.
            # We didn't hit SL above, so runner = (1 - tp1_pct) * move.
            pnl_pu = (tp1_r * tp1_hit_pct) + move * (1 - tp1_hit_pct)
            reason = f'tp1_then_close@{bars_to}'
        else:
            pnl_pu = move
            reason = f'close@{bars_to}'
    # Subtract round-trip commission (entry+exit on full size)
    pnl_pu -= entry_price * COMMISSION * 2
    return {'reason': reason, 'pnl_per_unit': pnl_pu, 'bars_to': bars_to,
            'tp1_hit': tp1_hit_pct > 0, 'sl_hit': sl_hit, 'last_close': last_close}


# ------------------------------------------------------------------
# Variant evaluators
# ------------------------------------------------------------------
def eval_variant_A(t, sim):
    """MFE gate: keep choch_exit only if MFE >= 0.5 * sl_atr_mult."""
    cfg = CONFIGS.get(t['symbol'], {})
    sl_atr = float(cfg.get('sl_atr', 2.0))
    mfe = float(t.get('favorable_excursion_atr') or 0)
    if mfe >= 0.5 * sl_atr:
        return ('honored', t['pnl'])
    # Block exit, use simulated outcome
    if sim['pnl_per_unit'] is None:
        return ('blocked_no_data', t['pnl'])
    cf_pnl = sim['pnl_per_unit'] * float(t['size'])
    return ('blocked', cf_pnl)


def eval_variant_B(t, sim, df_pre_after):
    """Confirmation bar: honor choch only if first bar after exit continues
    in choch direction. choch_exit on a long means dn_struct flipped, so the
    'continuation' direction is DOWN. Heuristic proxy: did the bar after exit
    close further from entry in the AGAINST-trade direction?
    """
    if sim.get('reason') in ('no_sl', 'no_data'):
        return ('blocked_no_data', t['pnl'])
    side = t['side']
    last = sim.get('last_close')
    # Use first bar's close (we sampled bars_to=1 minimum):
    # Approximation: if bars_to >= 1, the immediate first bar's behaviour
    # determines confirmation. Since we don't have that bar's close cleanly
    # here, use a simpler rule: did the trade hit SL in <=2 bars?
    # If yes → choch was right (blocked exit would've hit SL anyway).
    # If no  → choch was wrong, block worked.
    if sim.get('sl_hit') and sim.get('bars_to', 99) <= 2:
        return ('honored', t['pnl'])  # choch was right, fast SL
    cf_pnl = sim['pnl_per_unit'] * float(t['size'])
    return ('blocked', cf_pnl)


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
results = []
print(f"{'symbol':<10} {'side':<5} {'live_pnl':>9} {'cf_reason':<22} {'cf_pnl':>9}  variantA  variantB")
print("-" * 90)
for t in ch:
    sym = t['symbol']
    side = t['side']
    if t.get('entry_price') is None or t.get('initial_sl') is None:
        print(f"{sym:<10} {side:<5} ${t['pnl']:>+7.2f}  SKIP (missing entry_price/sl in old record)")
        continue
    exit_ts = parse_ts(t['timestamp'])
    sim = simulate_forward(sym, exit_ts, float(t['entry_price']),
                           float(t.get('initial_sl') or 0), side)
    cf_pnl_full = sim.get('pnl_per_unit', 0) * float(t['size'])

    A_status, A_pnl = eval_variant_A(t, sim)
    B_status, B_pnl = eval_variant_B(t, sim, None)
    results.append({
        'sym': sym, 'side': side, 'live_pnl': t['pnl'],
        'cf_reason': sim['reason'], 'cf_pnl': cf_pnl_full,
        'A_status': A_status, 'A_pnl': A_pnl,
        'B_status': B_status, 'B_pnl': B_pnl,
        'mfe_atr': float(t.get('favorable_excursion_atr') or 0),
    })
    print(f"{sym:<10} {side:<5} ${t['pnl']:>+7.2f}  {sim['reason']:<22} ${cf_pnl_full:>+7.2f}  "
          f"{A_status:<8} ${A_pnl:>+7.2f}  {B_status:<8} ${B_pnl:>+7.2f}")

print()
print("=" * 70)
print("AGGREGATE")
print("=" * 70)
total_live = sum(r['live_pnl'] for r in results)
total_A = sum(r['A_pnl'] for r in results)
total_B = sum(r['B_pnl'] for r in results)
total_cf = sum(r['cf_pnl'] for r in results)
print(f"  Baseline (live choch_exit):         ${total_live:>+8.2f}")
print(f"  Variant A — MFE gate (>=0.5R):      ${total_A:>+8.2f}  (delta vs live: ${total_A-total_live:+.2f})")
print(f"  Variant B — confirmation proxy:     ${total_B:>+8.2f}  (delta vs live: ${total_B-total_live:+.2f})")
print(f"  Counterfactual (block ALL choch):   ${total_cf:>+8.2f}  (delta vs live: ${total_cf-total_live:+.2f})")
print()
print(f"  Trades blocked under A: {sum(1 for r in results if r['A_status']=='blocked')}/{len(results)}")
print(f"  Trades blocked under B: {sum(1 for r in results if r['B_status']=='blocked')}/{len(results)}")
print()
print("Sample size: 8 trades — too small for statistical confidence.")
print("Use this as DIRECTIONAL SIGNAL only; need full forward-walk in backtester.")
