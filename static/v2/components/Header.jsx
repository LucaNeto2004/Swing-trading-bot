// Top ticker strip — horizontally scrolling prices
function TickerStrip({ tickers }) {
  return (
    <div className="ticker-strip">
      {tickers.map((t, i) => (
        <div key={i} className="tick">
          <span className="tick-sym">{t.sym}</span>
          <span className="tick-px">{fmt(t.px, t.px < 1 ? 6 : t.px < 100 ? 4 : 2)}</span>
          <span className={`tick-chg ${t.chg > 0 ? 'pos' : 'neg'}`}>
            {t.chg > 0 ? '+' : ''}{t.chg.toFixed(2)}%
          </span>
        </div>
      ))}
    </div>
  );
}

// Top bar — brand, env, session info
function TopBar({ live, onCmdK, onOrder, onHelp, page = 'terminal', snap }) {
  // Nav routes through Flask — preserves URL scheme (/v2, /v2/charts, ...).
  const NAV = [
    ['terminal',   'TERM',  '/v2',            ''],
    ['charts',     'CHRT',  '/v2/charts',     ''],
    ['signals',    'SIG',   '/v2/signals',    ''],
    ['journal',    'JRNL',  '/v2/journal',    'topbar-hide-nav1'],
    ['risk',       'RISK',  '/v2/risk',       'topbar-hide-nav1'],
    ['strategies', 'STRAT', '/v2/strategies', 'topbar-hide-nav2'],
  ];
  const [time, setTime] = useState(new Date());
  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const clock = time.toLocaleTimeString('en-US', { hour12: true });
  const modeChip = (snap?.mode || 'PAPER').toUpperCase();
  const modeClass = modeChip === 'LIVE' ? 'chip-pos' : 'chip-amber';
  const netChip = (snap?.network || 'TESTNET').toUpperCase();
  const effLev = snap?.effectiveLev ?? 0;
  const marginPctDisp = ((snap?.marginPct ?? 0) * 100).toFixed(0);
  const setLevDisp = snap?.setLev ?? 0;
  return (
    <div className="topbar">
      <div className="topbar-l">
        <BrandMark />
        <span className="brand-name">swing-bot</span>
        <span className="brand-ver topbar-hide-md">v2</span>
        <HR v />
        <span className="crumb topbar-hide-lg">paper / hyperliquid / whale-swing</span>
        <span className={`chip ${modeClass}`}>{modeChip}</span>
        <span className="chip chip-dim topbar-hide-md">{netChip}</span>
        <HR v className="topbar-hide-lg"/>
        <span className="meta-k topbar-hide-lg">eff.lev</span><span className="meta-v topbar-hide-lg">{effLev.toFixed(1)}×</span>
        <span className="meta-k topbar-hide-xl">margin</span><span className="meta-v topbar-hide-xl">{marginPctDisp}% / {setLevDisp}×</span>
        <span className="meta-k topbar-hide-xl">clock</span><span className="meta-v topbar-hide-xl">{clock}</span>
      </div>
      <div className="topbar-r">
        <div className="live-ind">
          <Pulse color={live ? 'var(--pos)' : 'var(--neg)'} />
          <span>{live ? 'LIVE' : 'PAUSED'}</span>
        </div>
        <HR v />
        <button className="btn-ghost" onClick={onCmdK}>⌘K ask</button>
        <div className="navpill">
          {NAV.map(([k, label, href, hide]) => {
            const cls = page === k ? '' : (hide || '');
            return (
              <a key={k} className={`navpill-i ${page === k ? 'on' : ''} ${cls}`} href={href}>{label}</a>
            );
          })}
        </div>
        {onOrder && <button className="btn-ghost topbar-hide-md" onClick={onOrder}>ORDER</button>}
        {onHelp && <button className="btn-ghost topbar-hide-lg" onClick={onHelp}>?</button>}
      </div>
    </div>
  );
}

Object.assign(window, { TickerStrip, TopBar });
