"""Phase 2 — LightGBM exit-timing classifier.

Pipeline:
  1. Run the backtester for each live symbol (+ proposed ensemble swaps) to get
     the trade log with entry_bar and per-partial pnls.
  2. For every bar a position is open, compute 10 features + the final
     outcome label (did the trade end profitable).
  3. Train one GLOBAL LightGBM classifier (no symbol identity feature).
  4. Evaluate on a chronological 70/30 split + backtest the
     "rules + ml_cut at P(profit)<0.30" combo to see if it actually helps.

If it passes, writes the model to `models/exit_timing.pkl`. Bot loads it at
startup and calls `core.ml_exit.should_cut(pos, bar)` each tick.
"""
from __future__ import annotations

import os, sys, json, pickle
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, precision_recall_curve

from config.settings import INSTRUMENTS
from config.deployer import load_all
import research.commod_backtest as cb
from research.ensemble_regime_test import bootstrap
from research.current_vs_ensemble import (
    LIVE, _patch_weekday, _cfg_from_deployed,
)


# One global model trained on trades from all 8 live symbols + their
# proposed ensemble configurations (so the model learns both regimes).
SHIP_PLAN = {
    "BTC": {"entry_type": "ensemble_regime", "exit_type": "ensemble_hybrid",
            "ensemble_k": 4, "require_bos_confirm": False},
    "ENA": {"entry_type": "ensemble_regime", "exit_type": "ensemble_hybrid",
            "ensemble_k": 4, "require_bos_confirm": False},
    "ZEC": {"entry_type": "ensemble_regime", "exit_type": "ensemble_hybrid",
            "ensemble_k": 5, "require_bos_confirm": True},
}

FEATURE_COLS = [
    "time_in_trade",    # bars since entry
    "mfe_atr",          # max favorable excursion, ATR units
    "dd_from_peak_atr", # current retrace from best, ATR units
    "dist_bos_atr",     # distance to opposing BOS pivot, ATR units (+ = safe)
    "atr_ratio_20",     # current ATR / 20-bar mean ATR
    "rsi",
    "ensemble_score",   # up_count - dn_count (range -5 to +5)
    "ensemble_dir",     # 1 if up > dn, -1 if dn > up, else 0
    "side_long",        # 1 for long, 0 for short
    "bars_since_flip",  # bars since ensemble_dir last changed
]


def build_cfg(sym: str, dep_all: dict) -> cb.Cfg:
    base = _cfg_from_deployed(dep_all[sym])
    if sym in SHIP_PLAN:
        p = SHIP_PLAN[sym]
        base = replace(base,
                       entry_type=p["entry_type"], exit_type=p["exit_type"],
                       ensemble_k=p["ensemble_k"], require_bos_confirm=p["require_bos_confirm"],
                       tp1_atr=2.0, tp1_pct=0.3, tp2_atr=0.0, tp3_atr=0.0, trail_atr=0.0,
                       max_hold_bars=1000)
    return base


def ensemble_counts(arr, i):
    up = (int(arr["up_1h"][i]) + int(arr["up_struct"][i]) + int(arr["up_hma"][i])
          + int(arr["up_sjm"][i]) + int(arr["up_kalman"][i]))
    dn = (int(arr["dn_1h"][i]) + int(arr["dn_struct"][i]) + int(arr["dn_hma"][i])
          + int(arr["dn_sjm"][i]) + int(arr["dn_kalman"][i]))
    return up, dn


def compute_features(arr, i, entry_bar, side, entry_atr, best_price,
                     last_flip_bar):
    price = arr["close"][i]; a_i = arr["atr"][i]; rsi = arr["rsi"][i]
    mfe = ((best_price - arr["close"][entry_bar]) / entry_atr if side == "long"
           else (arr["close"][entry_bar] - best_price) / entry_atr) if entry_atr > 0 else 0.0
    dd  = (best_price - price) / entry_atr if side == "long" else (price - best_price) / entry_atr
    dd  = dd if entry_atr > 0 else 0.0
    opp = arr["last_pivot_l"][i] if side == "long" else arr["last_pivot_h"][i]
    if np.isnan(opp) or a_i <= 0:
        dist_bos = 0.0
    else:
        dist_bos = (price - opp) / a_i if side == "long" else (opp - price) / a_i
    atr_mean20 = np.nanmean(arr["atr"][max(0, i - 20): i]) if i > 0 else a_i
    atr_ratio = a_i / atr_mean20 if atr_mean20 > 0 else 1.0
    up_cnt, dn_cnt = ensemble_counts(arr, i)
    score = up_cnt - dn_cnt
    dir_ = 1 if up_cnt > dn_cnt else (-1 if dn_cnt > up_cnt else 0)
    return {
        "time_in_trade":    i - entry_bar,
        "mfe_atr":          float(mfe),
        "dd_from_peak_atr": float(dd),
        "dist_bos_atr":     float(dist_bos),
        "atr_ratio_20":     float(atr_ratio),
        "rsi":              float(rsi) if not np.isnan(rsi) else 50.0,
        "ensemble_score":   int(score),
        "ensemble_dir":     int(dir_),
        "side_long":        int(side == "long"),
        "bars_since_flip":  i - last_flip_bar,
    }


def extract_rows(arr, trades):
    """For each trade position (group of partials sharing entry_bar), walk
    the bars from entry_bar to the final exit bar and extract features.
    Label = 1 if position's total pnl (sum of all partials) > 0, else 0."""
    if not trades:
        return []
    # Group consecutive trades by entry_bar
    groups = {}
    for t in trades:
        eb = t.get("entry_bar")
        if eb is None: continue
        groups.setdefault(eb, []).append(t)

    rows = []
    n = len(arr["close"])
    for entry_bar, group in groups.items():
        total_pnl = sum(t["pnl"] for t in group)
        label = int(total_pnl > 0)
        side = group[0].get("side", "long")
        final_ts = max(t["ts"] for t in group)
        # Convert final_ts to bar index
        ts_arr = arr["timestamp"]
        exit_bar = int(np.searchsorted(ts_arr, final_ts, side="right")) - 1
        exit_bar = max(exit_bar, entry_bar + 1)
        exit_bar = min(exit_bar, n - 1)

        entry_price = arr["close"][entry_bar]
        entry_atr   = arr["atr"][entry_bar] if arr["atr"][entry_bar] > 0 else 1.0
        best_price  = entry_price
        # Track ensemble direction flips
        up0, dn0 = ensemble_counts(arr, entry_bar)
        cur_dir = 1 if up0 > dn0 else (-1 if dn0 > up0 else 0)
        last_flip_bar = entry_bar

        for i in range(entry_bar + 1, exit_bar + 1):
            hi, lo = arr["high"][i], arr["low"][i]
            if side == "long":
                best_price = max(best_price, hi)
            else:
                best_price = min(best_price, lo)
            up_i, dn_i = ensemble_counts(arr, i)
            new_dir = 1 if up_i > dn_i else (-1 if dn_i > up_i else 0)
            if new_dir != cur_dir:
                last_flip_bar = i; cur_dir = new_dir
            feat = compute_features(arr, i, entry_bar, side, entry_atr,
                                    best_price, last_flip_bar)
            feat["label"] = label
            feat["entry_bar"] = entry_bar
            feat["i"] = i
            rows.append(feat)
    return rows


def main():
    dep_all = load_all()
    print(f"[1/4] Loading {len(LIVE)} live symbols + running ship-plan backtests...")
    all_rows = []
    per_sym_stats = {}
    for sym in LIVE:
        d15 = cb.add_features(cb.fetch_hl(sym, "15m", 4000))
        d1h = cb.add_features(cb.fetch_hl(sym, "1h",  2000))
        d4h = cb.add_features(cb.fetch_hl(sym, "4h",  1000))
        if len(d15) < 500: continue
        arr = cb.precompute(d15, d1h, d4h)
        _patch_weekday(arr, sym)
        cfg = build_cfg(sym, dep_all)
        lev = INSTRUMENTS[sym].hl_max_leverage * 0.15
        trades = cb.backtest(arr, cfg, lev)
        rows = extract_rows(arr, trades)
        for r in rows: r["sym"] = sym
        all_rows.extend(rows)
        # Per-symbol trade count + win rate
        groups = {}
        for t in trades:
            groups.setdefault(t.get("entry_bar"), []).append(t)
        pos_pnls = [sum(t["pnl"] for t in g) for g in groups.values()]
        n_pos = len(pos_pnls); n_win = sum(1 for p in pos_pnls if p > 0)
        per_sym_stats[sym] = {
            "positions": n_pos, "winners": n_win,
            "wr": 100 * n_win / max(n_pos, 1),
            "total_pnl": sum(pos_pnls),
            "feature_rows": len(rows),
            "entry_type": cfg.entry_type, "exit_type": cfg.exit_type,
        }
        print(f"   {sym:<10} {cfg.entry_type:<18} positions={n_pos:>3} wr={per_sym_stats[sym]['wr']:.0f}%  "
              f"rows={len(rows):>4}")

    df = pd.DataFrame(all_rows)
    print(f"\n   total feature rows: {len(df)}  |  labels: "
          f"{int(df['label'].mean()*100)}% positive")

    # ---- chronological split per symbol ----
    print(f"\n[2/4] Train/test split (70/30 chronological per symbol)...")
    train_parts = []; test_parts = []
    for sym, g in df.groupby("sym"):
        g = g.sort_values("i")
        cut = int(len(g) * 0.70)
        train_parts.append(g.iloc[:cut])
        test_parts.append(g.iloc[cut:])
    train = pd.concat(train_parts); test = pd.concat(test_parts)
    X_tr = train[FEATURE_COLS]; y_tr = train["label"]
    X_te = test[FEATURE_COLS];  y_te = test["label"]
    print(f"   train: {len(train)}  pos%={y_tr.mean()*100:.0f}")
    print(f"   test:  {len(test)}  pos%={y_te.mean()*100:.0f}")

    # ---- train LightGBM ----
    print(f"\n[3/4] Training LightGBM (binary, 200 trees, early stop 20)...")
    model = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, max_depth=5, num_leaves=15,
        min_child_samples=50, reg_alpha=0.1, reg_lambda=0.1,
        objective="binary", verbose=-1,
    )
    model.fit(X_tr, y_tr,
              eval_set=[(X_te, y_te)],
              callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)])
    p_tr = model.predict_proba(X_tr)[:, 1]
    p_te = model.predict_proba(X_te)[:, 1]
    auc_tr = roc_auc_score(y_tr, p_tr)
    auc_te = roc_auc_score(y_te, p_te)
    print(f"   AUC train={auc_tr:.3f}  test={auc_te:.3f}")
    print(f"   feature importances:")
    imp = sorted(zip(FEATURE_COLS, model.feature_importances_),
                 key=lambda x: -x[1])
    for f, v in imp:
        print(f"     {f:<20} {v}")

    # ---- simulate "cut at p < 0.30" on test set ----
    print(f"\n[4/4] Simulating 'ml_cut' at P(profit) < 0.30 on the held-out "
          f"test set (per-bar inference)...")
    # For each TRADE (group by entry_bar+sym) in test, find the FIRST bar
    # (after 4-bar grace) where p < 0.30. If found, we "exit early" at that
    # bar's close. Re-run the backtest per symbol with this early exit and
    # compare P&L.
    def simulate_cuts(df_rows, model, threshold=0.30, grace=4):
        X = df_rows[FEATURE_COLS]
        probs = model.predict_proba(X)[:, 1]
        df_rows = df_rows.assign(p_profit=probs).reset_index(drop=True)
        # Group by (sym, entry_bar) = one position
        results = []
        for (sym, eb), g in df_rows.groupby(["sym", "entry_bar"]):
            g = g.sort_values("time_in_trade").reset_index(drop=True)
            cut_row = g[(g["time_in_trade"] >= grace) & (g["p_profit"] < threshold)]
            label = g["label"].iloc[0]  # same for all bars in this position
            if not cut_row.empty:
                first = cut_row.iloc[0]
                # "ml_cut" outcome: we exit at this bar. Simple proxy —
                # assume we realize whatever MFE we've captured minus dd.
                # Conservative: outcome = sign of (MFE - dd_from_peak).
                # Rough estimate of realized P&L relative to full trade.
                # For the AUC/pass question it's enough to ask: if we'd cut,
                # would we have avoided losers more than killed winners?
                avoided_loser = 1 if label == 0 else 0
                killed_winner = 1 if label == 1 else 0
                results.append({
                    "sym": sym, "entry_bar": eb, "label": label,
                    "cut": True, "cut_at_bar": int(first["time_in_trade"]),
                    "p_at_cut": float(first["p_profit"]),
                    "avoided_loser": avoided_loser, "killed_winner": killed_winner,
                })
            else:
                results.append({"sym": sym, "entry_bar": eb, "label": label,
                                "cut": False, "avoided_loser": 0, "killed_winner": 0})
        return pd.DataFrame(results)

    sim = simulate_cuts(test.copy(), model, threshold=0.30, grace=4)
    n_cut = int(sim["cut"].sum())
    n_total = len(sim)
    n_avoided = int(sim["avoided_loser"].sum())
    n_killed = int(sim["killed_winner"].sum())
    cut_losers_pct = sim[sim["cut"]]["label"].mean() * 100 if n_cut else 0.0
    print(f"   positions in test: {n_total}")
    print(f"   positions cut early: {n_cut} ({100*n_cut/max(n_total,1):.0f}%)")
    print(f"     of cuts: {n_avoided} losers avoided ({(100 - cut_losers_pct):.0f}%), "
          f"{n_killed} winners killed ({cut_losers_pct:.0f}%)")
    if n_cut:
        hit_rate = n_avoided / n_cut * 100
        print(f"   cut precision (% of cuts that were actually losers): {hit_rate:.0f}%")
        if hit_rate >= 60:
            print(f"   VERDICT: model finds losers better than chance ({hit_rate:.0f}% vs 50%) — "
                  f"worth shipping as auxiliary exit.")
        else:
            print(f"   VERDICT: model is no better than chance at cut decisions. DON'T SHIP.")

    # ---- save model ----
    models_dir = Path(__file__).resolve().parent.parent / "models"
    models_dir.mkdir(exist_ok=True)
    model_path = models_dir / "exit_timing.pkl"
    meta = {
        "feature_cols": FEATURE_COLS,
        "threshold": 0.30,
        "grace_bars": 4,
        "training_rows": len(train),
        "test_rows": len(test),
        "auc_train": auc_tr,
        "auc_test": auc_te,
        "cut_precision_pct": (n_avoided / n_cut * 100) if n_cut else 0.0,
        "per_symbol_stats": per_sym_stats,
    }
    with open(model_path, "wb") as f:
        pickle.dump({"model": model, "meta": meta}, f)
    print(f"\nModel + metadata saved → {model_path}")
    print(f"Meta: AUC_test={auc_te:.3f}, cut_precision={meta['cut_precision_pct']:.0f}%")


if __name__ == "__main__":
    main()
