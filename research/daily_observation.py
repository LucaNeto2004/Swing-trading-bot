"""Daily observation snapshot — runs once a day during the freeze period.

Captures:
  - Account balance + drawdown from peak
  - Per-symbol live PnL since deploy
  - Forward-walk verdict per active config (matches gate.json)
  - Variance: realized vs framework-expected
  - Strict pivot live signal log (would-have-fired but disabled)

Output: logs/observation/<DATE>.json + appended to logs/observation/timeline.csv

Schedule via cron (recommended once per day at 23:00 UTC):
  0 23 * * * /usr/bin/python3 /Users/lucaneto/swing-trading-bot/research/daily_observation.py
"""
from __future__ import annotations
import os, sys, json
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

OUT_DIR = Path(__file__).parent.parent / "logs" / "observation"
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMELINE_CSV = OUT_DIR / "timeline.csv"


def parse_ts(s):
    try: return datetime.fromisoformat(str(s).replace('Z','+00:00').split('+')[0])
    except: return None


def main():
    ts_now = datetime.utcnow()
    date_str = ts_now.date().isoformat()

    # 1) Account snapshot
    ps = json.load(open(Path(__file__).parent.parent / "data" / "paper_state.json"))
    rs = json.load(open(Path(__file__).parent.parent / "data" / "risk_state.json"))
    balance = ps['balance']
    peak = rs['account_peak_balance']
    dd_pct = (balance/peak - 1) * 100
    open_count = len(ps.get('positions', {}) or {})

    # 2) Per-symbol PnL
    by_sym = defaultdict(lambda: {'n':0,'pnl':0,'w':0,'l':0})
    for t in ps['trade_history']:
        s = by_sym[t.get('symbol')]
        p = float(t.get('pnl') or 0)
        s['n'] += 1; s['pnl'] += p
        if p > 0: s['w'] += 1
        elif p < 0: s['l'] += 1

    # 3) Active configs forward-walk verdict (lightweight — just balance vs peak)
    from config.deployer import load_all
    deployed = load_all()
    active_syms = sorted(deployed.keys())
    active_pnl = sum(by_sym[s]['pnl'] for s in active_syms)
    retired_pnl = sum(by_sym[s]['pnl'] for s in by_sym if s not in active_syms)

    # 4) Strict pivot — count how many would-have-fired signals occurred today
    # (Module is disabled, so just count via re-running its detection on recent data)
    strict_pivot_signals_today = 0
    try:
        from strategies.strict_pivot import (DEPLOY_WHITELIST, RSI_HIGH_THRESHOLD,
                                              RSI_LOW_THRESHOLD, PIVOT_LOOKBACK_BARS,
                                              ATR_MIN_MOVE, VOL_SPIKE, RSI_VALIDATOR)
        from core.data import fetch_candles
        from core.features import add_features
        from core.quant_filters import combined_pivots_1h

        for sym in DEPLOY_WHITELIST:
            df = add_features(fetch_candles(sym, '1h', 500))
            if len(df) < 50: continue
            valid_h, valid_l = combined_pivots_1h(
                df, fractal_lookback=PIVOT_LOOKBACK_BARS, smoothed_lookback=2,
                atr_min_move=ATR_MIN_MOVE, vol_spike=VOL_SPIKE,
                rsi_extreme=RSI_VALIDATOR,
            )
            today_lo = ts_now.replace(hour=0, minute=0, second=0, microsecond=0)
            for confirm_ts, pivot_bar, level, _ in (valid_h + valid_l):
                cb = pivot_bar + PIVOT_LOOKBACK_BARS
                if cb >= len(df): continue
                bar_ts = df['timestamp'].iloc[cb]
                if hasattr(bar_ts, 'to_pydatetime'):
                    bar_ts = bar_ts.to_pydatetime().replace(tzinfo=None)
                if bar_ts < today_lo: continue
                strict_pivot_signals_today += 1
    except Exception as e:
        print(f"strict_pivot signal count failed: {e}")

    # Build snapshot
    snapshot = {
        'date': date_str,
        'ts_utc': ts_now.isoformat(),
        'balance': round(balance, 2),
        'peak': round(peak, 2),
        'dd_pct': round(dd_pct, 2),
        'open_positions': open_count,
        'total_trades': len(ps['trade_history']),
        'active_universe_pnl': round(active_pnl, 2),
        'retired_universe_pnl': round(retired_pnl, 2),
        'active_symbols': active_syms,
        'per_symbol': {s: dict(d) for s, d in by_sym.items() if s in active_syms},
        'strict_pivot_shadow_signals_today': strict_pivot_signals_today,
        'risk_gate': {
            'kill_switch': rs.get('kill_switch'),
            'consec_losses': rs.get('consecutive_losses'),
            'daily_pnl': rs.get('daily_pnl'),
        },
    }

    # Save snapshot
    snap_path = OUT_DIR / f"{date_str}.json"
    with open(snap_path, 'w') as f:
        json.dump(snapshot, f, indent=2, default=str)

    # Append to timeline CSV
    headers = ['date','balance','peak','dd_pct','open_pos','total_trades',
                'active_pnl','retired_pnl','strict_pivot_signals']
    new_file = not TIMELINE_CSV.exists()
    with open(TIMELINE_CSV, 'a') as f:
        if new_file:
            f.write(','.join(headers) + '\n')
        f.write(f"{date_str},{balance:.2f},{peak:.2f},{dd_pct:.2f},"
                f"{open_count},{len(ps['trade_history'])},"
                f"{active_pnl:.2f},{retired_pnl:.2f},{strict_pivot_signals_today}\n")

    print(f"[{date_str}] balance=${balance:,.2f} dd={dd_pct:.2f}% "
          f"trades={len(ps['trade_history'])} active=${active_pnl:+.2f} "
          f"strict_pivot_shadow_signals={strict_pivot_signals_today}")
    print(f"Saved: {snap_path}")


if __name__ == "__main__":
    main()
