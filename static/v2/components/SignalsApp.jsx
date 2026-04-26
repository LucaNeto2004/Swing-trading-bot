// Signals page — live-updating feed per the spec. Pulls /api/signals (filled
// from logs/signals.jsonl by main.py's cycle loop).
//
// Status values: watch | filled | primed | skip
//   watch  = scan trigger fired (bb_touch / ema_cross / vol_spike / etc) but
//            not a tradeable signal — telemetry only. Sparse, edge-only.
//   filled = order was placed (initial open or pyramid scale)
//   primed = pending (reserved for future gate)
//   skip   = signal fired but filtered (risk / BTC-confirm / already open)

function absTime(tsIso) {
  if (!tsIso) return '—';
  // Accept ISO with Z, ±HH:MM, or naive (assume UTC).
  let s = String(tsIso);
  const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(s);
  if (!hasTz) s = s + 'Z';
  const t = Date.parse(s);
  if (Number.isNaN(t)) return '—';
  const dt = new Date(t);
  return `${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}:${String(dt.getSeconds()).padStart(2,'0')}`;
}

function SignalsApp() {
  const [snap, setSnap] = React.useState(window.DATA.EMPTY_SNAPSHOT);
  const [events, setEvents] = React.useState([]);
  const [filter, setFilter] = React.useState('all');
  const [paused, setPaused] = React.useState(false);
  const [expanded, setExpanded] = React.useState(null);
  const [fetchErr, setFetchErr] = React.useState(null);
  const prevTopIdRef = React.useRef(null);
  const [flashId, setFlashId] = React.useState(null);

  React.useEffect(() => { document.body.className = 'theme-green density-default'; }, []);

  React.useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (paused) return;
      try {
        const [snapRaw, sigR] = await Promise.all([
          window.DATA.fetchAccountState(),
          fetch('/api/signals', { cache: 'no-store' }).then(r => r.json()),
        ]);
        if (cancelled) return;
        setSnap(window.DATA.mapSnapshot(snapRaw, 0));
        const evs = sigR.events || [];
        // Flash new top row
        const topId = evs[0]?.id || null;
        if (topId && prevTopIdRef.current && topId !== prevTopIdRef.current) {
          setFlashId(topId);
          setTimeout(() => setFlashId(cur => cur === topId ? null : cur), 1000);
        }
        prevTopIdRef.current = topId;
        setEvents(evs);
        setFetchErr(null);
      } catch (e) { if (!cancelled) setFetchErr(String(e.message || e)); }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, [paused]);

  const rows = React.useMemo(() => {
    const out = events;
    if (filter === 'filled') return out.filter(e => e.status === 'filled');
    if (filter === 'primed') return out.filter(e => e.status === 'primed');
    if (filter === 'skip')   return out.filter(e => e.status === 'skip');
    if (filter === 'watch')  return out.filter(e => e.status === 'watch');
    return out;
  }, [events, filter]);

  const counts = React.useMemo(() => ({
    all:    events.length,
    filled: events.filter(e => e.status === 'filled').length,
    primed: events.filter(e => e.status === 'primed').length,
    watch:  events.filter(e => e.status === 'watch').length,
    skip:   events.filter(e => e.status === 'skip').length,
  }), [events]);

  return (
    <div className="app theme-green">
      <TopBar live={!paused} snap={snap} page="signals" onCmdK={() => {}}/>

      <div className="charts-bar">
        <div className="charts-bar-l">
          <h1 className="charts-title">signals feed</h1>
          <span className="dim charts-sub">ema/bb/rsi/vol · live stream · {events.length} events</span>
        </div>
        <div className="charts-bar-c">
          <div className="seg">
            {[
              ['all',    `all ${counts.all}`],
              ['filled', `filled ${counts.filled}`],
              ['primed', `primed ${counts.primed}`],
              ['watch',  `watch ${counts.watch}`],
              ['skip',   `skip ${counts.skip}`],
            ].map(([k, l]) => (
              <button key={k} className={`seg-btn ${filter === k ? 'on' : ''}`} onClick={() => setFilter(k)}>{l}</button>
            ))}
          </div>
          <button className="btn-ghost" onClick={() => setPaused(p => !p)}>
            {paused ? '▶ resume' : '❚❚ pause'}
          </button>
        </div>
        <div className="charts-bar-r">
          <span className="dim small">skip ratio {events.length ? ((counts.skip / events.length) * 100).toFixed(0) : 0}%</span>
        </div>
      </div>

      <div className="sig-feed" style={{padding:'4px 0'}}>
        {rows.length === 0 && (
          <div className="dim" style={{padding:'24px'}}>
            {events.length === 0
              ? 'no events yet — scan triggers fire on 5m bar close. First events should appear within a few minutes of bot start.'
              : 'no events match this filter.'}
          </div>
        )}
        {rows.map(e => {
          const isUp = e.side === 'LONG';
          const conf = Number(e.confidence) || 0;
          const isFilled = e.status === 'filled';
          const isPrimed = e.status === 'primed';
          const isWatch  = e.status === 'watch';
          const isSkip   = e.status === 'skip';
          const statusCol = isFilled ? 'var(--pos)'
                          : isPrimed ? 'var(--warn)'
                          : isWatch  ? 'transparent'
                          : 'var(--dim-2)';
          const actionCol = isFilled ? 'var(--pos)'
                          : isPrimed ? 'var(--warn)'
                          : isWatch  ? 'var(--dim)'
                          : 'var(--dim-2)';
          const tagCls = isFilled ? 'tag-pos'
                       : isPrimed ? 'tag-warn'
                       : 'tag-dim';
          const tagTxt = isFilled ? '✓ filled'
                       : isPrimed ? '○ primed'
                       : isWatch  ? '· watch'
                       : '— skip';
          const actionTxt = e.action
                       || (isFilled ? 'OPEN' : isPrimed ? 'PRIMED' : isWatch ? 'WATCH' : 'SKIP');
          const pxDecimals = (e.entry < 0.01) ? 6
                           : (e.entry < 1)    ? 4
                           : (e.entry < 100)  ? 3
                           : 2;
          const open = expanded === e.id;
          const flashing = flashId === e.id;
          return (
            <div key={e.id || e.ts}
                 className="sig-row"
                 onClick={() => setExpanded(open ? null : e.id)}
                 style={{
                   display:'grid',
                   gridTemplateColumns:'90px 70px 28px 120px 110px 80px 160px 1fr 90px',
                   alignItems:'center',
                   gap:'10px',
                   borderBottom:'1px dotted var(--hair)',
                   padding:'7px 12px',
                   cursor:'pointer',
                   background: flashing
                     ? (isUp ? 'rgba(74,222,128,0.08)' : 'rgba(248,113,113,0.08)')
                     : 'transparent',
                   borderLeft: `3px solid ${statusCol}`,
                   transition:'background 400ms ease',
                 }}>
              <span className="dim mono-sm">{absTime(e.ts)}</span>
              <span className="tk-sym">{e.symbol}</span>
              <span style={{color: isUp ? 'var(--pos)' : 'var(--neg)', fontSize:12, textAlign:'center'}}>
                {isUp ? '▲' : '▼'}
              </span>
              <span className="dim small">{e.entry_type || '—'}</span>
              <span className="dim-2 small">px <span className="num" style={{color:'var(--fg)'}}>{fmt(e.entry, pxDecimals)}</span></span>
              <span className="dim-2 small">rsi <span className="num" style={{color:'var(--fg)'}}>{(Number(e.rsi) || 0).toFixed(1)}</span></span>
              <span>
                <span style={{display:'inline-block', width:90, height:4, background:'#1a2028',
                              position:'relative', top:-1, marginRight:8, verticalAlign:'middle'}}>
                  <span style={{display:'inline-block', width:`${Math.round(conf*100)}%`, height:'100%',
                                background: conf >= 0.6 ? 'var(--pos)' : conf >= 0.4 ? 'var(--warn)' : 'var(--dim-2)'}}/>
                </span>
                <span className="num dim">{Math.round(conf*100)}</span>
              </span>
              <span style={{color: actionCol, fontWeight: isFilled || isPrimed ? 600 : 400, fontSize:12}}>
                {actionTxt}
                {e.strategy_distance && (
                  <span className="dim-2" style={{fontWeight:400, marginLeft:6, fontSize:11}}>
                    · {e.strategy_distance}
                  </span>
                )}
              </span>
              <span style={{textAlign:'right'}}>
                <span className={`tag ${tagCls}`}>{tagTxt}</span>
              </span>
              {open && (() => {
                const eU = Number(e.ens_up || 0);
                const eD = Number(e.ens_dn || 0);
                const matchCnt = isUp ? eU : eD;
                const oppCnt   = isUp ? eD : eU;
                return (
                <div style={{gridColumn:'1 / -1', padding:'8px 0 4px 4px', color:'var(--dim)'}}>
                  <div className="small" style={{marginBottom:4}}>
                    <span style={{color:actionCol}}>{actionTxt}</span>
                    {e.regime && <span className="dim-2"> · regime {e.regime}</span>}
                    {(e.ens_up != null || e.ens_dn != null) && (
                      <span className="dim-2">
                        {' · ens '}
                        <span style={{color: matchCnt >= 4 ? 'var(--pos)' : 'var(--dim)'}}>{matchCnt}</span>
                        <span>/5 match</span>
                        {oppCnt > 0 && <span style={{color:'var(--dim-2)'}}> · {oppCnt} oppose</span>}
                        <span className="dim-2"> ({eU}↑ {eD}↓)</span>
                      </span>
                    )}
                    {e.rr != null && <span className="dim-2"> · RR {e.rr}</span>}
                  </div>
                  {(e.reasoning || []).map((r, i) => (
                    <div key={i} className="small" style={{padding:'2px 0'}}>
                      <span style={{color:'var(--dim-2)'}}>▸</span> {r}
                    </div>
                  ))}
                </div>
                );
              })()}
            </div>
          );
        })}
      </div>

      {fetchErr && (
        <div style={{position:'fixed', right:12, bottom:12, padding:'6px 10px',
                     background:'var(--panel)', border:'1px solid var(--neg)',
                     color:'var(--neg)', fontSize:10}}>api: {fetchErr}</div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<SignalsApp/>);
