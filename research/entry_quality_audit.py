"""Retroactively measure entry-quality metrics on every historical trade.

For each active-universe trade, fetch the 5m bar at the estimated entry time
(exit_timestamp - held_bars * 5min) and compute:
  - 5m RSI at entry bar
  - 1h trend agreement (price > / < 1h EMA50)
  - Cumulative 5m move over last 12 bars (60min)  — proxy for "leg already moved"
  - Distance from 1h EMA50 in ATR multiples — overextension proxy

Then bucket trades by which filter they would have FAILED:
  filter_RSI: short with RSI<35 or long with RSI>65 → "fade-into-extreme"
  filter_dist: |distance_from_1h_ema50| > 2.0 ATR → "overextended"
  filter_legmove: cumulative move in trade direction over 12 bars > 1.5% → "late on leg"

For each filter, compare PF / WR / avg PnL between PASS and FAIL buckets.
A useful filter has FAIL bucket significantly worse than PASS bucket.
"""
from __future__ import annotations
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, "/Users/lucaneto/swing-trading-bot")

import pandas as pd
import numpy as np
from datetime import timedelta
from collections import defaultdict

from core.data import fetch_candles
from core.features import add_features

ACTIVE = {'BTC','ENA','ETH','FARTCOIN','HYPE','PENDLE','TIA','XRP','ZEC'}

ps = json.load(open('/Users/lucaneto/swing-trading-bot/data/paper_state.json'))
trades = [t for t in ps['trade_history']
          if t.get('symbol') in ACTIVE and t.get('entry_price')
          and t.get('held_bars') is not None]

print(f"Trades to analyze: {len(trades)}\n")


def parse_ts(s):
    return pd.Timestamp(str(s).replace('Z','+00:00').split('+')[0]).tz_localize('UTC')


# Cache fetched candles per symbol — avoid repeated HL calls
CANDLE_CACHE = {}
EMA50_CACHE = {}


def get_candles(symbol):
    if symbol not in CANDLE_CACHE:
        df5 = add_features(fetch_candles(symbol, '5m', 5000))
        df5['ts'] = pd.to_datetime(df5['timestamp'], utc=True)
        df5 = df5.sort_values('ts').reset_index(drop=True)
        df1h = fetch_candles(symbol, '1h', 1000)
        df1h['ts'] = pd.to_datetime(df1h['timestamp'], utc=True)
        df1h = df1h.sort_values('ts').reset_index(drop=True)
        df1h['ema50'] = df1h['close'].ewm(span=50, adjust=False).mean()
        df1h['atr'] = (df1h['high']-df1h['low']).rolling(14).mean()
        CANDLE_CACHE[symbol] = df5
        EMA50_CACHE[symbol] = df1h
    return CANDLE_CACHE[symbol], EMA50_CACHE[symbol]


def features_at_entry(t):
    """Return dict of entry-time features, or None if data unavailable."""
    sym = t['symbol']
    exit_ts = parse_ts(t['timestamp'])
    held = int(t.get('held_bars') or 0)
    entry_ts_est = exit_ts - timedelta(minutes=5*held)
    df5, df1h = get_candles(sym)
    # Find 5m bar matching entry_ts_est (closest within 5 min)
    df5_pre = df5[df5['ts'] <= entry_ts_est]
    if len(df5_pre) < 13:
        return None
    bar = df5_pre.iloc[-1]
    last12 = df5_pre.iloc[-13:-1]
    # cumulative move = (last_close - 12bar_ago_close) / 12bar_ago_close
    cum_move = (bar['close'] - last12['close'].iloc[0]) / last12['close'].iloc[0]
    # 1h EMA50 / ATR at entry time
    df1h_pre = df1h[df1h['ts'] <= entry_ts_est]
    if len(df1h_pre) < 50:
        return None
    h = df1h_pre.iloc[-1]
    dist_atr = (bar['close'] - h['ema50']) / max(h['atr'], 1e-9)
    return {
        'rsi5': bar.get('rsi', np.nan),
        'cum_move_pct': cum_move * 100,
        'dist_ema50_atr_1h': dist_atr,
        'price_at_entry': bar['close'],
    }


# ------------------------------------------------------------------
# Compute features for every trade
# ------------------------------------------------------------------
print("Computing entry features (this hits HL once per symbol)...")
enriched = []
for t in trades:
    f = features_at_entry(t)
    if f is None:
        continue
    enriched.append({
        'sym': t['symbol'], 'side': t['side'], 'pnl': t['pnl'],
        'reason': t.get('exit_reason'), 'held': int(t.get('held_bars') or 0),
        **f,
    })
print(f"  Enriched {len(enriched)} / {len(trades)} trades\n")


# ------------------------------------------------------------------
# Bucket by filter and compare
# ------------------------------------------------------------------
def stats(rows):
    if not rows:
        return {'n':0,'pnl':0,'wr':0,'pf':None,'avg':0}
    pnl = sum(r['pnl'] for r in rows)
    wins = [r['pnl'] for r in rows if r['pnl']>0]
    losses = [r['pnl'] for r in rows if r['pnl']<0]
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    return {'n':len(rows), 'pnl':pnl, 'wr':len(wins)/len(rows)*100,
            'pf':pf, 'avg':pnl/len(rows)}


def show(label, pass_rows, fail_rows):
    p = stats(pass_rows); f = stats(fail_rows)
    pf_p = f"{p['pf']:.2f}" if p['pf'] else '—'
    pf_f = f"{f['pf']:.2f}" if f['pf'] else '—'
    print(f"\n{label}")
    print(f"  PASS: n={p['n']:>3}  pnl=${p['pnl']:>+6.0f}  avg=${p['avg']:>+5.1f}  WR={p['wr']:.0f}%  PF={pf_p}")
    print(f"  FAIL: n={f['n']:>3}  pnl=${f['pnl']:>+6.0f}  avg=${f['avg']:>+5.1f}  WR={f['wr']:.0f}%  PF={pf_f}")
    if p['n'] and f['n']:
        print(f"  delta_avg: ${p['avg']-f['avg']:+.1f}/trade  (FAIL vs PASS)")


# ============= FILTER 1: RSI gate =============
# Short with RSI<35 = fade into oversold (bad)
# Long with RSI>65 = fade into overbought (bad)
fail_rsi = [r for r in enriched
            if (r['side']=='short' and r['rsi5']<35) or (r['side']=='long' and r['rsi5']>65)]
pass_rsi = [r for r in enriched if r not in fail_rsi]
show("FILTER 1 — RSI gate (block short<35 / long>65 at entry)", pass_rsi, fail_rsi)

# ============= FILTER 2: distance from 1h EMA50 =============
# Block entries where price > 2 ATR from 1h EMA50 in the trade direction
# (short far above EMA50 OK; short far below = chasing → bad)
def is_overextended(r):
    d = r['dist_ema50_atr_1h']
    if r['side']=='short' and d < -2.0: return True   # shorting below EMA50 by >2 ATR
    if r['side']=='long' and d > 2.0:   return True   # buying above EMA50 by >2 ATR
    return False
fail_dist = [r for r in enriched if is_overextended(r)]
pass_dist = [r for r in enriched if not is_overextended(r)]
show("FILTER 2 — Distance from 1h EMA50 (block >2 ATR overextended)", pass_dist, fail_dist)

# ============= FILTER 3: cumulative 12-bar move =============
# Short when last 12 bars already down >1.5% = late on the leg
def is_late(r):
    m = r['cum_move_pct']
    if r['side']=='short' and m < -1.5: return True   # already dumped 1.5% in last hour
    if r['side']=='long' and m > 1.5:   return True   # already pumped 1.5% in last hour
    return False
fail_late = [r for r in enriched if is_late(r)]
pass_late = [r for r in enriched if not is_late(r)]
show("FILTER 3 — Late-on-leg (block when last 12 bars already moved >1.5% in trade dir)", pass_late, fail_late)

# ============= COMBINED FILTER (any of the three) =============
def fails_any(r):
    rsi_fail = (r['side']=='short' and r['rsi5']<35) or (r['side']=='long' and r['rsi5']>65)
    return rsi_fail or is_overextended(r) or is_late(r)
fail_any = [r for r in enriched if fails_any(r)]
pass_any = [r for r in enriched if not fails_any(r)]
show("COMBINED — fail any of the 3 filters", pass_any, fail_any)

# ------------------------------------------------------------------
# Per-symbol breakdown of the WINNING filter
# ------------------------------------------------------------------
print("\n" + "="*72)
print("PER-SYMBOL — combined filter")
print("="*72)
print(f"{'sym':<10} {'pass_n':>6} {'pass_$':>8}  {'fail_n':>6} {'fail_$':>8}  {'fail_avg':>8}")
sym_buckets = defaultdict(lambda: {'pass':[], 'fail':[]})
for r in enriched:
    bucket = 'fail' if fails_any(r) else 'pass'
    sym_buckets[r['sym']][bucket].append(r)
for sym, b in sorted(sym_buckets.items()):
    p = stats(b['pass']); f = stats(b['fail'])
    print(f"{sym:<10} {p['n']:>6} ${p['pnl']:>+6.0f}  {f['n']:>6} ${f['pnl']:>+6.0f}  ${f['avg']:>+6.1f}")
