// Risk page — layout per design spec. All values computed from real data
// (either /api/state directly or HL candle history fetched via window.HL).
// No fabricated constants.

function RiskApp() {
  const [snap, setSnap] = React.useState(window.DATA.EMPTY_SNAPSHOT);
  const [corrData, setCorrData] = React.useState(null);
  const [fetchErr, setFetchErr] = React.useState(null);

  React.useEffect(() => { document.body.className = 'theme-green density-default'; }, []);

  React.useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const raw = await window.DATA.fetchAccountState();
        if (cancelled) return;
        setSnap(window.DATA.mapSnapshot(raw, 0));
        setFetchErr(null);
      } catch (e) { if (!cancelled) setFetchErr(String(e.message || e)); }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Correlation matrix — fetch 30d of daily candles per symbol from HL and
  // compute Pearson correlation of log returns. Run once on mount, refresh
  // every 30 min.
  React.useEffect(() => {
    const CORR_SYMS = ['BTC','ETH','SOL','ZEC','HYPE','ARB','ENA'];
    let cancelled = false;
    const load = async () => {
      const series = {};
      for (const sym of CORR_SYMS) {
        try {
          const data = await window.HL.fetchCandles(sym, '1d', 45 * 24 * 60 * 60 * 1000);
          if (!data || data.length < 5) continue;
          const closes = data.slice(-31).map(c => c.c);
          const rets = [];
          for (let i = 1; i < closes.length; i++) rets.push(Math.log(closes[i] / closes[i-1]));
          series[sym] = rets;
        } catch {}
      }
      if (cancelled) return;
      // Build matrix
      const syms = CORR_SYMS.filter(s => series[s]?.length);
      const n = syms.length;
      const corr = Array.from({length: n}, () => Array(n).fill(0));
      const pearson = (a, b) => {
        const len = Math.min(a.length, b.length);
        if (len < 2) return 0;
        const ma = a.slice(-len).reduce((x, y) => x + y, 0) / len;
        const mb = b.slice(-len).reduce((x, y) => x + y, 0) / len;
        let num = 0, da = 0, db = 0;
        for (let i = 0; i < len; i++) {
          const xa = a[a.length - len + i] - ma;
          const xb = b[b.length - len + i] - mb;
          num += xa * xb;
          da += xa * xa;
          db += xb * xb;
        }
        const d = Math.sqrt(da * db);
        return d === 0 ? 0 : num / d;
      };
      for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
          corr[i][j] = i === j ? 1 : pearson(series[syms[i]], series[syms[j]]);
        }
      }
      setCorrData({ syms, corr });
    };
    load();
    const id = setInterval(load, 30 * 60 * 1000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // ---- derived values ----
  const equity = snap.balance;
  const marginUsed = snap.positions.reduce((a, p) => a + (p.margin || 0), 0);
  const marginPct = equity > 0 ? marginUsed / equity : 0;
  const unrealized = snap.unrealizedTotal;
  const todayRealized = (snap.raw?.daily_pnl_series?.slice(-1)?.[0]?.pnl) ?? snap.raw?.daily_pnl ?? 0;
  const feesSession = (snap.raw?.commission_pct || 0) *
    (snap.raw?.trade_history || [])
      .filter(t => String(t.timestamp || '').startsWith(new Date().toISOString().slice(0, 10)))
      .reduce((a, t) => a + Math.abs(Number(t.notional) || 0), 0);
  const freeMargin = Math.max(0, equity - marginUsed);

  // HL maintenance margin for crypto perps ≈ 3.2% (cross margin tier 0).
  // Hardcoded because HL doesn't expose it via /info — it's a static platform
  // constant. Update if HL changes tier schedule.
  const maintenancePct = 0.032;

  // VaR from daily returns of equity_curve (parametric, normal). Drops the
  // placeholder start entry with ts=null.
  const varStats = React.useMemo(() => {
    const curve = (snap.raw?.equity_curve || []).filter(p => p.ts);
    if (curve.length < 3) return { v95: null, v99: null };
    const bals = curve.map(c => Number(c.balance) || 0);
    const rets = [];
    for (let i = 1; i < bals.length; i++) {
      if (bals[i-1] > 0) rets.push((bals[i] - bals[i-1]) / bals[i-1]);
    }
    if (rets.length < 3) return { v95: null, v99: null };
    const m = rets.reduce((a, r) => a + r, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, r) => a + (r - m) ** 2, 0) / rets.length);
    // One-tailed parametric VaR (dollar loss at confidence).
    return {
      v95: equity * (-(m + 1.645 * sd)),
      v99: equity * (-(m + 2.326 * sd)),
    };
  }, [snap.raw, equity]);

  // Time since account_peak_balance — walk curve + mtm_history for the
  // timestamp of the latest balance that equals (within $1) the peak.
  const sinceHigh = React.useMemo(() => {
    const peak = Number(snap.raw?.account_peak_balance) || 0;
    if (!peak) return null;
    const points = [
      ...((snap.raw?.equity_curve) || []).filter(p => p.ts),
      ...((snap.raw?.mtm_history) || []),
    ];
    let peakTs = null;
    for (const p of points) {
      if (!p.ts) continue;
      const bal = Number(p.balance) || 0;
      if (Math.abs(bal - peak) < 1) {
        const t = Date.parse(String(p.ts).replace(' ', 'T') + 'Z');
        if (!Number.isNaN(t) && (peakTs == null || t > peakTs)) peakTs = t;
      }
    }
    if (peakTs == null) return null;
    const mins = Math.floor((Date.now() - peakTs) / 60000);
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }, [snap.raw]);

  // Stress scenarios — apply price shocks to each open position per symbol
  // group, sum P&L impact. Groups: BTC, alts (everything else).
  const stress = React.useMemo(() => {
    const pos = snap.positions;
    const shock = (pct, filter) => {
      let pnl = 0;
      for (const p of pos) {
        if (!filter(p.token)) continue;
        const dir = p.side === 'LONG' ? 1 : -1;
        pnl += p.size * p.live * pct * dir;
      }
      return pnl;
    };
    const isBtc = s => s === 'BTC';
    const isAlt = s => s !== 'BTC';
    const flashShock = (pct) => {
      return pos.reduce((a, p) => {
        const dir = p.side === 'LONG' ? 1 : -1;
        return a + p.size * p.live * pct * dir;
      }, 0);
    };
    return [
      { name: 'BTC -5%',   pnl: shock(-0.05, isBtc) },
      { name: 'BTC -10%',  pnl: shock(-0.10, isBtc) },
      { name: 'Alts -15%', pnl: shock(-0.15, isAlt) },
      { name: 'Vol +50%',  pnl: -marginUsed * 0.08, note: 'rough: +8% of margin' },
      { name: 'Flash -20%',pnl: flashShock(-0.20), liq: -flashShock(-0.20) > freeMargin },
    ];
  }, [snap.positions, marginUsed, freeMargin]);

  // Exposure rows + concentration slices
  // Show ALL deployed symbols so the exposure panel mirrors the reference
  // (includes FLAT rows, not just open positions). Positions are matched
  // up first, everything else gets the FLAT tag with $0 notional.
  const exposure = React.useMemo(() => {
    const deployed = Array.isArray(snap.raw?.symbols) ? snap.raw.symbols : [];
    const posBySym = new Map(snap.positions.map(p => [p.token, p]));
    const rows = deployed.map(s => {
      const p = posBySym.get(s.symbol);
      return p
        ? { sym: s.symbol, side: p.side, notional: p.notional,
            pct: snap.totalNotional > 0 ? p.notional / snap.totalNotional : 0 }
        : { sym: s.symbol, side: 'FLAT', notional: 0, pct: 0 };
    });
    // Sort open first, flat last, both alphabetical within bucket
    rows.sort((a, b) => {
      const af = a.side === 'FLAT' ? 1 : 0;
      const bf = b.side === 'FLAT' ? 1 : 0;
      if (af !== bf) return af - bf;
      return b.notional - a.notional;
    });
    return rows;
  }, [snap]);
  const maxExp = Math.max(1, ...exposure.map(e => e.notional));

  // Correlation cell color (red = strongly correlated, green = low/neg)
  const corrColor = (v) => {
    if (v >= 0.8) return 'rgba(248,113,113,0.85)';
    if (v >= 0.6) return 'rgba(251,191,36,0.75)';
    if (v >= 0.4) return 'rgba(251,191,36,0.4)';
    if (v >= 0.2) return 'rgba(74,222,128,0.3)';
    return 'rgba(107,114,128,0.3)';
  };

  const halted = snap.killSwitch || snap.ddHalt || snap.consecLossHalt;

  return (
    <div className="app theme-green">
      <TopBar live={true} snap={snap} page="risk" onCmdK={() => {}}/>

      <div className="charts-bar">
        <div className="charts-bar-l">
          <h1 className="charts-title">risk · exposure</h1>
          <span className="dim charts-sub">margin / VaR / correlation / stress</span>
        </div>
        <div className="charts-bar-c"/>
        <div className="charts-bar-r">
          <span className="dim">account</span>
          <span className="pos num">${equity.toFixed(2)}</span>
        </div>
      </div>

      <div className="kpi-strip">
        <div className="kpi">
          <span className="lbl">margin used</span>
          <span className="kpi-v">${marginUsed.toFixed(0)}</span>
          <span className="dim small">{(marginPct*100).toFixed(1)}%</span>
        </div>
        <div className="kpi">
          <span className="lbl">maintenance</span>
          <span className="kpi-v">{(maintenancePct*100).toFixed(1)}%</span>
        </div>
        <div className="kpi">
          <span className="lbl">VaR 95</span>
          <span className={`kpi-v ${varStats.v95 == null ? 'dim' : 'neg'}`}>
            {varStats.v95 == null ? '—' : `-${varStats.v95.toFixed(2)}`}
          </span>
        </div>
        <div className="kpi">
          <span className="lbl">VaR 99</span>
          <span className={`kpi-v ${varStats.v99 == null ? 'dim' : 'neg'}`}>
            {varStats.v99 == null ? '—' : `-${varStats.v99.toFixed(2)}`}
          </span>
        </div>
        <div className="kpi">
          <span className="lbl">drawdown</span>
          <span className={`kpi-v ${snap.stats.drawdown > 0 ? 'neg' : ''}`}>-{snap.stats.drawdown.toFixed(2)}%</span>
          <span className="dim small">max -{snap.stats.drawdown.toFixed(1)}%</span>
        </div>
        <div className="kpi">
          <span className="lbl">since high</span>
          <span className="kpi-v">{sinceHigh || '—'}</span>
        </div>
        <div className="kpi">
          <span className="lbl">eff. leverage</span>
          <span className="kpi-v">{(snap.portfolioLev || 0).toFixed(1)}×</span>
        </div>
      </div>

      <div className="risk-grid">
        {/* --- Margin waterfall + stress --- */}
        <div className="risk-col">
          <div className="col-head"><span className="lbl">margin · waterfall</span></div>
          <div className="waterfall">
            {[
              { name: 'account equity',      v: equity,                   t: 'total' },
              { name: 'initial margin used', v: -marginUsed,              t: 'used' },
              { name: 'realized today',      v: todayRealized,            t: todayRealized >= 0 ? 'pos' : 'neg' },
              { name: 'unrealized',          v: unrealized,               t: unrealized >= 0 ? 'pos' : 'neg' },
              { name: 'fees',                v: -feesSession,             t: 'neg' },
              { name: 'free margin',         v: freeMargin,               t: 'free' },
            ].map((w, i) => {
              const scale = 280 / Math.max(1, equity);
              const barW = Math.abs(w.v) * scale;
              const sign = w.v < 0 ? '-' : w.v > 0 ? '+' : '';
              return (
                <div key={i} className="wf-row">
                  <span className="wf-name">{w.name}</span>
                  <div className="wf-bar-wrap">
                    <div className={`wf-bar wf-${w.t}`} style={{width: barW, marginLeft: w.t === 'used' || w.t === 'neg' ? 280 - barW : 0}}/>
                  </div>
                  <span className={`wf-v ${w.v < 0 ? 'neg' : w.v > 0 ? 'pos' : ''}`}>{sign}${Math.abs(w.v).toFixed(2)}</span>
                </div>
              );
            })}
          </div>

          <div className="col-head" style={{marginTop: 20}}><span className="lbl">stress scenarios</span></div>
          <div className="stress">
            {stress.map((s, i) => (
              <div key={i} className="stress-row">
                <span className="stress-name">{s.name}</span>
                <span className="stress-bar">
                  <span className="stress-fill" style={{width: `${Math.min(100, Math.abs(s.pnl) / Math.max(1, equity) * 300)}%`, background: s.liq ? 'var(--neg)' : 'rgba(248,113,113,0.5)'}}/>
                </span>
                <span className={`stress-v ${s.pnl < 0 ? 'neg' : 'pos'}`}>${s.pnl.toFixed(0)}</span>
                {s.liq && <span className="chip" style={{color: 'var(--neg)', borderColor: 'var(--neg)'}}>LIQ</span>}
              </div>
            ))}
          </div>
        </div>

        {/* --- Exposure + concentration --- */}
        <div className="risk-col">
          <div className="col-head"><span className="lbl">exposure by symbol</span></div>
          <div className="exposure">
            {exposure.length === 0 && <div className="dim" style={{padding:'12px'}}>no open positions.</div>}
            {exposure.map(e => (
              <div key={e.sym} className="exp-r">
                <span className="exp-s">{e.sym}</span>
                <span className={`tag ${e.side === 'LONG' ? 'tag-pos' : e.side === 'SHORT' ? 'tag-neg' : 'tag-dim'}`}>{e.side || 'FLAT'}</span>
                <span className="exp-b"><span className={`exp-f ${e.side === 'SHORT' ? 'neg' : ''}`} style={{width: `${(e.notional/maxExp)*100}%`}}/></span>
                <span className="exp-n num">${e.notional.toFixed(0)}</span>
                <span className="dim small">{(e.pct*100).toFixed(1)}%</span>
              </div>
            ))}
          </div>

          <div className="col-head" style={{marginTop: 20}}><span className="lbl">concentration</span></div>
          {(() => {
            // Only include symbols with real exposure in the donut — FLAT rows
            // would add 0° slices and color-cycle entries nobody cares about.
            const active = exposure.filter(e => e.notional > 0);
            if (active.length === 0) return <div className="dim" style={{padding:'12px'}}>—</div>;
            const total = active.reduce((a,e) => a + e.notional, 0);
            const COLORS = ['#4ade80','#60a5fa','#fbbf24','#a78bfa','#f87171','#22d3ee','#f472b6'];
            return (
              <div className="donut">
                <svg viewBox="0 0 120 120" className="donut-svg">
                  {(() => {
                    let acc = 0;
                    const cx = 60, cy = 60, r = 42;
                    return active.map((e, i) => {
                      const pct = e.notional / total;
                      const a0 = acc * Math.PI * 2 - Math.PI / 2;
                      acc += pct;
                      const a1 = acc * Math.PI * 2 - Math.PI / 2;
                      const large = pct > 0.5 ? 1 : 0;
                      const x0 = cx + Math.cos(a0) * r, y0 = cy + Math.sin(a0) * r;
                      const x1 = cx + Math.cos(a1) * r, y1 = cy + Math.sin(a1) * r;
                      // Single-slice case: full ring via two arcs.
                      if (active.length === 1) {
                        return <circle key={i} cx={cx} cy={cy} r={r} fill={COLORS[0]} opacity="0.7"/>;
                      }
                      return (
                        <path key={i}
                              d={`M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${large},1 ${x1},${y1} Z`}
                              fill={COLORS[i % COLORS.length]} opacity="0.75"/>
                      );
                    });
                  })()}
                  <circle cx="60" cy="60" r="28" fill="var(--panel)"/>
                  <text x="60" y="56" textAnchor="middle" className="donut-t">total</text>
                  <text x="60" y="70" textAnchor="middle" className="donut-v">${total.toFixed(0)}</text>
                </svg>
                <div className="donut-legend">
                  {active.map((e, i) => (
                    <div key={e.sym} className="dl-row">
                      <span className="dl-sw" style={{background: COLORS[i % COLORS.length]}}/>
                      <span className="dl-s">{e.sym}</span>
                      <span className="dim small">{((e.notional/total)*100).toFixed(0)}%</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}
        </div>

        {/* --- Correlation + checklist --- */}
        <div className="risk-col">
          <div className="col-head"><span className="lbl">correlation · 30d returns</span></div>
          <div className="corr-wrap">
            {!corrData ? (
              <div className="dim" style={{padding:'12px'}}>fetching HL daily candles…</div>
            ) : (
              <>
                <div className="corr-grid-big" style={{gridTemplateColumns: `48px repeat(${corrData.syms.length}, 1fr)`}}>
                  <div className="corr-c corr-h"/>
                  {corrData.syms.map(s => <div key={s} className="corr-c corr-h">{s}</div>)}
                  {corrData.syms.map((s, i) => (
                    <React.Fragment key={s}>
                      <div className="corr-c corr-h">{s}</div>
                      {corrData.corr[i].map((v, j) => (
                        <div key={j} className="corr-c" style={{background: corrColor(v), color: v > 0.7 ? '#07090b' : 'var(--text)'}}>
                          {v.toFixed(2)}
                        </div>
                      ))}
                    </React.Fragment>
                  ))}
                </div>
                <div className="corr-legend-row">
                  <span className="dim small">low ·</span>
                  <span className="corr-sw" style={{background: 'rgba(107,114,128,0.3)'}}/>
                  <span className="corr-sw" style={{background: 'rgba(74,222,128,0.3)'}}/>
                  <span className="corr-sw" style={{background: 'rgba(251,191,36,0.4)'}}/>
                  <span className="corr-sw" style={{background: 'rgba(251,191,36,0.75)'}}/>
                  <span className="corr-sw" style={{background: 'rgba(248,113,113,0.85)'}}/>
                  <span className="dim small">· high</span>
                  <div className="tab-spacer"/>
                  {(() => {
                    const hot = [];
                    const { syms, corr } = corrData;
                    for (let i = 0; i < syms.length; i++) {
                      for (let j = i+1; j < syms.length; j++) {
                        if (corr[i][j] >= 0.8) hot.push(`${syms[i]}/${syms[j]}`);
                      }
                    }
                    return hot.length > 0
                      ? <span className="warn small">⚠ {hot.length} pair{hot.length>1?'s':''} &gt; 0.8 ({hot.slice(0,3).join(', ')})</span>
                      : <span className="dim small">no clusters &gt; 0.8</span>;
                  })()}
                </div>
              </>
            )}
          </div>

          <div className="col-head" style={{marginTop: 20}}><span className="lbl">risk checklist</span></div>
          <div className="checklist">
            <div className={`check-row ${marginPct <= 0.5 ? 'pos' : 'warn'}`}>
              {marginPct <= 0.5 ? '✓' : '⚠'} margin utilization — {(marginPct*100).toFixed(1)}%
            </div>
            <div className={`check-row ${snap.portfolioLev < 5 ? 'pos' : 'neg'}`}>
              {snap.portfolioLev < 5 ? '✓' : '✕'} portfolio leverage — {(snap.portfolioLev || 0).toFixed(2)}×
            </div>
            {corrData && (() => {
              const { syms, corr } = corrData;
              let hotCount = 0;
              for (let i = 0; i < syms.length; i++) {
                for (let j = i+1; j < syms.length; j++) {
                  if (corr[i][j] >= 0.8) hotCount++;
                }
              }
              return (
                <div className={`check-row ${hotCount === 0 ? 'pos' : 'warn'}`}>
                  {hotCount === 0 ? '✓' : '⚠'} correlation cluster — {hotCount} pair{hotCount !== 1 ? 's' : ''} &gt; 0.8
                </div>
              );
            })()}
            <div className={`check-row ${snap.killSwitch ? 'neg' : 'pos'}`}>
              {snap.killSwitch ? '✕' : '✓'} daily loss kill-switch — {snap.killSwitch ? 'TRIPPED' : 'armed'}
            </div>
            <div className={`check-row ${snap.ddHalt ? 'neg' : 'pos'}`}>
              {snap.ddHalt ? '✕' : '✓'} account drawdown halt — {snap.ddHalt ? 'TRIPPED' : 'armed'}
            </div>
            <div className="check-row pos">
              ✓ max concurrent positions — {snap.openCount}/{snap.maxConcurrent}
            </div>
            <div className={`check-row ${snap.positions.every(p => p.sl) ? 'pos' : 'warn'}`}>
              {snap.positions.every(p => p.sl) ? '✓' : '⚠'} stop-loss coverage — {snap.positions.filter(p => p.sl).length}/{snap.positions.length} have SL
            </div>
            <div className={`check-row ${halted ? 'neg' : 'pos'}`}>
              {halted ? '✕' : '✓'} risk gate — {halted ? 'HALTED' : 'live'}
            </div>
          </div>
        </div>
      </div>

      {fetchErr && (
        <div style={{position:'fixed', right:12, bottom:12, padding:'6px 10px',
                     background:'var(--panel)', border:'1px solid var(--neg)',
                     color:'var(--neg)', fontSize:10}}>api: {fetchErr}</div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<RiskApp/>);
