// Lower panel: tabs + positions/fills/symbols/attribution/vault/risk
function LowerPanel({ tab, setTab, positions, fills, symbolsAttrib, newFillFlash }) {
  const tabs = [
    { id: 'perps',  label: 'Perps',      count: positions.length },
    { id: 'fills',  label: 'Fills',      count: 36 },
    { id: 'symbols',label: 'Symbols',    count: 13 },
    { id: 'attrib', label: 'Attribution',count: null },
    { id: 'vault',  label: 'Vault',      count: null },
    { id: 'risk',   label: 'Risk',       count: null },
  ];

  const longVal = positions.filter(p => p.side === 'LONG').reduce((a,p)=>a+p.notional,0);
  const shortVal = positions.filter(p => p.side === 'SHORT').reduce((a,p)=>a+p.notional,0);
  const totalVal = longVal + shortVal;
  const sumPnl = positions.reduce((a,p)=>a+p.pnl,0);

  return (
    <div className="panel lower">
      <div className="tabs">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`tab ${tab === t.id ? 'on' : ''}`}
          >
            {t.label}
            {t.count !== null && <span className="tab-ct">({t.count})</span>}
          </button>
        ))}
        <div className="tab-spacer"/>
        <div className="tab-stats">
          <span className="ts-k">Long Value:</span><span className="ts-v">${fmt(longVal)}</span>
          <span className="ts-sep">·</span>
          <span className="ts-k">Short Value:</span><span className="ts-v">${fmt(shortVal)}</span>
          <span className="ts-sep">·</span>
          <span className="ts-k">Total</span><span className="ts-v">${fmt(totalVal)}</span>
          <span className="ts-sep">·</span>
          <span className="ts-k">Open:</span><span className="ts-v">{positions.length}/4</span>
          <span className="ts-sep">·</span>
          <span className="ts-k">Sum PnL:</span>
          <span className={`ts-v ${sumPnl >= 0 ? 'pos' : 'neg'}`}>
            {sumPnl >= 0 ? '+' : '−'}${fmt(Math.abs(sumPnl))}
          </span>
        </div>
      </div>

      {tab === 'perps' && <PositionsTable positions={positions}/>}
      {tab === 'fills' && <FillsTable fills={fills} flashKey={newFillFlash}/>}
      {tab === 'symbols' && <SymbolsTable symbols={symbolsAttrib}/>}
      {tab === 'attrib' && <AttributionView symbols={symbolsAttrib}/>}
      {tab === 'vault' && <VaultView/>}
      {tab === 'risk' && <RiskView positions={positions}/>}
    </div>
  );
}

function PositionsTable({ positions }) {
  // Liq price: prefer the server's computed value (dashboard.py uses
  // HL's actual maintenance-margin rate for paper; swap for HL's
  // liquidationPx when live). Fallback to a local synthetic if the
  // backend doesn't send one yet.
  const liqDist = (p) => {
    if (p.liqPrice && p.live) {
      return { dist: Math.abs((p.live - p.liqPrice) / p.live) * 100, liqPrice: p.liqPrice };
    }
    const liqPrice = p.side === 'LONG'
      ? p.avgEntry * (1 - (1/p.leverage) + 0.02)
      : p.avgEntry * (1 + (1/p.leverage) - 0.02);
    return { dist: Math.abs((p.live - liqPrice) / p.live) * 100, liqPrice };
  };

  return (
    <div className="tbl-wrap">
      <div className="tbl pos-tbl">
        <div className="tbl-head">
          <div className="th">TOKEN</div>
          <div className="th ta-r">SIZE</div>
          <div className="th ta-r">NOTIONAL</div>
          <div className="th ta-r">AVG ENTRY</div>
          <div className="th ta-r">LIVE</div>
          <div className="th ta-r">PNL / ROE</div>
          <div className="th ta-c">SL · TP1 · TP2</div>
          <div className="th ta-r">LIQ DIST</div>
          <div className="th ta-r">BARS</div>
          <div className="th ta-r">STATUS</div>
        </div>
        {positions.map((p, i) => (
          <div key={i} className="tbl-row">
            <div className="td token-cell">
              <div className="tk-sym">{p.token}</div>
              <div className={`tk-meta ${p.side === 'LONG' ? 'pos' : 'neg'}`}>
                {p.side} · {p.leverage.toFixed(2)}× <span className="dim">({p.mode})</span>
              </div>
            </div>
            <div className="td ta-r num">{fmt(p.size)}</div>
            <div className="td ta-r num">${fmt(p.notional)}</div>
            <div className="td ta-r num">{fmt(p.avgEntry)}</div>
            <div className="td ta-r num">{fmt(p.live)}</div>
            <div className="td ta-r">
              <div className={`num ${p.pnl >= 0 ? 'pos' : 'neg'}`}>{p.pnl >= 0 ? '+' : '−'}${fmt(Math.abs(p.pnl))}</div>
              <div className={`num-sm ${p.pnlPct >= 0 ? 'pos' : 'neg'}`}>{fmtPct(p.pnlPct)}</div>
            </div>
            <div className="td util-cell">
              {(() => {
                // Per-symbol progress bar: SL (exit) → furthest real TP.
                // Only dots for levels that ACTUALLY exist on this symbol's
                // position are drawn. Bar layout: SL=0% (left), furthest
                // TP=100% (right). Entry sits somewhere in between.
                // SHORT inverts direction (prices descend SL → TP).
                const sl = p.sl, tp1 = p.tp1, tp2 = p.tp2, tp3 = p.tp3;
                const isLong = p.side === 'LONG';
                const tps = [tp1, tp2, tp3].filter(x => x && x > 0);
                const farTp = tps.length ? tps[tps.length - 1] : null;
                // Trail level: when active, SL === trail stop. Before active,
                // compute the arming price as entry ± trail_offset.
                const trailOn = !!p.trail_active;
                const trailArm = (p.trail_offset > 0 && !trailOn && p.avgEntry)
                  ? (isLong ? p.avgEntry + p.trail_offset : p.avgEntry - p.trail_offset)
                  : null;
                if (!p.live) return <div className="util-bar"/>;
                // Anchor left = the worst-loss side (min for long, max for
                // short). When trail is active and SL has moved above entry
                // (long) the "loss side" becomes entry itself. Right anchor
                // extends to the furthest thing the price could reach:
                // far TP, live, or best_price — whichever is most extreme.
                const loLong  = Math.min(...[sl, p.avgEntry].filter(x => x && x > 0));
                const hiLong  = Math.max(...[farTp, p.live, p.best_price, p.avgEntry]
                                             .filter(x => x && x > 0));
                const loShort = Math.min(...[farTp, p.live, p.best_price, p.avgEntry]
                                             .filter(x => x && x > 0));
                const hiShort = Math.max(...[sl, p.avgEntry].filter(x => x && x > 0));
                const leftAnchor  = isLong ? loLong  : hiShort;
                const rightAnchor = isLong ? hiLong  : loShort;
                if (!leftAnchor || !rightAnchor) return <div className="util-bar"/>;
                const range = isLong ? (rightAnchor - leftAnchor) : (leftAnchor - rightAnchor);
                if (range <= 0) return <div className="util-bar"/>;
                const pctOf = (px) => {
                  const d = isLong ? (px - leftAnchor) : (leftAnchor - px);
                  return Math.max(0, Math.min(100, (d / range) * 100));
                };
                const livePct  = pctOf(p.live);
                const entryPct = p.avgEntry ? pctOf(p.avgEntry) : null;
                // Background gradient must visually span the full bar even
                // though the progress element is only livePct% wide. Trick:
                // scale background-size inversely so the gradient stretches
                // to (100/livePct)× the fill width.
                const fillBgSize = `${Math.max(100, 10000 / Math.max(0.1, livePct))}% 100%`;
                return (
                  <div className="util-bar">
                    <div className="util-bar-progress"
                         style={{ width: `${livePct}%`, backgroundSize: fillBgSize }}/>
                    {/* SL / TRAIL dot — rendered at its real relative position
                        (left anchor is min(sl,entry), so SL may sit past 0%
                        once trail pushes it above entry). */}
                    {sl && (
                      <div className={`util-tick util-tick-sl ${trailOn ? 'trail' : ''}`}
                           title={`${trailOn ? 'TRAIL' : 'SL'} ${fmt(sl)}`}
                           style={{ left: `${pctOf(sl)}%` }}/>
                    )}
                    {/* Trail-arm tick (only before trail activates) */}
                    {trailArm != null && (
                      <div className="util-tick util-tick-trail-arm"
                           title={`trail arms @ ${fmt(trailArm)}`}
                           style={{ left: `${pctOf(trailArm)}%` }}/>
                    )}
                    {/* Entry dot */}
                    {entryPct != null && (
                      <div className="util-tick util-tick-entry" title={`ENTRY ${fmt(p.avgEntry)}`} style={{ left: `${entryPct}%` }}/>
                    )}
                    {/* TP1 — only if set */}
                    {tp1 && (
                      <div className={`util-tick util-tick-tp ${p.tp1_hit ? 'hit' : ''}`}
                           title={`TP1 ${fmt(tp1)}${p.tp1_hit ? ' (hit)' : ''}`}
                           style={{ left: `${pctOf(tp1)}%` }}/>
                    )}
                    {/* TP2 — only if set */}
                    {tp2 && (
                      <div className={`util-tick util-tick-tp ${p.tp2_hit ? 'hit' : ''}`}
                           title={`TP2 ${fmt(tp2)}${p.tp2_hit ? ' (hit)' : ''}`}
                           style={{ left: `${pctOf(tp2)}%` }}/>
                    )}
                    {/* TP3 — only if set */}
                    {tp3 && (
                      <div className={`util-tick util-tick-tp ${p.tp3_hit ? 'hit' : ''}`}
                           title={`TP3 ${fmt(tp3)}${p.tp3_hit ? ' (hit)' : ''}`}
                           style={{ left: `${pctOf(tp3)}%` }}/>
                    )}
                    {/* Live-price marker — rides the fill edge. */}
                    <div className="util-live-marker" title={`LIVE ${fmt(p.live)}`} style={{ left: `${livePct}%` }}/>
                  </div>
                );
              })()}
            </div>
            <div className="td ta-r liq-cell">
              {(() => { const { dist, liqPrice } = liqDist(p);
                const col = dist > 30 ? 'pos' : dist > 15 ? 'warn' : 'neg';
                return <>
                  <div className={`num ${col}`}>{dist.toFixed(1)}%</div>
                  <div className="num-sm dim">@ {fmt(liqPrice)}</div>
                </>;
              })()}
            </div>
            <div className="td ta-r bars-cell">
              <div className="num-sm dim">{p.slHit}/{p.slMax}</div>
              <SlBar hit={p.slHit} max={p.slMax}/>
            </div>
            <div className="td ta-r">
              <span className="status-chip"><span className="dot pos"/>{p.status}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SlBar({ hit, max }) {
  const pct = hit / max;
  return (
    <div className="slbar">
      <div className="slbar-fill" style={{ width: `${pct * 100}%` }}/>
    </div>
  );
}

function FillsTable({ fills, flashKey }) {
  const tagColor = { open: 'dim', add: 'dim', tp: 'pos', sl: 'neg', close: 'warn' };
  return (
    <div className="tbl-wrap">
      <div className="tbl fills-tbl">
        <div className="tbl-head">
          <div className="th">TIME</div>
          <div className="th">SYMBOL</div>
          <div className="th">SIDE</div>
          <div className="th ta-r">QTY</div>
          <div className="th ta-r">PRICE</div>
          <div className="th ta-r">FEE</div>
          <div className="th ta-r">PNL</div>
          <div className="th">TAG</div>
        </div>
        {fills.map((f, i) => (
          <div key={`${f.t}-${f.sym}-${i}`} className={`tbl-row ${i === 0 && flashKey ? 'flash-row' : ''}`}>
            <div className="td dim num">{f.t}</div>
            <div className="td"><span className="tk-sym-sm">{f.sym}</span></div>
            <div className={`td ${f.side === 'BUY' ? 'pos' : 'neg'}`}>{f.side}</div>
            <div className="td ta-r num">{fmt(f.qty)}</div>
            <div className="td ta-r num">{fmt(f.px)}</div>
            <div className="td ta-r num dim">${fmt(f.fee)}</div>
            <div className="td ta-r num">
              {f.pnl === null ? <span className="dim">—</span> :
                <span className={f.pnl >= 0 ? 'pos' : 'neg'}>{f.pnl >= 0 ? '+' : '−'}${fmt(Math.abs(f.pnl))}</span>}
            </div>
            <div className="td"><span className={`tag tag-${tagColor[f.tag]}`}>{f.tag.toUpperCase()}</span></div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SymbolsTable({ symbols }) {
  const maxPnl = Math.max(...symbols.map(s => Math.abs(s.pnl)));
  return (
    <div className="tbl-wrap">
      <div className="tbl sym-tbl">
        <div className="tbl-head">
          <div className="th">SYMBOL</div>
          <div className="th ta-r">PNL</div>
          <div className="th">DISTRIBUTION</div>
          <div className="th ta-r">TRADES</div>
          <div className="th ta-r">WIN RATE</div>
          <div className="th ta-r">VOLUME</div>
        </div>
        {symbols.map((s, i) => (
          <div key={i} className="tbl-row">
            <div className="td"><span className="tk-sym-sm">{s.sym}</span></div>
            <div className={`td ta-r num ${s.pnl >= 0 ? 'pos' : 'neg'}`}>{s.pnl >= 0 ? '+' : '−'}${fmt(Math.abs(s.pnl))}</div>
            <div className="td">
              <div className="dist-bar">
                {s.pnl >= 0
                  ? <div className="dist-pos" style={{ width: `${(s.pnl / maxPnl) * 50}%`, left: '50%' }}/>
                  : <div className="dist-neg" style={{ width: `${(Math.abs(s.pnl) / maxPnl) * 50}%`, right: '50%' }}/>}
                <div className="dist-center"/>
              </div>
            </div>
            <div className="td ta-r num">{s.trades}</div>
            <div className="td ta-r num">
              {s.wr}%
              <div className="wr-bar"><div className="wr-fill" style={{ width: `${s.wr}%` }}/></div>
            </div>
            <div className="td ta-r num dim">${fmt(s.vol)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AttributionView({ symbols }) {
  const total = symbols.reduce((a, s) => a + Math.abs(s.pnl), 0);
  return (
    <div className="attrib-wrap">
      <div className="attrib-bar">
        {symbols.map((s, i) => {
          const w = (Math.abs(s.pnl) / total) * 100;
          return (
            <div key={i} className={`attrib-seg ${s.pnl >= 0 ? 'pos' : 'neg'}`} style={{ width: `${w}%` }}>
              <span className="attrib-lbl">{s.sym}</span>
              <span className="attrib-val">{s.pnl >= 0 ? '+' : '−'}${fmt(Math.abs(s.pnl), 0)}</span>
            </div>
          );
        })}
      </div>
      <div className="attrib-grid">
        {symbols.map((s, i) => (
          <div key={i} className="attrib-cell">
            <div className="attrib-sym">{s.sym}</div>
            <div className={`attrib-pnl ${s.pnl >= 0 ? 'pos' : 'neg'}`}>{s.pnl >= 0 ? '+' : '−'}${fmt(Math.abs(s.pnl), 0)}</div>
            <div className="attrib-meta"><span className="dim">WR</span> {s.wr}% <span className="dim">N</span> {s.trades}</div>
            <div className="attrib-hmap">
              {Array.from({length: 24}).map((_, j) => {
                const v = Math.sin(i * 1.3 + j * 0.7) * 0.5 + 0.5;
                const green = s.pnl >= 0 ? v : 1 - v;
                return <div key={j} className="hm-cell" style={{ background: green > 0.5 ? `rgba(74,222,128,${(green-0.5)*2})` : `rgba(248,113,113,${(0.5-green)*2})` }}/>;
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function VaultView() {
  return (
    <div className="vault-view">
      <div className="vault-card">
        <Label>strategy.whale-swing</Label>
        <div className="vault-title">Whale Swing v3.2</div>
        <div className="vault-meta">
          <span className="chip chip-dim">LIVE</span>
          <span className="dim">uptime 14d 03:12</span>
        </div>
        <HR/>
        <div className="kv"><span className="k">capital deployed</span><span className="v">$11,007.79</span></div>
        <div className="kv"><span className="k">target exposure</span><span className="v">40%</span></div>
        <div className="kv"><span className="k">current exposure</span><span className="v">24.5%</span></div>
        <div className="kv"><span className="k">signals (24h)</span><span className="v">142</span></div>
        <div className="kv"><span className="k">entries</span><span className="v">18</span></div>
        <div className="kv"><span className="k">hit ratio</span><span className="v pos">12.7%</span></div>
      </div>
      <div className="vault-card">
        <Label>params</Label>
        <div className="kv mono"><span className="k">lookback_bars</span><span className="v">240</span></div>
        <div className="kv mono"><span className="k">entry_zscore</span><span className="v">2.15</span></div>
        <div className="kv mono"><span className="k">exit_zscore</span><span className="v">0.40</span></div>
        <div className="kv mono"><span className="k">max_leverage</span><span className="v">3.00</span></div>
        <div className="kv mono"><span className="k">stop_atr_mult</span><span className="v">2.80</span></div>
        <div className="kv mono"><span className="k">tp_atr_mult</span><span className="v">4.50</span></div>
        <div className="kv mono"><span className="k">min_edge_bps</span><span className="v">18</span></div>
        <div className="kv mono"><span className="k">kill_drawdown</span><span className="v">15.0%</span></div>
      </div>
      <div className="vault-card">
        <Label>log (last 8)</Label>
        <div className="log">
          <div className="log-l"><span className="dim">16:57:12</span> <span className="pos">ENTER</span> ZEC @ 326.335 size=3.2140</div>
          <div className="log-l"><span className="dim">16:54:02</span> <span className="pos">ADD</span> HYPE @ 41.520 size=12.108</div>
          <div className="log-l"><span className="dim">16:51:48</span> <span className="warn">TP1</span> SOL @ 86.44 qty=4.21 pnl=+18.42</div>
          <div className="log-l"><span className="dim">16:48:33</span> <span className="warn">TP1</span> INJ @ 3.318 qty=142 pnl=+6.60</div>
          <div className="log-l"><span className="dim">16:45:10</span> <span className="warn">CLOSE</span> ETH @ 2341 qty=0.482 pnl=+11.20</div>
          <div className="log-l"><span className="dim">16:42:57</span> <span className="pos">ENTER</span> ZEC @ 317.210 size=6.004</div>
          <div className="log-l"><span className="dim">16:39:22</span> <span className="neg">SL</span> PENDLE @ 1.312 qty=820 pnl=-4.10</div>
          <div className="log-l"><span className="dim">16:36:01</span> <span className="pos">ENTER</span> HYPE @ 41.190 size=48.22</div>
        </div>
      </div>
    </div>
  );
}

function RiskView({ positions }) {
  return (
    <div className="risk-view">
      <div className="risk-card">
        <Label>account risk</Label>
        <div className="risk-kv"><span className="k">margin used</span><span className="v">$2,265.68 / $17,400.00</span></div>
        <div className="risk-kv"><span className="k">utilization</span><span className="v">13.02%</span></div>
        <div className="risk-kv"><span className="k">liq. distance (min)</span><span className="v pos">32.4%</span></div>
        <div className="risk-kv"><span className="k">max drawdown (30d)</span><span className="v warn">-8.80%</span></div>
        <div className="risk-kv"><span className="k">VaR 95 (1d)</span><span className="v">-$412.18</span></div>
        <div className="risk-kv"><span className="k">beta to BTC</span><span className="v">0.42</span></div>
      </div>
      <div className="risk-card wide">
        <Label>exposure by symbol</Label>
        <div className="exposure-bars">
          {positions.map((p, i) => {
            const max = Math.max(...positions.map(x => x.notional));
            const w = (p.notional / max) * 100;
            return (
              <div key={i} className="exp-row">
                <div className="exp-sym">{p.token}</div>
                <div className="exp-bar">
                  <div className={`exp-fill ${p.side === 'LONG' ? 'pos' : 'neg'}`} style={{ width: `${w}%` }}/>
                </div>
                <div className="exp-val">${fmt(p.notional)}</div>
                <div className="exp-side">{p.side}</div>
              </div>
            );
          })}
        </div>
        <HR/>
        <CorrelationMatrix/>
      </div>
    </div>
  );
}

Object.assign(window, { LowerPanel });
