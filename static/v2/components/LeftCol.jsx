// Equity card — top-left total equity display
function EquityCard({ equity, dayChange, dayChangePct, inUse, unrealized, realizedDay }) {
  const [mode, setMode] = React.useState('PERPS');
  const available = Math.max(0, (equity || 0) - (inUse || 0));
  return (
    <div className="panel equity-card">
      <Label>total equity</Label>
      <div className="equity-val">
        ${fmt(equity, 2)}
      </div>
      <div className="equity-sub">
        <ColorNum value={dayChange}>{fmtUsd(dayChange, 2, true)}</ColorNum>
        <span className="dim">/</span>
        <ColorNum value={dayChangePct}>{fmtPct(dayChangePct, 2)}</ColorNum>
        <span className="dim">total</span>
      </div>
      <HR />
      <div className="kv-grid">
        <div className="kv"><span className="k">available</span><span className="v">${fmt(available)}</span></div>
        <div className="kv"><span className="k">in-use</span><span className="v">${fmt(inUse || 0)}</span></div>
        <div className="kv"><span className="k">unrealized</span>
          <ColorNum value={unrealized || 0}>{fmtUsd(unrealized || 0, 2, true)}</ColorNum></div>
        <div className="kv"><span className="k">realized (d)</span>
          <ColorNum value={realizedDay || 0}>{fmtUsd(realizedDay || 0, 2, true)}</ColorNum></div>
      </div>
      <HR />
      <div className="mode-row">
        <span className="lbl">mode</span>
        <div className="seg">
          {['PERPS','SPOT','VAULT'].map(m => (
            <button key={m} className={`seg-btn ${mode === m ? 'on' : ''}`} onClick={() => setMode(m)}>{m}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

// Sentiment / leverage card — two-column
function SentLevCard({ sentiment = 'Very Bullish', score = 0.82, leverage = 2.85 }) {
  // Gauge arc
  const angle = Math.min(1, leverage / 5) * 180; // 0..5× maps to 0..180°
  const R = 42;
  const cx = 50, cy = 50;
  const rad = (a) => (a - 180) * Math.PI / 180;
  const p0 = { x: cx + R * Math.cos(rad(0)), y: cy + R * Math.sin(rad(0)) };
  const p1 = { x: cx + R * Math.cos(rad(angle)), y: cy + R * Math.sin(rad(angle)) };
  const large = angle > 180 ? 1 : 0;

  // Arrow direction + color keyed off the bias label.
  // Bullish → up/green, Bearish → down/red, Neutral/Flat → dash/amber.
  const bull = /Bullish|Lean Long/.test(sentiment);
  const bear = /Bearish|Lean Short/.test(sentiment);
  const arrowColor = bull ? 'var(--pos)' : bear ? 'var(--neg)' : 'var(--warn)';
  const arrowPath = bull
    ? 'M8 28 L20 12 L32 28 M20 12 L20 32'   // up arrow with down stem
    : bear
      ? 'M8 12 L20 28 L32 12 M20 28 L20 8'  // down arrow with up stem
      : 'M6 20 L34 20';                      // flat dash

  // Strength score (0..100) — distance from neutral. Very Bullish/Bearish
  // both score high; a balanced book scores near 0. This is what the bar
  // and the numeric label represent.
  const strength = Math.round(Math.abs(score - 0.5) * 200);

  return (
    <div className="panel sentlev-card">
      <div className="sentlev-inner">
        <div className="sent">
          <Label>position bias</Label>
          <div className="sent-arrow" style={{color: arrowColor}}>
            <svg viewBox="0 0 40 40" width="36" height="36">
              <path d={arrowPath} stroke={arrowColor} strokeWidth="2.5" fill="none" strokeLinecap="square"/>
            </svg>
          </div>
          <div className="sent-label" style={{color: arrowColor}}>{sentiment}</div>
          <div className="sent-score">
            <div className="sent-bar"><div className="sent-fill" style={{ width: `${strength}%`, background: arrowColor }}/></div>
            <span className="sent-pct" style={{color: arrowColor}}>{strength}</span>
          </div>
        </div>
        <HR v />
        <div className="lev">
          <Label>leverage</Label>
          <div className="gauge">
            <svg viewBox="0 0 100 58" width="100%">
              {/* backdrop arc */}
              <path d="M8 50 A42 42 0 0 1 92 50" fill="none" stroke="#1a2028" strokeWidth="6" strokeLinecap="square"/>
              {/* color segments */}
              <path d="M8 50 A42 42 0 0 1 39 11" fill="none" stroke="var(--pos)" strokeWidth="6" strokeLinecap="square"/>
              <path d="M39 11 A42 42 0 0 1 61 11" fill="none" stroke="var(--warn)" strokeWidth="6" strokeLinecap="square"/>
              <path d="M61 11 A42 42 0 0 1 92 50" fill="none" stroke="var(--neg)" strokeWidth="6" strokeLinecap="square"/>
              {/* needle */}
              <line
                x1="50" y1="50"
                x2={50 + 36 * Math.cos(rad(angle))}
                y2={50 + 36 * Math.sin(rad(angle))}
                stroke="#d7e0d9" strokeWidth="2" strokeLinecap="square"
              />
              <circle cx="50" cy="50" r="3" fill="#d7e0d9"/>
            </svg>
          </div>
          <div className="lev-val">{leverage.toFixed(2)}<span className="lev-x">×</span></div>
          <div className="lev-scale">
            <span>0</span><span>2.5</span><span>5</span>
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { EquityCard, SentLevCard });
