// ============= SIGNALS =============
window.SIGNALS_FEED = [
  { t: '16:24:13', sym: 'HYPE',  ev: 'ema_cross',   dir: 'UP', px: 41.704, rsi: 69.0, conf: 0.82, action: 'OPEN LONG · 0.8×',    status: 'filled' },
  { t: '16:23:47', sym: 'ZEC',   ev: 'bb_squeeze',  dir: 'UP', px: 327.64, rsi: 50.4, conf: 0.71, action: 'SCALE +25%',          status: 'filled' },
  { t: '16:22:09', sym: 'ARB',   ev: 'ema_cross',   dir: 'DN', px: 0.1281, rsi: 71.2, conf: 0.64, action: 'WATCH',               status: 'skip' },
  { t: '16:21:55', sym: 'ETH',   ev: 'rsi_overbt',  dir: 'DN', px: 2336.4, rsi: 60.5, conf: 0.58, action: 'WATCH',               status: 'skip' },
  { t: '16:20:31', sym: 'BTC',   ev: 'vol_spike',   dir: 'UP', px: 78418,  rsi: 73.4, conf: 0.77, action: 'PENDING LONG',        status: 'pending' },
  { t: '16:18:02', sym: 'ENA',   ev: 'ema_cross',   dir: 'DN', px: 0.1076, rsi: 76.0, conf: 0.69, action: 'WATCH',               status: 'skip' },
  { t: '16:17:41', sym: 'SOL',   ev: 'breakout',    dir: 'UP', px: 142.22, rsi: 62.1, conf: 0.74, action: 'OPEN LONG · 0.6×',    status: 'filled' },
  { t: '16:14:09', sym: 'AVAX',  ev: 'rsi_oversld', dir: 'UP', px: 18.44,  rsi: 29.8, conf: 0.61, action: 'WATCH',               status: 'skip' },
  { t: '16:12:33', sym: 'LINK',  ev: 'ema_cross',   dir: 'UP', px: 11.87,  rsi: 55.4, conf: 0.53, action: 'WATCH',               status: 'skip' },
  { t: '16:09:12', sym: 'HYPE',  ev: 'vol_spike',   dir: 'UP', px: 41.51,  rsi: 66.2, conf: 0.81, action: 'PRIMED',              status: 'primed' },
  { t: '16:07:28', sym: 'DOGE',  ev: 'bb_touch',    dir: 'UP', px: 0.1144, rsi: 41.2, conf: 0.48, action: 'WATCH',               status: 'skip' },
  { t: '16:04:51', sym: 'ZEC',   ev: 'ema_cross',   dir: 'UP', px: 324.1,  rsi: 48.9, conf: 0.79, action: 'OPEN LONG · 0.9×',    status: 'filled' },
  { t: '16:01:18', sym: 'SUI',   ev: 'breakout',    dir: 'UP', px: 2.91,   rsi: 58.3, conf: 0.62, action: 'WATCH',               status: 'skip' },
  { t: '15:58:44', sym: 'TIA',   ev: 'rsi_overbt',  dir: 'DN', px: 5.31,   rsi: 78.4, conf: 0.67, action: 'WATCH',               status: 'skip' },
  { t: '15:55:02', sym: 'ARB',   ev: 'bb_squeeze',  dir: 'DN', px: 0.1288, rsi: 72.1, conf: 0.55, action: 'WATCH',               status: 'skip' },
];

// ============= JOURNAL — closed trades =============
window.JOURNAL = [
  { id: 'T-3482', sym: 'SOL',  side: 'LONG',  opened: 'Apr 22 09:14', closed: 'Apr 22 14:02', hold: '4h 48m', size: 6400,  entry: 138.44, exit: 142.22, r: +1.84, pnl: +242.14, mae: -0.42, mfe: +2.11, strat: 'whale-swing',  tags: ['trend'] },
  { id: 'T-3481', sym: 'ZEC',  side: 'LONG',  opened: 'Apr 22 06:52', closed: 'Apr 22 13:47', hold: '6h 55m', size: 3200,  entry: 315.80, exit: 324.02, r: +2.14, pnl: +263.04, mae: -0.31, mfe: +2.38, strat: 'whale-swing',  tags: ['scale'] },
  { id: 'T-3480', sym: 'ARB',  side: 'SHORT', opened: 'Apr 22 04:01', closed: 'Apr 22 06:18', hold: '2h 17m', size: 4800,  entry: 0.1322, exit: 0.1288, r: +1.22, pnl: +163.20, mae: -0.18, mfe: +1.48, strat: 'mean-rev',      tags: ['quick'] },
  { id: 'T-3479', sym: 'BTC',  side: 'LONG',  opened: 'Apr 21 22:40', closed: 'Apr 22 03:12', hold: '4h 32m', size: 12000, entry: 77440,  exit: 77080,  r: -0.58, pnl: -108.40, mae: -1.02, mfe: +0.44, strat: 'whale-swing',  tags: ['stopped'] },
  { id: 'T-3478', sym: 'ETH',  side: 'SHORT', opened: 'Apr 21 18:22', closed: 'Apr 21 20:04', hold: '1h 42m', size: 5600,  entry: 2348.1, exit: 2336.4, r: +1.02, pnl: +140.11, mae: -0.24, mfe: +1.14, strat: 'mean-rev',      tags: [] },
  { id: 'T-3477', sym: 'HYPE', side: 'LONG',  opened: 'Apr 21 14:12', closed: 'Apr 21 17:58', hold: '3h 46m', size: 4200,  entry: 40.22,  exit: 41.51,  r: +1.48, pnl: +184.44, mae: -0.35, mfe: +1.66, strat: 'whale-swing',  tags: ['trend'] },
  { id: 'T-3476', sym: 'TIA',  side: 'SHORT', opened: 'Apr 21 11:01', closed: 'Apr 21 13:48', hold: '2h 47m', size: 3800,  entry: 5.48,   exit: 5.31,   r: +1.11, pnl: +152.00, mae: -0.22, mfe: +1.32, strat: 'mean-rev',      tags: [] },
  { id: 'T-3475', sym: 'ENA',  side: 'SHORT', opened: 'Apr 21 08:34', closed: 'Apr 21 10:12', hold: '1h 38m', size: 2800,  entry: 0.1122, exit: 0.1076, r: +1.34, pnl: +128.80, mae: -0.14, mfe: +1.48, strat: 'mean-rev',      tags: ['quick'] },
  { id: 'T-3474', sym: 'SUI',  side: 'LONG',  opened: 'Apr 20 19:44', closed: 'Apr 21 02:09', hold: '6h 25m', size: 3100,  entry: 2.72,   exit: 2.64,   r: -1.00, pnl: -248.00, mae: -1.14, mfe: +0.38, strat: 'breakout',      tags: ['stopped'] },
  { id: 'T-3473', sym: 'AVAX', side: 'LONG',  opened: 'Apr 20 15:11', closed: 'Apr 20 18:24', hold: '3h 13m', size: 5200,  entry: 18.04,  exit: 18.88,  r: +1.64, pnl: +218.40, mae: -0.28, mfe: +1.82, strat: 'breakout',      tags: ['trend'] },
  { id: 'T-3472', sym: 'DOGE', side: 'SHORT', opened: 'Apr 20 11:58', closed: 'Apr 20 14:33', hold: '2h 35m', size: 2100,  entry: 0.1176, exit: 0.1144, r: +1.12, pnl: +67.20,  mae: -0.19, mfe: +1.28, strat: 'mean-rev',      tags: ['quick'] },
  { id: 'T-3471', sym: 'LINK', side: 'LONG',  opened: 'Apr 20 08:04', closed: 'Apr 20 10:47', hold: '2h 43m', size: 3600,  entry: 11.54,  exit: 11.81,  r: +0.88, pnl: +97.20,  mae: -0.41, mfe: +1.08, strat: 'whale-swing',  tags: [] },
  { id: 'T-3470', sym: 'BTC',  side: 'SHORT', opened: 'Apr 19 21:14', closed: 'Apr 20 02:22', hold: '5h 08m', size: 9200,  entry: 78240,  exit: 77980,  r: +0.42, pnl: +102.08, mae: -0.61, mfe: +0.68, strat: 'mean-rev',      tags: [] },
  { id: 'T-3469', sym: 'SOL',  side: 'SHORT', opened: 'Apr 19 17:02', closed: 'Apr 19 19:41', hold: '2h 39m', size: 4100,  entry: 141.02, exit: 142.84, r: -0.94, pnl: -131.62, mae: -1.11, mfe: +0.21, strat: 'breakout',      tags: ['stopped'] },
  { id: 'T-3468', sym: 'ZEC',  side: 'LONG',  opened: 'Apr 19 11:12', closed: 'Apr 19 16:04', hold: '4h 52m', size: 2800,  entry: 308.2,  exit: 318.4,  r: +2.21, pnl: +285.60, mae: -0.22, mfe: +2.44, strat: 'whale-swing',  tags: ['scale','trend'] },
];

// ============= RISK =============
window.RISK_DATA = {
  equity: 12840.22,
  margin: { used: 1667.22, total: 12840.22, pct: 0.13, maint: 0.032 },
  exposure: [
    { sym: 'HYPE', side: 'LONG',  notional: 498.20, pct: 0.039 },
    { sym: 'ZEC',  side: 'LONG',  notional: 984.11, pct: 0.077 },
    { sym: 'BTC',  side: 'FLAT',  notional: 0,       pct: 0 },
    { sym: 'ETH',  side: 'FLAT',  notional: 0,       pct: 0 },
    { sym: 'ARB',  side: 'FLAT',  notional: 0,       pct: 0 },
    { sym: 'ENA',  side: 'FLAT',  notional: 0,       pct: 0 },
    { sym: 'SOL',  side: 'LONG',  notional: 184.91, pct: 0.014 },
  ],
  // 7x7 correlation matrix (30d)
  corrSyms: ['BTC', 'ETH', 'SOL', 'ZEC', 'HYPE', 'ARB', 'ENA'],
  corr: [
    [1.00, 0.89, 0.82, 0.41, 0.52, 0.74, 0.68],
    [0.89, 1.00, 0.88, 0.48, 0.58, 0.80, 0.74],
    [0.82, 0.88, 1.00, 0.44, 0.62, 0.78, 0.71],
    [0.41, 0.48, 0.44, 1.00, 0.28, 0.32, 0.22],
    [0.52, 0.58, 0.62, 0.28, 1.00, 0.44, 0.38],
    [0.74, 0.80, 0.78, 0.32, 0.44, 1.00, 0.81],
    [0.68, 0.74, 0.71, 0.22, 0.38, 0.81, 1.00],
  ],
  scenarios: [
    { name: 'BTC −5%',  pnl: -324.20, liq: 0 },
    { name: 'BTC −10%', pnl: -684.10, liq: 0 },
    { name: 'Alts −15%', pnl: -812.44, liq: 0 },
    { name: 'Vol +50%',  pnl: -142.00, liq: 0 },
    { name: 'Flash −20%', pnl: -1844.11, liq: 1 },
  ],
  drawdown: { cur: -0.012, max: -0.061, sinceHigh: '14h 22m' },
  var95: -188.22,
  var99: -342.10,
};

// ============= STRATEGIES =============
window.STRATS = [
  {
    id: 'whale-swing', name: 'whale-swing v3.2', status: 'LIVE',
    pnl30d: +2844.11, pnl30dPct: +28.44, winRate: 0.64, trades: 124, sharpe: 2.14, maxDD: -6.1, avgR: 1.42, avgHold: '4h 18m',
    color: '#4ade80',
  },
  {
    id: 'mean-rev', name: 'mean-rev v1.4', status: 'LIVE',
    pnl30d: +942.80, pnl30dPct: +9.43, winRate: 0.72, trades: 88, sharpe: 1.88, maxDD: -3.4, avgR: 0.84, avgHold: '2h 11m',
    color: '#60a5fa',
  },
  {
    id: 'breakout', name: 'breakout v0.9', status: 'PAPER',
    pnl30d: -188.40, pnl30dPct: -1.88, winRate: 0.41, trades: 47, sharpe: 0.42, maxDD: -8.2, avgR: 1.04, avgHold: '5h 32m',
    color: '#fbbf24',
  },
  {
    id: 'scalper-lite', name: 'scalper-lite v2.0', status: 'OFF',
    pnl30d: +412.22, pnl30dPct: +4.12, winRate: 0.58, trades: 221, sharpe: 1.22, maxDD: -2.1, avgR: 0.38, avgHold: '38m',
    color: '#a78bfa',
  },
];

// equity curves — 60 points each, deterministic
window.STRAT_CURVES = (() => {
  const out = {};
  window.STRATS.forEach((s, si) => {
    let seed = s.id.split('').reduce((a,c)=>a+c.charCodeAt(0),0);
    const rand = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
    const N = 60;
    const target = s.pnl30dPct / 100;
    const arr = [0];
    for (let i = 1; i < N; i++) {
      const drift = target / N;
      const vol = 0.008 + si * 0.002;
      arr.push(arr[i-1] + drift + (rand() - 0.5) * vol);
    }
    // rescale last to target
    const scale = target / arr[N-1];
    out[s.id] = arr.map(v => v * scale);
  });
  return out;
})();
