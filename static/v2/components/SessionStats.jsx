// Session stats below the sentiment card
function SessionStats({ range, setRange, ranges, stats }) {
  // Label suffix tracks the active range — "session" when 24h is selected,
  // otherwise "7d" / "30d" / "All". Same data underneath; just a clearer
  // label so users know the metrics reflect their chosen window.
  const rangeLabel = (() => {
    const r = String(range || '').toLowerCase();
    if (r === '24h' || r === '') return 'session';
    if (r === 'all') return 'all';
    return r;  // "7d", "30d"
  })();
  const sparkColor = stats.sessionPnl >= 0 ? 'var(--pos)' : 'var(--neg)';
  return (
    <div className="panel session-stats">
      <div className="range-row">
        {ranges.map(r => {
          const key = r.toLowerCase();
          return (
            <button
              key={r}
              onClick={() => setRange(key)}
              className={`range-btn ${range === key ? 'on' : ''}`}
            >
              {r}
            </button>
          );
        })}
      </div>
      <HR />
      <div className="stat-row">
        <div className="stat-k">PnL ({rangeLabel})</div>
        <div className="stat-v">
          <ColorNum value={stats.sessionPnlPct}>{fmtPct(stats.sessionPnlPct)}</ColorNum>
          <MiniSpark pts={stats.spark} color={sparkColor} />
          <ColorNum value={stats.sessionPnl}>{fmtUsd(stats.sessionPnl, 2, true)}</ColorNum>
        </div>
      </div>
      <div className="stat-row">
        <div className="stat-k">Volume ({rangeLabel})</div>
        <div className="stat-v"><span className="num">${fmt(stats.volume)}</span></div>
      </div>
      <div className="stat-row">
        <div className="stat-k">Win Rate</div>
        <div className="stat-v">
          <span className="num">{stats.winRate.toFixed(1)}%</span>
          <span className="dim">({stats.wins}/{stats.losses})</span>
        </div>
      </div>
      <div className="stat-row">
        <div className="stat-k">Profit Factor</div>
        <div className="stat-v"><span className="num">
          {stats.pfInfinite ? '∞' : (stats.profitFactor > 0 ? stats.profitFactor.toFixed(2) : '—')}
        </span></div>
      </div>
      <div className="stat-row">
        <div className="stat-k">Drawdown</div>
        <div className="stat-v">
          <span className="num">{stats.drawdown.toFixed(2)}%</span>
          <MiniBar val={stats.drawdown} max={20} />
        </div>
      </div>
      <div className="stat-row">
        <div className="stat-k">Sharpe (30d)</div>
        <div className="stat-v"><span className="num">{stats.sharpe.toFixed(2)}</span></div>
      </div>
      <div className="stat-row">
        <div className="stat-k">Avg Hold</div>
        <div className="stat-v"><span className="num">{stats.avgHold}</span></div>
      </div>
    </div>
  );
}

function MiniSpark({ pts, color = 'var(--pos)' }) {
  if (!pts || !pts.length) return null;
  const W = 56, H = 12;
  const min = Math.min(...pts), max = Math.max(...pts);
  const r = max - min || 1;
  const d = pts.map((p, i) => {
    const x = (i / (pts.length - 1)) * W;
    const y = H - ((p - min) / r) * H;
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <svg width={W} height={H} className="mini-spark">
      <path d={d} fill="none" stroke={color} strokeWidth="1"/>
    </svg>
  );
}

function MiniBar({ val, max = 100 }) {
  const pct = Math.min(1, val / max);
  return (
    <div className="mini-bar">
      <div className="mini-bar-fill" style={{ width: `${pct * 100}%` }}/>
    </div>
  );
}

Object.assign(window, { SessionStats, MiniSpark, MiniBar });
