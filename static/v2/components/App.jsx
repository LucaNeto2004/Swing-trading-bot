// Dashboard app root — wires real /api/state data into the design components.
const { useState, useEffect, useMemo, useRef } = React;

const RANGE_DAYS = { '24h': 1, '7d': 7, '30d': 30, 'all': 0, 'All': 0 };
const POLL_MS = 1500;

function installClaudeStub(snapshotRef) {
  if (!window.claude) {
    window.claude = {
      complete: async (prompt) => {
        const isInsight = /ONE tight sentence/.test(prompt);
        const s = snapshotRef.current;
        if (isInsight && s) return buildRuleInsight(s);
        return 'llm proxy not configured — wire backend to enable ⌘K.';
      },
    };
  }
}

function buildRuleInsight(s) {
  if (!s || (s.openCount === 0 && s.fills.length === 0)) {
    return 'no positions open and no recent fills — bot idle.';
  }
  const bestPos = s.positions.reduce((b, p) => (!b || p.pnl > b.pnl ? p : b), null);
  const worstPos = s.positions.reduce((b, p) => (!b || p.pnl < b.pnl ? p : b), null);
  if (s.openCount > 0 && bestPos && worstPos && bestPos !== worstPos) {
    return `${s.openCount} open · ${bestPos.token} +$${bestPos.pnl.toFixed(0)} leading, ${worstPos.token} ${worstPos.pnl >= 0 ? '+' : '-'}$${Math.abs(worstPos.pnl).toFixed(0)} trailing.`;
  }
  if (s.openCount > 0 && bestPos) {
    return `${s.openCount} open · ${bestPos.token} ${bestPos.side} ${bestPos.pnl >= 0 ? '+' : '-'}$${Math.abs(bestPos.pnl).toFixed(0)} @ ${bestPos.live.toFixed(2)}.`;
  }
  const pnlSign = s.stats.sessionPnl >= 0 ? '+' : '-';
  return `flat — session ${pnlSign}$${Math.abs(s.stats.sessionPnl).toFixed(2)}, ${s.stats.wins}W/${s.stats.losses}L, dd ${s.stats.drawdown.toFixed(1)}%.`;
}

function App() {
  const defaults = window.__tweakDefaults || {};
  const [theme, setTheme] = useState(defaults.theme || 'green');
  const [density, setDensity] = useState(defaults.density || 'default');
  const [live, setLive] = useState(defaults.live !== false);

  const [range, setRange] = useState('24h');             // chart range
  const [statsRange, setStatsRange] = useState('24h');   // left-column stats range (independent)
  const [chartMode, setChartMode] = useState('pnl');
  const [tab, setTab] = useState('perps');

  const [snap, setSnap] = useState(window.DATA.EMPTY_SNAPSHOT);
  const [fetchErr, setFetchErr] = useState(null);
  const [newFillFlash, setNewFillFlash] = useState(null);
  const prevTopFillRef = useRef(null);
  const rangeRef = useRef(range);
  const statsRangeRef = useRef(statsRange);
  useEffect(() => { rangeRef.current = range; }, [range]);
  useEffect(() => { statsRangeRef.current = statsRange; }, [statsRange]);
  // Live session PnL trace for the 24h chart. Stores absolute balance
  // samples; conversion to PnL happens in the pts useMemo using whatever
  // baseline is current (today's midnight equity).
  const mtmRef = useRef([]);
  const [mtmRev, setMtmRev] = useState(0);

  const [claudeOpen, setClaudeOpen] = useState(false);
  const [orderOpen, setOrderOpen] = useState(false);
  const [hkOpen, setHkOpen] = useState(false);
  const [insightTick, setInsightTick] = useState(0);

  const snapRef = useRef(snap);
  useEffect(() => { snapRef.current = snap; }, [snap]);

  useEffect(() => { installClaudeStub(snapRef); }, []);

  // Worker-driven 1.5s polling (mirrors dashboard.html).
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const raw = await window.DATA.fetchAccountState();
        if (cancelled) return;
        // Stats card scopes to its own range (statsRange); chart range is independent.
        const days = RANGE_DAYS[statsRangeRef.current] ?? 1;
        const mapped = window.DATA.mapSnapshot(raw, days);
        const topKey = mapped.fills[0]
          ? `${mapped.fills[0].t}|${mapped.fills[0].sym}|${mapped.fills[0].qty}`
          : null;
        if (topKey && prevTopFillRef.current && topKey !== prevTopFillRef.current) {
          setNewFillFlash(Date.now());
        }
        prevTopFillRef.current = topKey;
        // Record live mtm sample carrying real wall-clock ts.
        if (mapped.raw && mapped.balance) {
          mtmRef.current.push({ ts: Date.now(), balance: mapped.balance });
          if (mtmRef.current.length > 57600) {
            mtmRef.current = mtmRef.current.slice(-57600);
          }
          setMtmRev(r => r + 1);
        }
        setSnap(mapped);
        setFetchErr(null);
      } catch (e) {
        if (!cancelled) setFetchErr(String(e.message || e));
      }
    };
    tick();
    if (!live) return;
    let worker = null, fallbackId = null;
    try {
      const blob = new Blob(
        [`setInterval(()=>postMessage(1), ${POLL_MS});`],
        { type: 'application/javascript' }
      );
      worker = new Worker(URL.createObjectURL(blob));
      worker.onmessage = () => tick();
    } catch {
      fallbackId = setInterval(tick, POLL_MS);
    }
    return () => {
      cancelled = true;
      if (worker) worker.terminate();
      if (fallbackId) clearInterval(fallbackId);
    };
  }, [live]);

  // Re-map when STATS range changes (NOT chart range) so the left-column
  // metrics reflect the chosen window immediately. The chart's own range
  // toggle is independent and only affects chart pts.
  useEffect(() => {
    if (!snap.raw || !Object.keys(snap.raw).length) return;
    const days = RANGE_DAYS[statsRange] ?? 1;
    setSnap(window.DATA.mapSnapshot(snap.raw, days));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statsRange]);

  // Claude insight refresh every 45s
  useEffect(() => {
    const id = setInterval(() => setInsightTick(t => t + 1), 45000);
    return () => clearInterval(id);
  }, []);

  // Keyboard shortcuts
  useEffect(() => {
    const onKey = (e) => {
      const inInput = e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA';
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); setClaudeOpen(true); return; }
      if (inInput) return;
      if (e.key === '/') { e.preventDefault(); setClaudeOpen(true); }
      else if (e.key === '?') { e.preventDefault(); setHkOpen(h => !h); }
      else if (e.key === 'Escape') { setClaudeOpen(false); setOrderOpen(false); setHkOpen(false); }
      else if (e.key === 'o') setOrderOpen(o => !o);
      else if (e.key === 'r') setLive(l => !l);
      else if (e.key === 't') setTheme(t => ({ green: 'amber', amber: 'blue', blue: 'pink', pink: 'green' })[t]);
      else if (['1','2','3','4','5','6'].includes(e.key)) {
        const map = ['perps','fills','symbols','attrib','vault','risk'];
        setTab(map[parseInt(e.key) - 1]);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    document.body.className = `theme-${theme} density-${density}`;
  }, [theme, density]);

  // Window baseline — balance recorded in equity_curve just before the range
  // cutoff. Matches dashboard.html L1553-1564.
  const windowBaseline = useMemo(() => {
    const days = RANGE_DAYS[range] ?? 1;
    const startBal = snap.startingBalance || 10000;
    const curve = snap.raw?.equity_curve || [];
    if (days === 0) return startBal;
    // Calendar-day cutoff (local midnight `days-1` days ago). Matches
    // templates/dashboard.html's windowCutoff() so "24h" means
    // "since midnight today", resetting every day at 00:00 local.
    const dt = new Date();
    dt.setHours(0, 0, 0, 0);
    dt.setDate(dt.getDate() - (days - 1));
    const cutoffMs = dt.getTime();
    let bal = startBal;
    for (const p of curve) {
      if (!p.ts) { bal = Number(p.balance) || bal; continue; }
      const s = String(p.ts);
      const iso = s.replace(' ', 'T') + (s.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(s) ? '' : 'Z');
      const t = Date.parse(iso);
      if (Number.isNaN(t)) continue;
      if (t <= cutoffMs) bal = Number(p.balance) || bal;
      else break;
    }
    return bal;
  }, [range, snap.raw, snap.startingBalance]);

  // Chart points — historical equity curve sliced to the window, balance values.
  // Chart.jsx appends `livePx` into its own rolling tail so the right edge
  // animates live between trade closes.
  //
  // The equity_curve has a leading entry with ts=null representing the account
  // starting balance. We include it only for the All range — for 24h / 7d / 30d
  // the left edge anchors at windowBaseline (balance at window cutoff) instead,
  // otherwise the chart would start at $10,000 then jump to the window baseline,
  // showing a spurious dip.
  // Time-aware pts for the 24h chart — each sample carries its real timestamp.
  // Calendar-day window: hardStart = today's local midnight, so the chart
  // resets to $0 at 00:00 every day. Left-anchored at { t: midnight, y: 0 },
  // right endpoint at { t: now, y: current session PnL }.
  // Source of density: `state.mtm_history` — the dashboard refresher samples
  // live equity every 10s and persists it.
  // Falls back to the in-tab mtmRef if the server history is empty.
  const pts24h = useMemo(() => {
    const now = Date.now();
    const midnight = (() => {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.getTime();
    })();
    const hardStart = midnight;

    // Collect real raw-balance samples — mtm_history ONLY. Live-equity
    // convention (balance + sum(unrealized)) so every point is comparable.
    //
    // IMPORTANT: we used to also fold in equity_curve here, but equity_curve
    // is realized-only (cumulative trade PnL, no open unrealized) — mixing
    // the two conventions causes a vertical cliff at the handoff point and
    // invents straight-line pseudo-data through hours where nothing was
    // sampled. Cleaner to show flat $0 until the first real sample and fill
    // in honestly as mtm_history accumulates over the next 24h.
    const raw = [];
    const parseTs = (ts) => {
      if (!ts) return NaN;
      const s = String(ts);
      const iso = s.replace(' ', 'T') + (s.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(s) ? '' : 'Z');
      return Date.parse(iso);
    };
    for (const m of (snap.raw?.mtm_history || [])) {
      const t = parseTs(m.ts);
      if (Number.isNaN(t) || t < hardStart || t > now) continue;
      raw.push({ t, balance: Number(m.balance) || 0 });
    }
    for (const m of mtmRef.current) {
      if (m.ts < hardStart || m.ts > now) continue;
      raw.push({ t: m.ts, balance: Number(m.balance) || 0 });
    }
    raw.sort((a, b) => a.t - b.t);

    // Density throttle — bin samples into 4s buckets, keep last value per
    // bucket. mtm_history (10s) + mtmRef (1.5s) together produce thousands
    // of points that pile up at the right edge and render as dense spikes.
    // Binning preserves the signal shape while capping render density.
    const BIN_MS = 4000;
    const binned = [];
    let lastKey = -1;
    for (const s of raw) {
      const k = Math.floor(s.t / BIN_MS);
      if (k !== lastKey) { binned.push(s); lastKey = k; }
      else binned[binned.length - 1] = s;
    }

    // Baseline: balance at today's midnight. Raw samples → PnL relative to
    // that baseline, so the session resets every day at 00:00.
    const baseline = windowBaseline;

    // Full-window series — real samples only, plus a "now" endpoint.
    // No synthetic { t:midnight, y:0 } anchor: that would draw a fake
    // straight line from y=0 up to the first sample, inventing data for
    // the pre-first-sample gap. The y=0 baseline still renders as a
    // gridline (y-scale includes 0), so "PnL since midnight" stays visible.
    const full = [];
    for (const p of binned) {
      full.push({ t: p.t, y: p.balance - baseline });
    }
    if (snap.raw && snap.balance) {
      full.push({ t: now, y: snap.balance - baseline });
    }

    // Calendar-day window defaults to midnight → now. When the empty prefix
    // (midnight → first real mtm sample) is > 30% of the day elapsed, shift
    // the viewport to ~5min before the first sample so the data fills the
    // plot instead of leaving a big blank strip on the left. Label still
    // reads "today (resets midnight)" — purely a viewport trick, the y=0
    // baseline gridline still represents midnight.
    const firstRealT = binned.length > 0 ? binned[0].t : now;
    const elapsed = Math.max(1, now - hardStart);
    const flatPrefix = firstRealT - hardStart;
    const zoomed = flatPrefix > elapsed * 0.3;
    const FIVE_MIN = 5 * 60 * 1000;
    const rawStart = zoomed ? firstRealT - FIVE_MIN : hardStart;
    const windowStart = Math.floor(rawStart / FIVE_MIN) * FIVE_MIN;
    const windowEnd = now;

    // Trim series to the visible window. The midnight anchor guarantees at
    // least one point; pad to two so the line always renders.
    const series = full.filter(p => p.t >= windowStart && p.t <= windowEnd);
    if (series.length === 0) {
      series.push({ t: windowStart, y: 0 }, { t: windowEnd, y: 0 });
    } else if (series.length === 1) {
      series.push({ t: windowEnd, y: series[0].y });
    }

    const activeMs = now - firstRealT;

    return { series, windowStart, windowEnd, baseline, zoomed, activeMs };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mtmRev, snap.raw, snap.balance, windowBaseline]);

  // Time-aware series for 7d / 30d / All — mirrors the 24h pattern:
  // single clean source (equity_curve trade-close snapshots), live endpoint
  // on the right, auto-zoom when the window has a long flat prefix.
  // No mtm_history here — it uses the live-equity convention (includes
  // unrealized) while equity_curve is realized-only; mixing creates a
  // visual cliff at the handoff point.
  const ptsTimeMulti = useMemo(() => {
    const days = RANGE_DAYS[range] ?? 1;
    if (days === 1) return null;

    const parseTs = (ts) => {
      if (!ts) return NaN;
      const s = String(ts);
      const iso = s.replace(' ', 'T') + (s.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(s) ? '' : 'Z');
      return Date.parse(iso);
    };

    const curve = snap.raw?.equity_curve || [];
    const now = Date.now();
    let hardStart;
    if (days === 0) {
      let earliest = now;
      for (const p of curve) {
        const t = parseTs(p.ts);
        if (!Number.isNaN(t) && t < earliest) earliest = t;
      }
      hardStart = earliest < now ? earliest : now - 7 * 86400 * 1000;
    } else {
      const dt = new Date();
      dt.setHours(0, 0, 0, 0);
      dt.setDate(dt.getDate() - (days - 1));
      hardStart = dt.getTime();
    }
    const WINDOW_MS = Math.max(1, now - hardStart);

    const samples = [];
    for (const p of curve) {
      const t = parseTs(p.ts);
      if (Number.isNaN(t) || t < hardStart || t > now) continue;
      samples.push({ t, balance: Number(p.balance) || 0 });
    }
    samples.sort((a, b) => a.t - b.t);

    const full = samples.slice();
    if (snap.balance) {
      full.push({ t: now, balance: snap.balance });
    }

    // Auto-zoom when > 50% of the window is flat prefix (bot just started,
    // no trades early in the window). Shifts viewport to ~2% lead-in before
    // the first real sample so the data fills the plot instead of being
    // jammed into the right edge. Label stays on the natural range —
    // purely a viewport trick.
    const firstRealT = samples.length > 0 ? samples[0].t : now;
    const flatPrefix = firstRealT - hardStart;
    const zoomed = flatPrefix > WINDOW_MS * 0.5;
    const LEAD_MS = Math.max(5 * 60 * 1000, WINDOW_MS * 0.02);
    const rawStart = zoomed ? firstRealT - LEAD_MS : hardStart;
    // Quantize to clean boundaries so poll-to-poll jitter doesn't re-scale.
    const quantMs = days >= 30 ? 6 * 3600 * 1000 : 5 * 60 * 1000;
    const windowStart = Math.floor(rawStart / quantMs) * quantMs;
    const windowEnd = now;

    const inWindow = full.filter(p => p.t >= windowStart && p.t <= windowEnd);
    // Anchor the left edge at $0 PnL (balance = windowBaseline) so the line
    // visually starts at zero and builds up. The zero-filter in Chart.jsx's
    // y-scale path ignores this anchor so it won't compress scale.
    if (inWindow.length === 0 || inWindow[0].t > windowStart) {
      inWindow.unshift({ t: windowStart, balance: windowBaseline });
    }
    if (inWindow.length === 1) {
      inWindow.push({ t: windowEnd, balance: windowBaseline });
    }

    const pnlSeries = inWindow.map(s => ({ t: s.t, y: s.balance - windowBaseline }));
    const eqSeries  = inWindow.map(s => ({ t: s.t, y: s.balance }));
    return { pnlSeries, eqSeries, windowStart, windowEnd };
  }, [range, snap.raw, snap.balance, windowBaseline]);

  const pts = useMemo(() => {
    const days = RANGE_DAYS[range] ?? 1;
    if (days === 1) return pts24h.series.map(p => p.y);
    if (ptsTimeMulti) return ptsTimeMulti.eqSeries.map(p => p.y);
    return [windowBaseline, windowBaseline];
  }, [range, pts24h, ptsTimeMulti, windowBaseline]);

  // On 24h, pts ALREADY contains PnL values (first-sample-anchored to 0).
  // On other ranges, pts is raw balance and we subtract windowBaseline.
  const pnlPts = useMemo(() => (
    range === '24h' ? pts : pts.map(p => p - windowBaseline)
  ), [pts, windowBaseline, range]);
  const effectiveMode = range === '24h' ? 'pnl' : chartMode;
  const displayPts = effectiveMode === 'pnl' ? pnlPts : pts;
  // All ranges now drive the chart through tSeries (time-aware). Chart.jsx's
  // legacy liveTail path isn't used, so we pass null everywhere.
  const hasRealState = snap.raw && Object.keys(snap.raw).length > 0;
  const livePx = null;
  // 24h chart baselined to today's midnight balance (windowBaseline).
  // Headline = right-most curve value = session PnL since midnight.
  const allPnl = range === '24h'
    ? (pts24h.series.length > 0 ? pts24h.series[pts24h.series.length - 1].y : 0)
    : (hasRealState ? snap.balance - windowBaseline : 0);

  // Claude context
  const claudeContext = useMemo(() => {
    const posLines = snap.positions.map(p =>
      `${p.token} ${p.side} size=${p.size.toFixed(2)} entry=${p.avgEntry.toFixed(4)} live=${p.live.toFixed(4)} pnl=$${p.pnl.toFixed(2)} (${p.pnlPct.toFixed(2)}%) lev=${p.leverage}× bars=${p.slHit}/${p.slMax}`
    ).join('\n');
    const topContrib = snap.symbolsAttrib.filter(s => s.pnl > 0).slice(0, 3).map(s => `${s.sym} +$${s.pnl.toFixed(0)}`).join(', ');
    const drags = snap.symbolsAttrib.filter(s => s.pnl < 0).slice(0, 3).map(s => `${s.sym} -$${Math.abs(s.pnl).toFixed(0)}`).join(', ');
    return `equity=$${snap.balance.toFixed(2)} sessionPnL=${snap.stats.sessionPnl >= 0 ? '+' : '-'}$${Math.abs(snap.stats.sessionPnl).toFixed(2)} (${snap.stats.sessionPnlPct.toFixed(2)}%)
winRate=${snap.stats.winRate.toFixed(1)}% profitFactor=${snap.stats.profitFactor.toFixed(2)} drawdown=${snap.stats.drawdown.toFixed(2)}%
strategy=whale-swing (${snap.mode.toLowerCase()}, ${snap.network.toLowerCase()}) eff_lev=${snap.effectiveLev}× max_concurrent=${snap.maxConcurrent}

positions (${snap.openCount}):
${posLines || '  (none)'}

top: ${topContrib || '—'}
drags: ${drags || '—'}`;
  }, [snap]);

  const sentimentScore = snap.biasScore ?? 0.5;
  const sentimentLabel = snap.biasLabel ?? 'Flat';
  const leverageGauge = Math.max(0, Math.min(5, snap.portfolioLev ?? 0));

  return (
    <div className={`app theme-${theme} density-${density}`}>
      <TopBar
        live={live}
        snap={snap}
        page="terminal"
        onCmdK={() => setClaudeOpen(true)}
        onOrder={() => setOrderOpen(true)}
        onHelp={() => setHkOpen(true)}
      />
      <TickerStrip tickers={snap.tickers}/>
      <ClaudeInsight context={claudeContext} tick={insightTick}/>
      <div className="main">
        <div className="left-col">
          <EquityCard
            equity={snap.balance}
            dayChange={snap.stats.sessionPnl}
            dayChangePct={snap.stats.sessionPnlPct}
            inUse={snap.positions.reduce((a, p) => a + (p.margin || 0), 0)}
            unrealized={snap.unrealizedTotal}
            realizedDay={(snap.raw.daily_pnl_series?.slice(-1)?.[0]?.pnl) ?? snap.raw.daily_pnl ?? 0}
          />
          <SentLevCard
            sentiment={sentimentLabel}
            score={sentimentScore}
            leverage={leverageGauge}
          />
          <SessionStats
            range={statsRange}
            setRange={setStatsRange}
            ranges={['24h','7d','30d','All']}
            stats={snap.stats}
          />
        </div>
        <div className="right-col">
          <EquityChart
            range={range}
            setRange={setRange}
            pts={displayPts}
            allPnl={allPnl}
            mode={effectiveMode}
            setMode={setChartMode}
            livePx={livePx}
            tSeries={
              range === '24h'
                ? pts24h.series
                : (ptsTimeMulti
                    ? (effectiveMode === 'pnl' ? ptsTimeMulti.pnlSeries : ptsTimeMulti.eqSeries)
                    : null)
            }
            tWindow={
              range === '24h'
                ? { start: pts24h.windowStart, end: pts24h.windowEnd }
                : (ptsTimeMulti
                    ? { start: ptsTimeMulti.windowStart, end: ptsTimeMulti.windowEnd }
                    : null)
            }
            activeMs={range === '24h' && pts24h.zoomed ? pts24h.activeMs : null}
          />
          <LowerPanel
            tab={tab}
            setTab={setTab}
            positions={snap.positions}
            fills={snap.fills}
            symbolsAttrib={snap.symbolsAttrib}
            newFillFlash={newFillFlash}
          />
        </div>
      </div>
      {orderOpen && <OrderDrawer open={orderOpen} setOpen={setOrderOpen}/>}
      <ClaudeBar open={claudeOpen} setOpen={setClaudeOpen} context={claudeContext}/>
      <HotkeyOverlay open={hkOpen} setOpen={setHkOpen}/>
      {fetchErr && (
        <div style={{position:'fixed', right:12, bottom:12, padding:'6px 10px',
                     background:'var(--panel)', border:'1px solid var(--neg)',
                     color:'var(--neg)', fontSize:10}}>
          api: {fetchErr}
        </div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
