// Strategies page — groups symbols by `entry_type` (the bot only runs one
// strategy, `whale-swing`, but its per-symbol configs use different entry
// types which act as strategy variants). Cards + overlaid 30d cumulative
// return curves + head-to-head table, all from /api/state.

const STRAT_COLORS = ['#4ade80', '#60a5fa', '#fbbf24', '#a78bfa', '#f87171', '#22d3ee', '#f472b6'];

function StrategiesApp() {
  const [snap, setSnap] = React.useState(window.DATA.EMPTY_SNAPSHOT);
  const [selected, setSelected] = React.useState(null);   // null = all; otherwise Set<string>
  const [fetchErr, setFetchErr] = React.useState(null);
  const [hover, setHover] = React.useState(null);   // { x, tAt, snaps: [{g, pct, y}] }
  const svgRef = React.useRef(null);

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
    // 1.5s cadence matches the main dashboard — strategy curves and KPIs
    // re-roll continuously as mark prices tick.
    const id = setInterval(tick, 1500);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Build per-entry-type groups.
  const groups = React.useMemo(() => {
    const syms = Array.isArray(snap.raw?.symbols) ? snap.raw.symbols : [];
    const th = Array.isArray(snap.raw?.trade_history) ? snap.raw.trade_history : [];
    const symToType = new Map(syms.map(s => [s.symbol, s.entry_type || 'unknown']));
    const posBySym = new Map(snap.positions.map(p => [p.token, p]));

    const now = Date.now();
    const cutoff = now - 30 * 24 * 3600 * 1000;
    const groupsMap = new Map(); // entry_type -> group object

    for (const s of syms) {
      const et = s.entry_type || 'unknown';
      if (!groupsMap.has(et)) {
        groupsMap.set(et, {
          id: et,
          name: et,
          symbols: [],
          inPositionSymbols: [],
          trades: [],
          // Always anchor the left edge at 0 PnL at the window start so the
          // line spans the full 30d axis, matching the behaviour of the main
          // dashboard chart.
          cumCurve: [{ t: cutoff, pnl: 0 }],
        });
      }
      const g = groupsMap.get(et);
      g.symbols.push(s.symbol);
      if (posBySym.has(s.symbol)) g.inPositionSymbols.push(s.symbol);
    }

    // Walk trade_history once, sorted ascending by ts, append to the
    // appropriate group.
    const sortedTrades = [...th].filter(t => {
      const ts = Date.parse(String(t.timestamp || '').replace(' ', 'T') + 'Z');
      return !Number.isNaN(ts) && ts >= cutoff;
    }).sort((a, b) => {
      const ta = Date.parse(String(a.timestamp || '').replace(' ', 'T') + 'Z');
      const tb = Date.parse(String(b.timestamp || '').replace(' ', 'T') + 'Z');
      return ta - tb;
    });

    // First pass — raw per-trade cumulative per group.
    const rawCurves = new Map(); // entry_type -> [{t, pnl}]
    for (const g of groupsMap.values()) rawCurves.set(g.id, []);
    for (const t of sortedTrades) {
      const et = symToType.get(t.symbol);
      if (!et) continue;
      const g = groupsMap.get(et);
      if (!g) continue;
      const tsMs = Date.parse(String(t.timestamp || '').replace(' ', 'T') + 'Z');
      g.trades.push(t);
      const raw = rawCurves.get(g.id);
      const prev = raw.length ? raw[raw.length - 1].pnl : 0;
      raw.push({ t: tsMs, pnl: prev + (Number(t.pnl) || 0) });
    }

    // Second pass — resample each curve at daily boundaries using carry-
    // forward, so every group has one point per UTC day in the 30-day window.
    // This gives a smooth visual like the reference instead of jagged
    // per-trade steps concentrated in the last few days.
    const dayMs = 24 * 3600 * 1000;
    const dayStart = Math.floor(cutoff / dayMs) * dayMs;
    const dayCount = Math.ceil((now - dayStart) / dayMs) + 1;
    for (const g of groupsMap.values()) {
      const raw = rawCurves.get(g.id);
      const daily = [];
      let ri = 0;
      let runningPnl = 0;
      for (let d = 0; d < dayCount; d++) {
        const t = dayStart + d * dayMs;
        // Consume all raw trades whose timestamp is ≤ this day's end.
        const dayEnd = Math.min(t + dayMs, now);
        while (ri < raw.length && raw[ri].t <= dayEnd) {
          runningPnl = raw[ri].pnl;
          ri++;
        }
        daily.push({ t: Math.min(t, now), pnl: runningPnl });
      }
      // Live right-edge — fold in unrealized PnL from open positions in
      // this entry_type so the curve ticks with mark prices, not just on
      // close events. Realized (runningPnl) + unrealized (live mark).
      let unrealized = 0;
      for (const sym of g.inPositionSymbols) {
        const pos = posBySym.get(sym);
        if (pos) unrealized += Number(pos.pnl) || 0;
      }
      const livePnl = runningPnl + unrealized;
      if (daily.length === 0 || daily[daily.length - 1].t < now) {
        daily.push({ t: now, pnl: livePnl });
      } else {
        // Overwrite the last bucket with the live value so the right edge
        // always reflects mark-to-market, not just the last close.
        daily[daily.length - 1] = { t: now, pnl: livePnl };
      }
      g.cumCurve = daily;
    }

    // Compute KPIs per group
    const startBal = Number(snap.startingBalance) || 10000;
    const groupsArr = Array.from(groupsMap.values()).map((g, i) => {
      const pnls = g.trades.map(t => Number(t.pnl) || 0);
      const wins = pnls.filter(p => p > 0);
      const losses = pnls.filter(p => p < 0);
      const realizedTotal = pnls.reduce((a, p) => a + p, 0);
      // Add unrealized from currently open positions in this group so the
      // card's $ figure ticks with live prices, not just at close time.
      let unrealizedTotal = 0;
      for (const sym of g.inPositionSymbols) {
        const pos = posBySym.get(sym);
        if (pos) unrealizedTotal += Number(pos.pnl) || 0;
      }
      const total = realizedTotal + unrealizedTotal;
      const wr = pnls.length ? (wins.length / pnls.length) : 0;
      const grossW = wins.reduce((a, p) => a + p, 0);
      const grossL = Math.abs(losses.reduce((a, p) => a + p, 0));
      const pf = grossL > 0 ? grossW / grossL : (grossW > 0 ? Infinity : 0);
      const avgWin = wins.length ? grossW / wins.length : 0;
      const avgLoss = losses.length ? grossL / losses.length : 0;
      const avgR = avgLoss > 0 ? avgWin / avgLoss : (avgWin > 0 ? Infinity : 0);
      // Max drawdown on cumulative curve
      let peak = 0, maxDD = 0;
      for (const pt of g.cumCurve) {
        peak = Math.max(peak, pt.pnl);
        const dd = peak - pt.pnl;
        if (dd > maxDD) maxDD = dd;
      }
      const maxDDpct = startBal > 0 ? (maxDD / startBal) * 100 : 0;
      // Crude Sharpe from per-trade pnl (not annualized, relative only)
      const mean = pnls.length ? total / pnls.length : 0;
      const variance = pnls.length
        ? pnls.reduce((a, p) => a + (p - mean) ** 2, 0) / pnls.length
        : 0;
      const sd = Math.sqrt(variance);
      const sharpe = sd > 0 ? mean / sd : 0;
      // Avg hold
      const avgBars = g.trades.length
        ? g.trades.reduce((a, t) => a + (Number(t.held_bars) || 0), 0) / g.trades.length
        : 0;
      const avgHoldMin = Math.round(avgBars * 5);
      const hh = Math.floor(avgHoldMin / 60);
      const mm = avgHoldMin % 60;
      const avgHold = g.trades.length ? `${hh}h ${String(mm).padStart(2,'0')}m` : '—';

      const status = g.inPositionSymbols.length > 0 ? 'LIVE' : (g.symbols.length > 0 ? 'IDLE' : 'OFF');

      return {
        ...g,
        color: STRAT_COLORS[i % STRAT_COLORS.length],
        pnl30d: total,
        pnl30dPct: startBal > 0 ? (total / startBal) * 100 : 0,
        tradesCount: pnls.length,
        wr: wr * 100,
        pf: Number.isFinite(pf) ? pf : 0,
        pfInfinite: pf === Infinity,
        avgR: Number.isFinite(avgR) ? avgR : 0,
        maxDDpct,
        sharpe,
        avgHold,
        status,
      };
    }).sort((a, b) => b.pnl30d - a.pnl30d);

    return groupsArr;
  }, [snap]);

  const isOn = (id) => selected === null || selected.has(id);
  const toggle = (id) => {
    setSelected(prev => {
      const base = prev === null ? new Set(groups.map(g => g.id)) : new Set(prev);
      if (base.has(id)) base.delete(id); else base.add(id);
      return base.size === groups.length ? null : base;
    });
  };

  // Overlaid equity curves
  const W = 1200, H = 420, padL = 60, padR = 32, padT = 20, padB = 28;
  const iw = W - padL - padR, ih = H - padT - padB;
  const shownGroups = groups.filter(g => isOn(g.id));

  const now = Date.now();
  const windowStart = now - 30 * 24 * 3600 * 1000;
  const allPctValues = [];
  const startBal = Number(snap.startingBalance) || 10000;
  for (const g of shownGroups) {
    for (const pt of g.cumCurve) {
      allPctValues.push((pt.pnl / startBal) * 100);
    }
  }
  const minV = allPctValues.length ? Math.min(0, ...allPctValues) : 0;
  const maxV = allPctValues.length ? Math.max(0, ...allPctValues) : 1;
  const pad = (maxV - minV) * 0.1 || 1;
  const lo = minV - pad, hi = maxV + pad;

  const xt = (t) => padL + ((t - windowStart) / Math.max(1, now - windowStart)) * iw;
  const yv = (v) => padT + ih - ((v - lo) / (hi - lo)) * ih;

  return (
    <div className="app theme-green">
      <TopBar live={true} snap={snap} page="strategies" onCmdK={() => {}}/>

      <div className="charts-bar">
        <div className="charts-bar-l">
          <h1 className="charts-title">strategies · compare</h1>
          <span className="dim charts-sub">30d · grouped by entry_type · click a card to toggle overlay</span>
        </div>
        <div className="charts-bar-c"/>
        <div className="charts-bar-r">
          <span className="dim">{groups.length} deployed variants · {snap.openCount} open</span>
        </div>
      </div>

      {/* Strategy cards */}
      <div className="strat-cards">
        {groups.map(g => {
          const on = isOn(g.id);
          // Mini sparkline of cum curve
          const curve = g.cumCurve;
          let miniPath = null;
          if (curve.length >= 2) {
            const lo2 = Math.min(0, ...curve.map(c => c.pnl));
            const hi2 = Math.max(0, ...curve.map(c => c.pnl));
            const rng = hi2 - lo2 || 1;
            miniPath = curve.map((c, i) => {
              const t0 = curve[0].t;
              const tN = curve[curve.length - 1].t;
              const x = ((c.t - t0) / Math.max(1, tN - t0)) * 100;
              const y = 24 - ((c.pnl - lo2) / rng) * 24;
              return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');
          }
          return (
            <button key={g.id} className={`strat-card ${on ? 'on' : ''}`}
                    onClick={() => toggle(g.id)}
                    style={{'--sc': g.color}}>
              <div className="sc-head">
                <span className="sc-dot" style={{background: g.color}}/>
                <span className="sc-name">{g.name}</span>
                <span className={`sc-status sc-${g.status.toLowerCase()}`}>{g.status}</span>
              </div>
              <div className="sc-pnl" style={{color: g.pnl30d > 0 ? 'var(--pos)' : g.pnl30d < 0 ? 'var(--neg)' : 'var(--dim)'}}>
                {g.pnl30d >= 0 ? '+' : ''}${g.pnl30d.toFixed(0)}
                <span className="sc-pct"> {g.pnl30dPct >= 0 ? '+' : ''}{g.pnl30dPct.toFixed(2)}%</span>
              </div>
              <div className="sc-mini">
                <svg viewBox="0 0 100 24" className="sc-mini-svg">
                  {miniPath && <path d={miniPath} fill="none" stroke={g.color} strokeWidth="1.2"/>}
                </svg>
              </div>
              <div className="sc-kvs">
                <div><span className="dim">WR</span> <span>{g.wr.toFixed(0)}%</span></div>
                <div><span className="dim">Sharpe</span> <span>{g.sharpe.toFixed(2)}</span></div>
                <div><span className="dim">DD</span> <span className="neg">-{g.maxDDpct.toFixed(1)}%</span></div>
                <div><span className="dim">N</span> <span>{g.tradesCount}</span></div>
              </div>
            </button>
          );
        })}
      </div>

      {/* Overlaid equity curves */}
      <div style={{padding:'0 14px'}}>
        <div className="col-head" style={{padding:'10px 0'}}>
          <span className="lbl">equity curves · overlaid · 30d cumulative return</span>
        </div>
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          style={{width:'100%', height:'auto', background:'var(--panel-2)', cursor:'crosshair'}}
          onMouseMove={(e) => {
            if (!svgRef.current || shownGroups.length === 0) return;
            const rect = svgRef.current.getBoundingClientRect();
            const sx = ((e.clientX - rect.left) / rect.width) * W;
            if (sx < padL || sx > padL + iw) { setHover(null); return; }
            const span = Math.max(1, now - windowStart);
            const tAt = windowStart + ((sx - padL) / iw) * span;
            // For each visible group, interpolate pct at tAt.
            const snaps = shownGroups.map(g => {
              const c = g.cumCurve;
              if (c.length === 0) return null;
              // binary-ish search: find the point just before tAt
              let i = 0;
              while (i < c.length - 1 && c[i+1].t <= tAt) i++;
              const a = c[i], b = c[Math.min(i+1, c.length-1)];
              const frac = b.t > a.t ? (tAt - a.t) / (b.t - a.t) : 0;
              const pnl = a.pnl + (b.pnl - a.pnl) * Math.max(0, Math.min(1, frac));
              const pct = (pnl / startBal) * 100;
              return { g, pct, y: yv(pct) };
            }).filter(Boolean);
            setHover({ x: sx, tAt, snaps });
          }}
          onMouseLeave={() => setHover(null)}
        >
          {/* Y grid */}
          {Array.from({length: 5}, (_, i) => {
            const v = lo + (hi - lo) * (i / 4);
            return (
              <g key={i}>
                <line x1={padL} y1={yv(v)} x2={padL+iw} y2={yv(v)} stroke="#151c22" strokeDasharray="2,3"/>
                <text x={padL - 8} y={yv(v) + 3} textAnchor="end" fontSize="10" fill="var(--dim)">
                  {v >= 0 ? '+' : ''}{v.toFixed(1)}%
                </text>
              </g>
            );
          })}
          {/* Zero line */}
          <line x1={padL} y1={yv(0)} x2={padL+iw} y2={yv(0)} stroke="#3a4650" strokeDasharray="4,3"/>

          {/* X axis labels: d30, d20, d10, d1 */}
          {[30, 20, 10, 1].map((d, i) => {
            const t = now - d * 24 * 3600 * 1000;
            const x = xt(t);
            return (
              <g key={i}>
                <line x1={x} y1={padT+ih} x2={x} y2={padT+ih+3} stroke="#2a3339"/>
                <text x={x} y={padT+ih+16} textAnchor="middle" fontSize="10" fill="var(--dim)">
                  d{d}
                </text>
              </g>
            );
          })}

          {/* Curves */}
          {shownGroups.map(g => {
            if (g.cumCurve.length < 2) return null;
            const path = g.cumCurve.map((pt, i) => {
              const pct = (pt.pnl / startBal) * 100;
              return `${i === 0 ? 'M' : 'L'}${xt(pt.t).toFixed(1)},${yv(pct).toFixed(1)}`;
            }).join(' ');
            const last = g.cumCurve[g.cumCurve.length - 1];
            const lastPct = (last.pnl / startBal) * 100;
            const labelX = Math.min(xt(last.t) + 8, padL + iw - 20);
            return (
              <g key={g.id}>
                <path d={path} fill="none" stroke={g.color} strokeWidth="1.5"/>
                <circle cx={xt(last.t)} cy={yv(lastPct)} r="3" fill={g.color}/>
                <text x={labelX} y={yv(lastPct) + 3} fontSize="10" fill={g.color}>
                  {g.name.slice(0, 2)}
                </text>
              </g>
            );
          })}

          {/* Hover crosshair + per-strategy snapshots */}
          {hover && (
            <g>
              <line x1={hover.x} y1={padT} x2={hover.x} y2={padT + ih} stroke="#3a4650" strokeDasharray="3,3"/>
              {hover.snaps.map(({g, pct, y}) => (
                <circle key={g.id} cx={hover.x} cy={y} r="3" fill="#07090b" stroke={g.color} strokeWidth="1.5"/>
              ))}
              {(() => {
                const w = 150, rowH = 14;
                const boxH = 14 + rowH * (hover.snaps.length + 1);
                const tx = Math.min(hover.x + 10, padL + iw - w - 4);
                const ty = Math.max(padT + 4, padT + 4);
                const d = new Date(hover.tAt);
                const lbl = `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
                return (
                  <g transform={`translate(${tx}, ${ty})`}>
                    <rect width={w} height={boxH} fill="#0c1013" stroke="#2a3339"/>
                    <text x="8" y="12" fontSize="9" fill="var(--dim)">{lbl}</text>
                    {hover.snaps.map((s, i) => (
                      <g key={s.g.id} transform={`translate(8, ${26 + i * rowH})`}>
                        <circle cx="3" cy="-3" r="3" fill={s.g.color}/>
                        <text x="12" y="0" fontSize="10" fill="var(--text)">{s.g.name}</text>
                        <text x={w - 16} y="0" fontSize="10" textAnchor="end"
                              fill={s.pct >= 0 ? 'var(--pos)' : 'var(--neg)'}>
                          {s.pct >= 0 ? '+' : ''}{s.pct.toFixed(2)}%
                        </text>
                      </g>
                    ))}
                  </g>
                );
              })()}
            </g>
          )}
        </svg>
      </div>

      {/* Head-to-head table */}
      <div style={{padding:'20px 14px'}}>
        <div className="col-head" style={{padding:'8px 0'}}>
          <span className="lbl">head-to-head</span>
        </div>
        {groups.length === 0 ? (
          <div className="dim" style={{padding:'12px'}}>no deployed variants yet.</div>
        ) : (
          <div style={{display:'grid', gridTemplateColumns:`140px repeat(${groups.length}, 1fr)`, gap:'1px', background:'var(--hair)'}}>
            <div style={{background:'var(--panel)', padding:'8px 10px'}} className="dim small">&nbsp;</div>
            {groups.map(g => (
              <div key={g.id} style={{background:'var(--panel)', padding:'8px 10px'}}>
                <span className="sc-dot" style={{background: g.color, display:'inline-block', width:6, height:6, marginRight:6}}/>
                <span className="tk-sym-sm">{g.name}</span>
              </div>
            ))}
            {[
              ['30d P&L',   g => `${g.pnl30d >= 0 ? '+' : ''}$${g.pnl30d.toFixed(0)}`, g => g.pnl30d >= 0 ? 'pos' : 'neg'],
              ['30d %',     g => `${g.pnl30dPct >= 0 ? '+' : ''}${g.pnl30dPct.toFixed(2)}%`, g => g.pnl30dPct >= 0 ? 'pos' : 'neg'],
              ['trades',    g => `${g.tradesCount}`, () => ''],
              ['win rate',  g => `${g.wr.toFixed(1)}%`, () => ''],
              ['avg R',     g => (g.avgR > 0 ? `${g.avgR.toFixed(2)}R` : '—'), () => ''],
              ['sharpe',    g => g.sharpe.toFixed(2), () => ''],
              ['max DD',    g => `-${g.maxDDpct.toFixed(1)}%`, () => 'neg'],
              ['avg hold',  g => g.avgHold, () => ''],
              ['status',    g => g.status, g => g.status === 'LIVE' ? 'pos' : 'dim'],
              ['symbols',   g => g.symbols.slice(0, 4).join(', ') + (g.symbols.length > 4 ? '…' : ''), () => 'dim small'],
            ].map(([label, fn, clsFn]) => (
              <React.Fragment key={label}>
                <div style={{background:'var(--panel)', padding:'8px 10px'}} className="dim small">{label}</div>
                {groups.map(g => (
                  <div key={g.id} style={{background:'var(--panel)', padding:'8px 10px'}} className={clsFn(g)}>
                    {fn(g)}
                  </div>
                ))}
              </React.Fragment>
            ))}
          </div>
        )}
      </div>

      {fetchErr && (
        <div style={{position:'fixed', right:12, bottom:12, padding:'6px 10px',
                     background:'var(--panel)', border:'1px solid var(--neg)',
                     color:'var(--neg)', fontSize:10}}>api: {fetchErr}</div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<StrategiesApp/>);
