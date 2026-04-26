// Order entry drawer
const { useState: useStateO } = React;

function OrderDrawer({ open, setOpen, onSubmit }) {
  const [side, setSide] = useStateO('LONG');
  const [type, setType] = useStateO('MARKET');
  const [symbol, setSymbol] = useStateO('BTC');
  const [size, setSize] = useStateO('0.10');
  const [price, setPrice] = useStateO('');
  const [lev, setLev] = useStateO(2);
  const [tp, setTp] = useStateO('');
  const [sl, setSl] = useStateO('');
  const [submitted, setSubmitted] = useStateO(null);

  if (!open) return null;

  const submit = () => {
    const order = { side, type, symbol, size, price: type === 'LIMIT' ? price : 'mkt', lev, tp, sl, t: new Date().toLocaleTimeString() };
    setSubmitted(order);
    onSubmit?.(order);
    setTimeout(() => setSubmitted(null), 2200);
  };

  return (
    <div className="drawer">
      <div className="drawer-head">
        <span className="lbl">order entry</span>
        <button className="btn-ghost" onClick={() => setOpen(false)}>ESC</button>
      </div>

      <div className="drawer-tabs">
        <button className={`dtab ${side === 'LONG' ? 'on pos' : ''}`} onClick={() => setSide('LONG')}>LONG</button>
        <button className={`dtab ${side === 'SHORT' ? 'on neg' : ''}`} onClick={() => setSide('SHORT')}>SHORT</button>
      </div>
      <div className="drawer-seg">
        {['MARKET', 'LIMIT', 'STOP'].map(t => (
          <button key={t} className={`seg-btn ${type === t ? 'on' : ''}`} onClick={() => setType(t)}>{t}</button>
        ))}
      </div>

      <div className="form-row">
        <label className="lbl">symbol</label>
        <select className="input" value={symbol} onChange={e => setSymbol(e.target.value)}>
          {window.DATA.TICKERS.map(t => <option key={t.sym} value={t.sym}>{t.sym}</option>)}
        </select>
      </div>

      <div className="form-row">
        <label className="lbl">size</label>
        <input className="input" value={size} onChange={e => setSize(e.target.value)} />
      </div>

      {type === 'LIMIT' && (
        <div className="form-row">
          <label className="lbl">limit price</label>
          <input className="input" value={price} onChange={e => setPrice(e.target.value)} placeholder="—"/>
        </div>
      )}

      <div className="form-row">
        <label className="lbl">leverage {lev.toFixed(1)}×</label>
        <input type="range" min="1" max="10" step="0.5" value={lev}
          onChange={e => setLev(parseFloat(e.target.value))} className="slider"/>
      </div>

      <div className="lev-ticks">
        {[1,2,3,4,5,6,7,8,9,10].map(n => (
          <div key={n} className={`lev-tick ${n <= lev ? 'on' : ''} ${n >= 7 ? 'danger' : n >= 4 ? 'warn' : ''}`}/>
        ))}
      </div>

      <div className="form-row">
        <label className="lbl">tp</label>
        <input className="input" value={tp} onChange={e => setTp(e.target.value)} placeholder="—"/>
      </div>
      <div className="form-row">
        <label className="lbl">sl</label>
        <input className="input" value={sl} onChange={e => setSl(e.target.value)} placeholder="—"/>
      </div>

      <div className="drawer-summary">
        <div className="kv"><span className="k">notional</span><span className="v num">≈ ${(parseFloat(size) * 78000 * lev).toFixed(0)}</span></div>
        <div className="kv"><span className="k">margin req</span><span className="v num">${(parseFloat(size) * 78000).toFixed(0)}</span></div>
        <div className="kv"><span className="k">est. fee</span><span className="v num dim">$0.54</span></div>
      </div>

      <button className={`submit ${side === 'LONG' ? 'pos' : 'neg'}`} onClick={submit}>
        {side === 'LONG' ? '▲' : '▼'} {type} {side} {symbol}
      </button>

      {submitted && (
        <div className="submit-toast">
          ✓ queued · {submitted.type} {submitted.side} {submitted.size} {submitted.symbol}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { OrderDrawer });
