// Real-data adapter — fetches /api/state and maps it to the shapes the
// design components expect. Replaces the original mock data module.

const EXIT_TAG_MAP = {
  tp1_partial: 'tp', tp2_partial: 'tp', tp3_partial: 'tp',
  stop_loss: 'sl',
  ensemble_exit: 'close', choch_exit: 'close', pullback_exit_pivot_h: 'close',
  pullback_exit_pivot_l: 'close', pullback_exit_regime_flip_red: 'close',
  pullback_exit_regime_flip_green: 'close', bos_exit: 'close',
  regime_exit: 'close', runner_stop: 'close', trail_stop: 'close',
  structural_stop: 'sl', manual_close_retired: 'close',
  test_exit: 'close',
};

function tagForExitReason(reason) {
  if (!reason) return 'close';
  return EXIT_TAG_MAP[reason] || 'close';
}

function hhmmss(ts) {
  if (!ts) return '--:--:--';
  const s = String(ts);
  const m = s.match(/(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : s.slice(-8);
}

function mapTickers(ticker) {
  if (!Array.isArray(ticker)) return [];
  const rows = ticker.map(t => ({
    sym: t.symbol,
    px: Number(t.price) || 0,
    chg: Number(t.change_pct) || 0,
  }));
  // Mirror to window.DATA.TICKERS so legacy components (OrderDrawer) see
  // live symbols for their select dropdown.
  if (window.DATA) window.DATA.TICKERS = rows;
  return rows;
}

function mapPositions(positions, balance) {
  if (!Array.isArray(positions)) return [];
  return positions.map(p => {
    const unrl = Number(p.unrealized) || 0;
    const notional = Number(p.notional) || 0;
    const pnlPct = notional > 0 ? (unrl / notional) * 100 : 0;
    const util = balance > 0 ? Math.min(1, notional / (balance * 10)) : 0;
    const setLev = Number(p.set_leverage) || 1;
    const margin = setLev > 0 ? notional / setLev : 0;
    return {
      token: p.symbol,
      side: String(p.side || '').toUpperCase(),
      leverage: Number(p.effective_leverage) || 1,
      setLeverage: setLev,
      margin,
      mode: 'iso',
      size: Number(p.size) || 0,
      notional,
      avgEntry: Number(p.entry) || 0,
      live: Number(p.live) || Number(p.entry) || 0,
      pnl: unrl,
      pnlPct,
      util,
      slHit: Number(p.bars_held) || 0,
      slMax: Number(p.max_hold_bars) || 1000,
      status: 'ACTIVE',
      sl: Number(p.sl) || null,
      tp1: Number(p.tp1) || null,
      tp2: Number(p.tp2) || null,
      tp3: Number(p.tp3) || null,
      tp1_hit: !!p.tp1_hit,
      tp2_hit: !!p.tp2_hit,
      tp3_hit: !!p.tp3_hit,
      trail_active: !!p.trail_active,
      trail_offset: Number(p.trail_offset) || 0,
      best_price: Number(p.best_price) || null,
      liqDistPct: Number(p.liq_distance_pct) || null,
      liqPrice: Number(p.liq_price) || null,
    };
  });
}

function mapFills(trade_history, commissionPct) {
  if (!Array.isArray(trade_history)) return [];
  const rows = [];
  const tail = trade_history.slice(-40).reverse();
  for (const t of tail) {
    const posSide = String(t.side || '').toLowerCase();
    const reason = String(t.exit_reason || '');
    const tag = tagForExitReason(reason);
    const action = posSide === 'long' ? 'SELL' : 'BUY';
    const notional = Number(t.notional) || 0;
    const fee = commissionPct ? notional * commissionPct : 0;
    rows.push({
      t: hhmmss(t.timestamp),
      sym: t.symbol,
      side: action,
      qty: Number(t.size) || 0,
      px: Number(t.price) || 0,
      fee,
      pnl: t.pnl == null ? null : Number(t.pnl),
      tag,
    });
  }
  return rows;
}

function mapSymbolsAttrib(attribution_symbol) {
  if (!Array.isArray(attribution_symbol)) return [];
  return attribution_symbol.map(s => ({
    sym: s.key,
    pnl: Number(s.pnl) || 0,
    trades: Number(s.trades) || 0,
    wr: Number(s.win_rate) || 0,
    vol: 0,
  }));
}

function mapEquityCurve(equity_curve) {
  if (!Array.isArray(equity_curve) || equity_curve.length === 0) return [10000, 10000];
  return equity_curve.map(c => Number(c.balance) || 0);
}

// Window-cutoff helper — calendar-day reset at local midnight, matching the
// old dashboard's windowCutoff(). `days=1` means "since midnight today";
// `days=7` means "since midnight 6 days ago". `days=0` → all time.
function windowCutoffMs(days) {
  if (!days) return 0;
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - (days - 1));
  return d.getTime();
}

function parseTsMs(ts) {
  if (!ts) return NaN;
  const s = String(ts).replace(' ', 'T');
  const t = Date.parse(s.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z');
  return t;
}

// Session math — matches dashboard.html L1502-1543. Window-filter trade_history
// and walk equity_curve to find baseline balance at cutoff.
function mapStats(state, rangeDays) {
  const s = state.stats || {};
  const startBal = Number(state.starting_balance) || 10000;
  const balance = Number(state.balance) || startBal;
  const positions = state.positions || [];
  const unrealizedTotal = positions.reduce((a, p) => a + (Number(p.unrealized) || 0), 0);
  const liveEquity = balance + unrealizedTotal;

  const peak = Math.max(Number(state.account_peak_balance) || 0, liveEquity);
  const ddPct = peak > 0 ? Math.max(0, ((peak - liveEquity) / peak) * 100) : 0;

  const th = state.trade_history || [];
  const curve = state.equity_curve || [];

  let sessionBalAtCutoff = startBal;
  let sessionTrades = th;
  if (rangeDays > 0) {
    const sCut = windowCutoffMs(rangeDays);
    for (const p of curve) {
      if (!p.ts) { sessionBalAtCutoff = Number(p.balance) || sessionBalAtCutoff; continue; }
      const t = parseTsMs(p.ts);
      if (Number.isNaN(t)) continue;
      if (t <= sCut) sessionBalAtCutoff = Number(p.balance) || sessionBalAtCutoff;
      else break;
    }
    sessionTrades = th.filter(tr => {
      const t = parseTsMs(tr.timestamp);
      return !Number.isNaN(t) && t >= sCut;
    });
  }

  const sessionPnl = liveEquity - sessionBalAtCutoff;
  const sessionPnlPct = startBal > 0 ? (sessionPnl / startBal) * 100 : 0;

  const sessionWins = sessionTrades.filter(t => Number(t.pnl || 0) > 0).length;
  const sessionLosses = sessionTrades.filter(t => Number(t.pnl || 0) < 0).length;
  const sessionTotal = sessionWins + sessionLosses;
  const winRate = sessionTotal ? (sessionWins / sessionTotal) * 100 : 0;

  const sessionGW = sessionTrades.filter(t => Number(t.pnl || 0) > 0).reduce((a, t) => a + Number(t.pnl), 0);
  const sessionGL = -sessionTrades.filter(t => Number(t.pnl || 0) < 0).reduce((a, t) => a + Number(t.pnl), 0);
  const profitFactor = sessionGL > 0 ? sessionGW / sessionGL : (sessionGW > 0 ? Infinity : 0);

  const sessionVol = sessionTrades.reduce((a, t) => a + Math.abs(Number(t.notional) || 0), 0)
    + positions.reduce((a, p) => a + Math.abs(Number(p.notional) || 0), 0);

  const spark = curve.slice(-12).map(c => Number(c.balance) || 0);
  while (spark.length < 12) spark.unshift(spark[0] || startBal);

  let avgHold = '—';
  if (sessionTrades.length > 0) {
    const avgBars = sessionTrades.reduce((a, t) => a + (Number(t.held_bars) || 0), 0) / sessionTrades.length;
    const mins = Math.round(avgBars * 5);
    const hh = String(Math.floor(mins / 60)).padStart(2, '0');
    const mm = String(mins % 60).padStart(2, '0');
    avgHold = `${hh}:${mm}`;
  }

  return {
    sessionPnl, sessionPnlPct,
    volume: sessionVol,
    winRate, wins: sessionWins, losses: sessionLosses,
    profitFactor: Number.isFinite(profitFactor) ? profitFactor : 0,
    pfInfinite: profitFactor === Infinity,
    drawdown: ddPct,
    sharpe: 0,
    avgHold,
    spark,
  };
}

// Bias label buckets — 0-100 score maps to a symmetric 5-bucket label.
// Kept in sync with the arrow color / direction in SentLevCard so users
// can't see a "Very Bullish" label with a small score or vice versa.
function biasLabelFor(biasPct) {
  if (biasPct >= 0.80) return 'Very Bullish';
  if (biasPct >= 0.60) return 'Bullish';
  if (biasPct >= 0.40) return 'Neutral';
  if (biasPct >= 0.20) return 'Bearish';
  return 'Very Bearish';
}

function mapSnapshot(state, rangeDays) {
  const rawBalance = Number(state.balance) || 0;
  const commission = Number(state.commission_pct) || 0;
  const positions = state.positions || [];
  const unrealizedTotal = positions.reduce((a, p) => a + (Number(p.unrealized) || 0), 0);
  const liveEquity = rawBalance + unrealizedTotal;

  const longNotional = positions.filter(p => String(p.side).toLowerCase() === 'long')
    .reduce((a, p) => a + Math.abs(Number(p.notional) || 0), 0);
  const shortNotional = positions.filter(p => String(p.side).toLowerCase() === 'short')
    .reduce((a, p) => a + Math.abs(Number(p.notional) || 0), 0);
  const totalNotional = longNotional + shortNotional;
  const biasScore = totalNotional > 0 ? longNotional / totalNotional : 0.5;
  const biasLabel = totalNotional > 0 ? biasLabelFor(biasScore) : 'Flat';

  const vaultValue = Number(state.vault_value || 0);
  const perpsValue = Math.max(liveEquity - vaultValue, 0);
  const portfolioLev = perpsValue > 0 ? (totalNotional / perpsValue) : 0;

  return {
    raw: state,
    balance: liveEquity,
    realizedBalance: rawBalance,
    unrealizedTotal,
    longNotional, shortNotional, totalNotional,
    biasScore, biasLabel, portfolioLev,
    startingBalance: Number(state.starting_balance) || rawBalance,
    mode: state.mode || 'PAPER',
    network: state.network || 'TESTNET',
    effectiveLev: Number(state.effective_leverage) || 0,
    marginPct: Number(state.margin_pct) || 0,
    setLev: Number(state.set_leverage) || 0,
    maxConcurrent: Number(state.max_concurrent) || 4,
    lastRefresh: state.last_refresh || null,
    killSwitch: !!state.kill_switch,
    ddHalt: !!state.account_dd_halt,
    consecLossHalt: !!state.consecutive_loss_halt,
    tickers: mapTickers(state.ticker),
    positions: mapPositions(state.positions, liveEquity),
    fills: mapFills(state.trade_history, commission),
    symbolsAttrib: mapSymbolsAttrib(state.attribution_symbol),
    equityPts: mapEquityCurve(state.equity_curve),
    stats: mapStats(state, rangeDays),
    openCount: Number(state.open_count) || 0,
  };
}

async function fetchAccountState() {
  const r = await fetch('/api/state', { cache: 'no-store' });
  if (!r.ok) throw new Error(`/api/state ${r.status}`);
  return await r.json();
}

const EMPTY_SNAPSHOT = {
  raw: {},
  balance: 0, startingBalance: 10000,
  mode: 'PAPER', network: 'TESTNET',
  effectiveLev: 0, marginPct: 0, setLev: 0, maxConcurrent: 4,
  biasScore: 0.5, biasLabel: 'Flat', portfolioLev: 0,
  longNotional: 0, shortNotional: 0, totalNotional: 0,
  unrealizedTotal: 0, realizedBalance: 0,
  lastRefresh: null, killSwitch: false, ddHalt: false, consecLossHalt: false,
  tickers: [], positions: [], fills: [], symbolsAttrib: [],
  equityPts: [10000, 10000],
  stats: { sessionPnl: 0, sessionPnlPct: 0, volume: 0, winRate: 0, wins: 0, losses: 0,
    profitFactor: 0, pfInfinite: false, drawdown: 0, sharpe: 0, avgHold: '—',
    spark: Array(12).fill(10000) },
  openCount: 0,
};

window.DATA = {
  fetchAccountState,
  mapSnapshot,
  EMPTY_SNAPSHOT,
  // Kept for components that still reference the mock shape directly.
  TICKERS: [], POSITIONS: [], FILLS: [], SYMBOLS_ATTRIB: [],
  genEquity: () => [10000, 10000],
};
