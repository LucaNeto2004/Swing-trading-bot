# xyz:SILVER

**Status:** active (paper)
**Last verified:** 2026-04-24
**Sources:** `config/deployed/whale_xyz_SILVER.json`, `config/settings.py::TRADING_HOURS` + `is_tradeable_now`, backtest 2026-04-19

## What it is

HyperLiquid xyz-deployer HIP-3 commodity perp on physical silver. The only non-native-crypto symbol currently in the swing-bot universe.

## Trading hours — hard rule

Enter **only on weekdays, 08:00–22:00 UTC** (London open → NY close). No weekend. No overnight Asia.

- Enforcement: `config/settings.py::is_tradeable_now(symbol, now_utc)`, called in `main.py` before strategy evaluation.
- Blocks **new entries only**. Existing positions continue to manage SL/TP/max_hold through the window close.
- Extend `TRADING_HOURS` dict if any future xyz-deployer pair is added (xyz:GOLD, xyz:BRENTOIL, xyz:CL).

**Why:** xyz-deployer quotes 24/7 but real liquidity is London+NY only. Off-hours, spreads widen and SL slippage exceeds the ATR noise the strategy is sized for. Confirmed by three consecutive SL hits on Saturday 2026-04-18 (MFE < 1.3 ATR — entry signals were wrong *and* execution was worse than expected).

**Backtest caveat:** Grid runs must mirror this session filter or SILVER performance will be overstated.

## Deployed config (2026-04-19)

From `config/deployed/whale_xyz_SILVER.json`:

| Field | Value |
|---|---|
| entry_type | `bb_touch` |
| trend_filter | `ema_slope` |
| trend_filter_1h | `both_agree` (ema_cross AND ICT structure must agree) |
| rsi_oversold / overbought | 35 / 65 |
| sl_atr | 1.5 |
| TP ladder | TP1 2.0×ATR (30%), TP2 3.0×ATR (30%), TP3 4.0×ATR (20%) |
| trail_atr | 0.0 (disabled) |
| max_hold_bars | 288 (24h on 5m bars) |
| direction | both |
| use_1h_filter | true |
| require_4h_agreement | false |

Multi-tier scale-out 30/30/20 added 2026-04-19. 4h filter switched off per focused backtest same day.

## Backtest numbers (run date 2026-04-19)

- Trades: 28
- Win rate: 46.4%
- Profit factor: 3.74
- P&L: +$3,407.62
- Avg bars in trade: 56 (~4.7h)
- Max win / loss: +$1,501.89 / −$342.63

## Commission caveat

Since SILVER/ETH were demoted from the HL institutional blend / HIP-3 discount on 2026-04-20, commission is **0.00030** (HL tier 0 crypto perp, 50/50 maker/taker). Earlier backtests using 0.00006 overstated edge — re-run any SILVER config through `research/commod_oos.py` with the new commission before redeploying.

## Related

- [risk-gate](../concepts/risk-gate.md) — commission source of truth
- [regime-filters](../concepts/regime-filters.md) — why SILVER stayed on `both_agree` during the 2026-04-21 swap
