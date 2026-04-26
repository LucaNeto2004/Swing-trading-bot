// Correlation matrix for Risk tab
function CorrelationMatrix() {
  const syms = ['BTC', 'ETH', 'SOL', 'ZEC', 'HYPE', 'INJ'];
  // Deterministic correlations
  const corr = (a, b) => {
    if (a === b) return 1;
    const h = (a.charCodeAt(0) * 13 + b.charCodeAt(0) * 7 + a.length * b.length) % 100;
    return (h / 100) * 1.6 - 0.6;
  };
  const color = (v) => {
    if (v > 0.6) return 'rgba(74,222,128,0.55)';
    if (v > 0.3) return 'rgba(74,222,128,0.28)';
    if (v > 0) return 'rgba(74,222,128,0.12)';
    if (v > -0.3) return 'rgba(248,113,113,0.12)';
    if (v > -0.6) return 'rgba(248,113,113,0.28)';
    return 'rgba(248,113,113,0.55)';
  };
  return (
    <div className="corr">
      <Label>correlation (30d returns)</Label>
      <div className="corr-grid" style={{ gridTemplateColumns: `60px repeat(${syms.length}, 1fr)` }}>
        <div className="corr-cell head"/>
        {syms.map(s => <div key={s} className="corr-cell head">{s}</div>)}
        {syms.map(r => (
          <React.Fragment key={r}>
            <div className="corr-cell head">{r}</div>
            {syms.map(c => {
              const v = corr(r, c);
              return (
                <div key={c} className="corr-cell" style={{ background: color(v) }}>
                  <span className={v > 0 ? 'pos' : v < 0 ? 'neg' : 'dim'}>
                    {v === 1 ? '1.00' : v.toFixed(2)}
                  </span>
                </div>
              );
            })}
          </React.Fragment>
        ))}
      </div>
      <div className="corr-legend">
        <span className="dim">-1</span>
        <div className="corr-scale"/>
        <span className="dim">+1</span>
      </div>
    </div>
  );
}

Object.assign(window, { CorrelationMatrix });
