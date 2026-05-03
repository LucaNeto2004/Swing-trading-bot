"""Stage 2 of discovery — run the OOS pipeline against candidate symbols.

Reads /tmp/discovery_candidates.json (output of discover_candidates.py),
monkey-patches commod_backtest.SYMBOLS / LEV_CAP / commod_oos.SYMBOLS to point
at the candidates, then runs the canonical OOS pipeline (IS grid → OOS verify
→ quartiles → random benchmark → ±20% sensitivity → PASS/FAIL verdict).

Output: /tmp/discover_grid.json + console summary table.

Usage:
  python research/discover_grid.py                          # all candidates
  python research/discover_grid.py --syms AAVE DOGE        # specific subset
  python research/discover_grid.py --candidates-file /path  # alt input file
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

import research.commod_backtest as cb
import research.commod_oos as oos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-file", default="/tmp/discovery_candidates.json")
    ap.add_argument("--syms", nargs="+", default=None,
                    help="optional subset of candidate symbols to run")
    args = ap.parse_args()

    if not Path(args.candidates_file).exists():
        print(f"ERROR: {args.candidates_file} not found.")
        print(f"Run research/discover_candidates.py first.")
        sys.exit(1)

    data = json.load(open(args.candidates_file))
    candidates = data.get("candidates", [])
    if args.syms:
        wanted = set(args.syms)
        candidates = [c for c in candidates if c["symbol"] in wanted]
        missing = wanted - {c["symbol"] for c in candidates}
        if missing:
            print(f"WARN: not in candidate file: {missing}")

    if not candidates:
        print("No candidates to evaluate.")
        sys.exit(0)

    syms = [c["symbol"] for c in candidates]
    lev_cap = {c["symbol"]: c["max_lev"] for c in candidates}

    print(f"Discovery grid for {len(syms)} candidates: {syms}")
    print(f"Leverage caps: {lev_cap}\n")

    # Monkey-patch the pipeline globals to target candidates
    cb.SYMBOLS = syms
    cb.LEV_CAP = lev_cap
    oos.SYMBOLS = syms
    oos.LEV_CAP = lev_cap

    # Crypto-only candidates trade 24/7 — neutralize the weekday gate that
    # commod_backtest applies by default (designed for HIP-3 commodity perps).
    # We do this by patching precompute to set weekday=all-True for non-xyz
    # symbols. Reuse whale_oos.py's pattern.
    _orig_precompute = cb.precompute

    def _precompute_24_7(d15, d1h, d4h, *args, **kwargs):
        arr = _orig_precompute(d15, d1h, d4h, *args, **kwargs)
        # If we were called for a crypto symbol, override weekday mask.
        # The arr doesn't carry symbol info, so override unconditionally —
        # since we only ever call this with crypto candidates here.
        arr["weekday"] = np.ones_like(arr["weekday"], dtype=bool)
        return arr

    cb.precompute = _precompute_24_7

    # Run the OOS pipeline
    oos.main()

    # Re-read commod_oos.json that the pipeline wrote, rename to discover_grid.json
    src = "/tmp/commod_oos.json"
    dst = "/tmp/discover_grid.json"
    if Path(src).exists():
        results = json.load(open(src))
        with open(dst, "w") as f:
            json.dump({
                "candidates_input": candidates,
                "results": results,
            }, f, indent=2, default=str)
        print(f"\n=== DISCOVERY VERDICT (saved to {dst}) ===\n")
        print(f"{'SYM':<10} {'VERDICT':<6} {'IS PF':>6} {'OOS PF':>7} {'OOS $':>8} {'OOS n':>6}  CONFIG")
        print("-" * 100)
        for sym, r in results.items():
            if r.get("elected_cfg") is None:
                print(f"{sym:<10} SKIP   —      —       —        —      {r.get('reason','')}")
                continue
            v = "PASS ✓" if r["verdict"]["pass"] else "FAIL ✗"
            cfg = r["elected_cfg"]
            cfg_str = f"{cfg['entry_type']}·{cfg['trend_filter_1h']}·sl{cfg['sl_atr']}·tr{cfg['trail_atr']}"
            oos_pf = r["oos"]["pf"] or 0
            print(f"{sym:<10} {v:<6} {r['is']['pf']:>6} {oos_pf:>7} "
                  f"${r['oos']['pnl']:>+6.0f}  {r['oos']['n']:>6}   {cfg_str}")
        # Surface failures
        passes = [s for s, r in results.items() if r.get("verdict", {}).get("pass")]
        fails = [(s, r) for s, r in results.items() if r.get("elected_cfg") and not r["verdict"]["pass"]]
        print(f"\n{len(passes)} passing, {len(fails)} failing")
        if passes:
            print(f"\n→ Passing candidates ready for review/deploy:")
            for s in passes:
                cfg = results[s]["elected_cfg"]
                print(f"  {s}: {json.dumps(cfg, indent=None)}")


if __name__ == "__main__":
    main()
