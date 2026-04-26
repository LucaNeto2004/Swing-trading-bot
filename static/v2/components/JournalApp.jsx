// Journal page — closed trades from /api/state.trade_history. Layout matches
// the Claude design reference, but only renders data the bot actually stores.
//
// Columns shown as "—":
//   R multiple      — needs entry_price + initial_sl on TradeRecord (not
//                      currently saved; bot-side change in core/execution.py)
//   Entry price     — same
//   MAE             — not tracked (we only record favorable_excursion_atr as MFE)

function JournalApp() {
  const [filter, setFilter] = React.useState('all');      // all | wins | losses
  const [stratFilter, setStratFilter] = React.useState(null); // null or entry_type
  const [snap, setSnap] = React.useState(window.DATA.EMPTY_SNAPSHOT);
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

  // Symbol -> entry_type lookup so every row can show a strategy tag.
  const strategyBySym = React.useMemo(() => {
    const syms = Array.isArray(snap.raw?.symbols) ? snap.raw.symbols : [];
    return new Map(syms.map(s => [s.symbol, s.entry_type || 'whale-swing']));
  }, [snap]);

  const availableStrategies = React.useMemo(() => {
    const s = new Set();
    for (const t of (snap.raw?.trade_history || [])) {
      s.add(strategyBySym.get(t.symbol) || 'whale-swing');
    }
    return Array.from(s).sort();
  }, [snap, strategyBySym]);

  const trades = React.useMemo(() => {
    const th = Array.isArray(snap.raw?.trade_history) ? snap.raw.trade_history : [];
    const rows = [...th].sort((a, b) => {
      const ta = Date.parse(String(a.timestamp || '').replace(' ', 'T') + 'Z');
      const tb = Date.parse(String(b.timestamp || '').replace(' ', 'T') + 'Z');
      return (tb || 0) - (ta || 0);
    }).map((t, i) => {
      const pnl = Number(t.pnl) || 0;
      const heldBars = Number(t.held_bars) || 0;
      const holdMin = heldBars * 5;
      const hh = Math.floor(holdMin / 60);
      const mm = holdMin % 60;
      const hold = hh > 0 ? `${hh}h ${String(mm).padStart(2, '0')}m` : `${mm}m`;
      const closedTs = Date.parse(String(t.timestamp || '').replace(' ', 'T') + 'Z');
      const openedTs = Number.isFinite(closedTs) ? closedTs - holdMin * 60 * 1000 : null;
      const fmtTs = (ms) => {
        if (!ms) return '—';
        const d = new Date(ms);
        const mon = d.toLocaleString('en-US', { month: 'short' });
        return `${mon} ${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
      };
      // Prefer the per-leg `r` (populated on every exit) over the legacy
      // `runner_r` (only on runner exits). Fall back for old records.
      const rVal = (t.r != null) ? Number(t.r)
                 : (t.runner_r != null) ? Number(t.runner_r)
                 : null;
      return {
        id: `T-${String(i + 1).padStart(4, '0')}`,
        sym: t.symbol,
        side: String(t.side || '').toUpperCase(),
        openedStr: fmtTs(openedTs),
        closedStr: fmtTs(closedTs),
        hold,
        size: Number(t.size) || 0,
        notional: Number(t.notional) || 0,
        entry: t.entry_price == null ? null : Number(t.entry_price),
        exit: Number(t.price) || 0,
        pnl,
        r: rVal,
        mfe: t.favorable_excursion_atr == null ? null : Number(t.favorable_excursion_atr),
        mae: t.adverse_excursion_atr == null ? null : Number(t.adverse_excursion_atr),
        strategy: strategyBySym.get(t.symbol) || 'whale-swing',
        exit_reason: t.exit_reason || '',
      };
    });
    let out = rows;
    if (filter === 'wins') out = out.filter(t => t.pnl > 0);
    if (filter === 'losses') out = out.filter(t => t.pnl < 0);
    if (stratFilter) out = out.filter(t => t.strategy === stratFilter);
    return out;
  }, [snap, filter, stratFilter, strategyBySym]);

  const stats = React.useMemo(() => {
    const n = trades.length;
    const wins = trades.filter(t => t.pnl > 0);
    const losses = trades.filter(t => t.pnl < 0);
    const totalPnl = trades.reduce((a, t) => a + t.pnl, 0);
    const wr = (wins.length + losses.length) ? wins.length / (wins.length + losses.length) : 0;
    const gross = wins.reduce((a, t) => a + t.pnl, 0);
    const lossSum = Math.abs(losses.reduce((a, t) => a + t.pnl, 0));
    const pf = lossSum > 0 ? gross / lossSum : (gross > 0 ? Infinity : 0);
    const best = trades.reduce((a, t) => !a || t.pnl > a.pnl ? t : a, null);
    const worst = trades.reduce((a, t) => !a || t.pnl < a.pnl ? t : a, null);
    const rVals = trades.map(t => t.r).filter(r => r != null && Number.isFinite(r));
    const avgR = rVals.length ? rVals.reduce((a, b) => a + b, 0) / rVals.length : null;
    // PnL distribution (bucketed in $50 steps around 0)
    const dist = new Array(11).fill(0);
    const step = 50;
    for (const t of trades) {
      const bucket = Math.max(0, Math.min(10, 5 + Math.round(t.pnl / step)));
      dist[bucket]++;
    }
    return { n, wins: wins.length, losses: losses.length, totalPnl, wr, pf, best, worst, dist, step, avgR, rCount: rVals.length };
  }, [trades]);

  return (
    <div className="app theme-green">
      <TopBar live={true} snap={snap} page="journal" onCmdK={() => {}}/>

      <div className="charts-bar">
        <div className="charts-bar-l">
          <h1 className="charts-title">trade journal</h1>
          <span className="dim charts-sub">closed positions · last 30d</span>
        </div>
        <div className="charts-bar-c">
          <div className="seg">
            {[['all','all'],['wins','wins'],['losses','losses']].map(([k,l]) => (
              <button key={k} className={`seg-btn ${filter === k ? 'on' : ''}`} onClick={() => setFilter(k)}>{l}</button>
            ))}
          </div>
          {availableStrategies.length > 0 && (
            <div className="seg">
              {availableStrategies.map(s => (
                <button key={s}
                        className={`seg-btn ${stratFilter === s ? 'on' : ''}`}
                        onClick={() => setStratFilter(stratFilter === s ? null : s)}>
                  {s}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="charts-bar-r">
          <button className="btn-ghost" onClick={() => {
            // Minimal CSV export
            const headers = ['id','symbol','side','opened','closed','hold','size','exit','pnl','mfe','strategy','exit_reason'];
            const lines = [headers.join(',')];
            for (const t of trades) {
              lines.push([t.id, t.sym, t.side, t.openedStr, t.closedStr, t.hold, t.size, t.exit, t.pnl, t.mfe ?? '', t.strategy, t.exit_reason].join(','));
            }
            const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `journal_${Date.now()}.csv`;
            a.click();
          }}>export csv</button>
        </div>
      </div>

      <div className="kpi-strip">
        <div className="kpi"><span className="lbl">trades</span><span className="kpi-v">{stats.n}</span></div>
        <div className="kpi">
          <span className="lbl">net pnl</span>
          <span className={`kpi-v ${stats.totalPnl >= 0 ? 'pos' : 'neg'}`}>{stats.totalPnl >= 0 ? '+' : ''}${stats.totalPnl.toFixed(2)}</span>
        </div>
        <div className="kpi">
          <span className="lbl">win rate</span>
          <span className="kpi-v">{(stats.wr*100).toFixed(1)}%</span>
          <span className="kpi-bar"><span className="kpi-bar-f" style={{width: `${stats.wr*100}%`}}/></span>
        </div>
        <div className="kpi">
          <span className="lbl">avg R</span>
          <span className={`kpi-v ${stats.avgR == null ? 'dim' : stats.avgR >= 0 ? 'pos' : 'neg'}`}>
            {stats.avgR == null ? '—' : `${stats.avgR >= 0 ? '+' : ''}${stats.avgR.toFixed(2)}R`}
          </span>
          <span className="dim small">{stats.rCount} of {stats.n} trades</span>
        </div>
        <div className="kpi">
          <span className="lbl">profit factor</span>
          <span className="kpi-v">{Number.isFinite(stats.pf) ? stats.pf.toFixed(2) : (stats.pf > 0 ? '∞' : '—')}</span>
        </div>
        <div className="kpi">
          <span className="lbl">best</span>
          <span className="kpi-v pos">{stats.best ? `+$${stats.best.pnl.toFixed(2)}` : '—'} <span className="dim small">{stats.best?.sym || ''}</span></span>
        </div>
        <div className="kpi">
          <span className="lbl">worst</span>
          <span className="kpi-v neg">{stats.worst ? `-$${Math.abs(stats.worst.pnl).toFixed(2)}` : '—'} <span className="dim small">{stats.worst?.sym || ''}</span></span>
        </div>
      </div>

      {/* PnL distribution (substitute for R-distribution — same visual shape) */}
      <div className="r-dist">
        <span className="lbl">pnl distribution <span className="dim small">($ buckets)</span></span>
        <div className="r-bins">
          {stats.dist.map((count, i) => {
            const mid = (i - 5) * stats.step;
            const h = Math.max(2, count * 10);
            return (
              <div key={i} className="r-bin">
                <div className={`r-bar ${mid < 0 ? 'neg' : 'pos'}`} style={{height: h}}/>
                <div className="r-lbl">{mid > 0 ? '+' : ''}{mid}</div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="journal-tbl">
        <div className="jtbl-head" style={{gridTemplateColumns:'80px 80px 70px 220px 90px 90px 160px 70px 100px 100px 140px'}}>
          <span>id</span>
          <span>symbol</span>
          <span>side</span>
          <span>opened → closed</span>
          <span>hold</span>
          <span className="ta-r">size</span>
          <span className="ta-r">entry → exit</span>
          <span className="ta-r">R</span>
          <span className="ta-r">P&L</span>
          <span>MAE / MFE</span>
          <span>strategy</span>
        </div>
        {trades.length === 0 && (
          <div className="dim" style={{padding:'20px'}}>no closed trades match this filter.</div>
        )}
        {trades.map(t => (
          <div key={t.id} className="jtbl-row" style={{gridTemplateColumns:'80px 80px 70px 220px 90px 90px 160px 70px 100px 100px 140px'}}>
            <span className="dim mono-sm">{t.id}</span>
            <span className="tk-sym">{t.sym}</span>
            <span className={`tag ${t.side === 'LONG' ? 'tag-pos' : 'tag-neg'}`}>{t.side}</span>
            <span className="dim jt-times">{t.openedStr} <span className="dim-2">→</span> {t.closedStr}</span>
            <span className="dim">{t.hold}</span>
            <span className="ta-r num">${fmt(t.size)}</span>
            <span className="ta-r num jt-px">{t.entry == null ? <span className="dim">—</span> : fmt(t.entry, 4)} <span className="dim">→</span> {fmt(t.exit, 4)}</span>
            <span className={`ta-r num ${t.r == null ? 'dim' : t.r >= 0 ? 'pos' : 'neg'}`}>
              {t.r == null ? '—' : `${t.r >= 0 ? '+' : ''}${t.r.toFixed(2)}R`}
            </span>
            <span className={`ta-r num ${t.pnl >= 0 ? 'pos' : 'neg'}`}>{t.pnl >= 0 ? '+' : '−'}${Math.abs(t.pnl).toFixed(2)}</span>
            <span className="mae-mfe">
              <span className="dim small">{t.mae != null ? t.mae.toFixed(2) : '—'} / {t.mfe != null ? t.mfe.toFixed(2) : '—'}</span>
            </span>
            <span className="strat-cell">
              <span className="strat-dot" style={{background:'var(--pos)'}}/>
              <span className="dim">{t.strategy}</span>
              {t.exit_reason && <span className="tag tag-dim small">{t.exit_reason}</span>}
            </span>
          </div>
        ))}
      </div>

      {fetchErr && (
        <div style={{position:'fixed', right:12, bottom:12, padding:'6px 10px',
                     background:'var(--panel)', border:'1px solid var(--neg)',
                     color:'var(--neg)', fontSize:10}}>api: {fetchErr}</div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<JournalApp/>);
