// Charts grid page — symbols come from the bot's live /api/state. Each tile
// is a real HL candle stream (see hlLive.jsx); no mock symbols, no mock meta.
function ChartsApp() {
  const defaults = window.__tweakDefaults || {};
  const [theme] = React.useState(defaults.theme || 'green');
  const [live, setLive] = React.useState(true);
  const [timeframe, setTimeframe] = React.useState('5m');
  const [layout, setLayout] = React.useState('3x2');
  const [focus, setFocus] = React.useState(null);
  const [snap, setSnap] = React.useState(window.DATA.EMPTY_SNAPSHOT);
  const [fetchErr, setFetchErr] = React.useState(null);

  React.useEffect(() => {
    document.body.className = `theme-${theme} density-default layout-${layout}`;
  }, [theme, layout]);

  React.useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') setFocus(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Poll /api/state so the OPEN/IDLE status and live prices stay current.
  React.useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const raw = await window.DATA.fetchAccountState();
        if (cancelled) return;
        setSnap(window.DATA.mapSnapshot(raw, 1));
        setFetchErr(null);
      } catch (e) { if (!cancelled) setFetchErr(String(e.message || e)); }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Build the symbol meta list: deployed symbols with a flag for OPEN/IDLE
  // and live price. `sym` is passed straight to the HL candle API; only
  // symbols HL actually lists can render tiles, so we filter xyz:* out.
  const symsMeta = React.useMemo(() => {
    const deployed = Array.isArray(snap.raw?.symbols) ? snap.raw.symbols : [];
    const prices = snap.raw?.prices || {};
    const posBySym = new Map(snap.positions.map(p => [p.token, p]));
    const items = deployed
      .map(s => s.symbol)
      .filter(s => typeof s === 'string' && !s.startsWith('xyz:'))
      .map(sym => {
        const p = posBySym.get(sym);
        return {
          sym,
          px: Number(prices[sym]) || (p?.live ?? 0),
          status: p ? 'OPEN' : 'IDLE',
          side: p?.side || null,
          entry: p?.avgEntry ?? null,
          sl: p?.sl ?? null,
          tp1: p?.tp1 ?? null,
          tp2: p?.tp2 ?? null,
          tp3: p?.tp3 ?? null,
          tp1_hit: p?.tp1_hit ?? false,
          tp2_hit: p?.tp2_hit ?? false,
          tp3_hit: p?.tp3_hit ?? false,
        };
      });
    return items;
  }, [snap]);

  const openCount = symsMeta.filter(s => s.status === 'OPEN').length;
  const focusMeta = focus ? symsMeta.find(s => s.sym === focus) : null;

  return (
    <div className={`app theme-${theme}`}>
      <TopBar live={live} snap={snap} page="charts" onCmdK={() => {}}/>

      <div className="charts-bar">
        <div className="charts-bar-l">
          <h1 className="charts-title">live charts</h1>
          <span className="dim charts-sub">{symsMeta.length} symbols · EMA 9/21/50 · BB(20,2) · RSI(14)</span>
        </div>
        <div className="charts-bar-c">
          <div className="seg">
            {['1m','5m','15m','1h','4h','1d'].map(tf => (
              <button key={tf} className={`seg-btn ${timeframe === tf ? 'on' : ''}`} onClick={() => setTimeframe(tf)}>{tf}</button>
            ))}
          </div>
          <div className="seg">
            {['3x2','2x3','4x2','4x3'].map(l => (
              <button key={l} className={`seg-btn ${layout === l ? 'on' : ''}`} onClick={() => setLayout(l)}>{l.replace('x','×')}</button>
            ))}
          </div>
          <button className="btn-ghost" onClick={() => setLive(l => !l)}>{live ? '❚❚' : '▶'}</button>
        </div>
        <div className="charts-bar-r">
          <span className={`chip ${openCount > 0 ? 'chip-pos' : 'chip-dim'}`}>{openCount}/{symsMeta.length} open</span>
          <span className="pos small-dot">● live · ws</span>
        </div>
      </div>

      {symsMeta.length === 0 ? (
        <div style={{padding:'60px 24px', textAlign:'center'}} className="dim">
          waiting for symbol list from /api/state…
        </div>
      ) : (
        <div className="charts-grid">
          {symsMeta.map(meta => (
            <ChartTile key={meta.sym} meta={meta} onExpand={() => setFocus(meta.sym)} interval={timeframe}/>
          ))}
        </div>
      )}

      {focusMeta && (
        <div className="chart-fs" onClick={(e) => { if (e.target.classList.contains('chart-fs')) setFocus(null); }}>
          <div className="chart-fs-inner">
            <div className="chart-fs-bar">
              <span className="dim">fullscreen · scroll=zoom · drag=pan · dbl-click=reset · esc to close</span>
              <div className="tab-spacer"/>
              <button className="btn-ghost" onClick={() => setFocus(null)}>✕ close</button>
            </div>
            <ChartTile meta={focusMeta} big interval={timeframe}/>
          </div>
        </div>
      )}

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

ReactDOM.createRoot(document.getElementById('root')).render(<ChartsApp/>);
