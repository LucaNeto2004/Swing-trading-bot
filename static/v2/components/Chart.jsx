// Equity / PnL chart — main centerpiece

// Pick a "nice" step + tick list for a given value range. Mirrors the
// algorithm D3 uses under the hood: round step from {1,2,5,10,20,25,50,...}
// so ticks read as clean dollar values (+$100, +$250, …) instead of the raw
// equal-division numbers (+$463, +$465, +$527 …).
function niceTicks(minV, maxV, targetCount = 5) {
  if (!(maxV > minV)) return { ticks: [minV], step: 1, min: minV, max: maxV };
  const rough = (maxV - minV) / Math.max(1, targetCount - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  let step;
  if (norm < 1.5)      step = 1 * mag;
  else if (norm < 3)   step = 2 * mag;
  else if (norm < 4)   step = 2.5 * mag;
  else if (norm < 7)   step = 5 * mag;
  else                 step = 10 * mag;
  const niceMin = Math.floor(minV / step) * step;
  const niceMax = Math.ceil(maxV / step) * step;
  const ticks = [];
  for (let v = niceMin; v <= niceMax + step * 0.001; v += step) ticks.push(v);
  return { ticks, step, min: niceMin, max: niceMax };
}

// Monotone-cubic-style smoothing via quadratic Bezier with midpoint T-joins.
// No neighbor-peeking → no overshoot, no parabolic arcs across sparse data.
// Per reference 24h-chart spec.
function smoothPath(pts, xOf, yOf) {
  if (!pts || pts.length === 0) return '';
  if (pts.length === 1) {
    const p = pts[0];
    return `M${xOf(p).toFixed(1)},${yOf(p).toFixed(1)}`;
  }
  const segs = [];
  segs.push(`M${xOf(pts[0]).toFixed(1)},${yOf(pts[0]).toFixed(1)}`);
  for (let i = 1; i < pts.length; i++) {
    const p0 = pts[i - 1], p1 = pts[i];
    const x0 = xOf(p0), y0 = yOf(p0), x1 = xOf(p1), y1 = yOf(p1);
    const mx = (x0 + x1) / 2;
    const my = (y0 + y1) / 2;
    segs.push(`Q${x0.toFixed(1)},${y0.toFixed(1)} ${mx.toFixed(1)},${my.toFixed(1)}`);
    segs.push(`T${x1.toFixed(1)},${y1.toFixed(1)}`);
  }
  return segs.join(' ');
}

function EquityChart({ range, setRange, pts, allPnl, mode, setMode, livePx, tSeries, tWindow, activeMs }) {
  const W = 1180, H = 360;
  const padL = 48, padR = 72, padT = 30, padB = 32;
  const iw = W - padL - padR, ih = H - padT - padB;

  const [hover, setHover] = useState(null);
  const svgRef = useRef(null);

  // 24h uses the time-aware series passed in; non-24h uses index-based pts.
  const isTime = !!(tSeries && tWindow);

  // Live-tail (used only on non-24h ranges in equity mode; 24h is already
  // fully driven by tSeries).
  const [liveTail, setLiveTail] = useState([]);
  const lastPushRef = useRef(0);
  useEffect(() => {
    if (livePx == null) return;
    const now = Date.now();
    if (now - lastPushRef.current < 800) return;
    lastPushRef.current = now;
    setLiveTail(t => {
      const next = [...t, livePx];
      return next.length > 180 ? next.slice(-180) : next;
    });
  }, [livePx]);

  const isPnl = mode === 'pnl' || range === '24h';

  // Bail out early with an empty placeholder when we have no data yet.
  // Otherwise Math.min(...[]) returns Infinity and the first attempt to
  // index data[data.length - 1] throws, blanking the whole page.
  const hasData = isTime ? (tSeries && tSeries.length > 0) : (pts && pts.length > 0);
  if (!hasData) {
    return (
      <div className="panel chart-panel">
        <div className="chart-head">
          <div className="chart-tabs"><span className="dim" style={{fontSize:11}}>waiting for data…</span></div>
        </div>
        <div style={{height: H, display:'grid', placeItems:'center', color:'var(--dim)', fontSize:11}}>
          no samples yet — first /api/state poll still pending
        </div>
      </div>
    );
  }

  // Compute y-range from the active data source.
  const yValues = isTime ? tSeries.map(p => p.y) : pts;
  const rawMin = Math.min(...yValues), rawMax = Math.max(...yValues);
  const span = (rawMax - rawMin) || 1;
  // 15% headroom top and bottom.
  let roughLo = rawMin - span * 0.15;
  let roughHi = rawMax + span * 0.15;
  if (isPnl) {
    // Time-based PnL charts (24h/7d/30d/All): always anchor the scale to 0
    // so the session baseline gridline is visible. No negative padding when
    // all data is non-negative; no positive padding when all non-positive.
    if (isTime) {
      if (rawMin >= 0) roughLo = 0;
      if (rawMax <= 0) roughHi = 0;
    } else {
      const range = roughHi - roughLo;
      if (roughLo > 0 && roughLo < range * 0.25) roughLo = 0;
      if (roughHi < 0 && Math.abs(roughHi) < range * 0.25) roughHi = 0;
    }
  }
  // Nice-number snapped y-range + tick list so labels read as clean dollars.
  const nice = niceTicks(roughLo, roughHi, 5);
  const lo = nice.min, hi = nice.max;
  const y = (v) => padT + ih - ((v - lo) / (hi - lo || 1)) * ih;

  // Path + fill + xLast — two code paths: time-based vs index-based.
  // Both go through Catmull-Rom → cubic Bezier so the line is smooth rather
  // than chunky straight-segments with visible corners.
  let path, fill, xLast, firstX, lastY, data;
  if (isTime) {
    const span = Math.max(1, tWindow.end - tWindow.start);
    const xt = (t) => padL + ((t - tWindow.start) / span) * iw;
    // Minimum-pixel-spacing filter — drop samples that would render <2px
    // apart. Guarantees no visual cramming regardless of data density; the
    // last point is always preserved so the live edge stays accurate.
    // No prefix trimming — all points in the visible window render as a
    // single continuous line (Option A: viewport carries the "lead-in",
    // the data doesn't need to be carved up).
    const MIN_PX = 2;
    const thinned = [tSeries[0]];
    for (let i = 1; i < tSeries.length; i++) {
      const dx = xt(tSeries[i].t) - xt(thinned[thinned.length - 1].t);
      if (dx >= MIN_PX) thinned.push(tSeries[i]);
    }
    if (thinned[thinned.length - 1] !== tSeries[tSeries.length - 1]) {
      thinned.push(tSeries[tSeries.length - 1]);
    }
    data = thinned;
    path = smoothPath(data, p => xt(p.t), p => y(p.y));
    firstX = xt(data[0].t);
    xLast = xt(data[data.length - 1].t);
    lastY = data[data.length - 1].y;
    fill = `${path} L${xLast.toFixed(1)},${(padT+ih).toFixed(1)} L${firstX.toFixed(1)},${(padT+ih).toFixed(1)} Z`;
  } else {
    const merged = (() => {
      if (!liveTail.length) return pts;
      const keep = Math.max(1, pts.length - liveTail.length);
      return [...pts.slice(0, keep), ...liveTail];
    })();
    data = merged;
    const xi = (i) => padL + (i / Math.max(1, data.length - 1)) * iw;
    path = smoothPath(data.map((v, i) => ({ i, v })), p => xi(p.i), p => y(p.v));
    firstX = xi(0);
    xLast = xi(data.length - 1);
    lastY = data[data.length - 1];
    fill = `${path} L${xLast.toFixed(1)},${(padT+ih).toFixed(1)} L${firstX.toFixed(1)},${(padT+ih).toFixed(1)} Z`;
  }

  // Y-axis grid values — from the nice-tick list above.
  const yVals = nice.ticks;

  // X-axis labels — dynamic from the window span. <48h shows HH:MM, longer
  // windows show "MMM d". Calendar positions now track the real timeline
  // because the series is time-aware on every range.
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const spanMs = isTime ? (tWindow.end - tWindow.start) : 0;
  let TICK_COUNT = 8;
  if (isTime) {
    if (spanMs >= 28 * 86400 * 1000) TICK_COUNT = 6;
    else if (spanMs >= 6 * 86400 * 1000) TICK_COUNT = 7;
  }
  const labels = isTime
    ? Array.from({length: TICK_COUNT}, (_, i) => {
        const t = tWindow.start + spanMs * (i / (TICK_COUNT - 1));
        const d = new Date(t);
        if (spanMs < 48 * 3600 * 1000) {
          return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
        }
        return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
      })
    : [];

  const onMove = (e) => {
    const rect = svgRef.current.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    if (isTime) {
      const span = Math.max(1, tWindow.end - tWindow.start);
      const tAt = tWindow.start + ((px - padL) / iw) * span;
      let best = 0, bestD = Infinity;
      for (let i = 0; i < data.length; i++) {
        const d = Math.abs(data[i].t - tAt);
        if (d < bestD) { bestD = d; best = i; }
      }
      const p = data[best];
      setHover({ idx: best, t: p.t, v: p.y, x: padL + ((p.t - tWindow.start) / span) * iw, y: y(p.y) });
      return;
    }
    const idx = Math.round(((px - padL) / iw) * (data.length - 1));
    if (idx >= 0 && idx < data.length) {
      setHover({ idx, t: null, v: data[idx], x: padL + (idx / Math.max(1, data.length - 1)) * iw, y: y(data[idx]) });
    }
  };

  return (
    <div className="panel chart-panel">
      <div className="chart-head">
        <div className="chart-tabs">
          {range !== '24h' && (
            <button className={`ctab ${mode === 'equity' ? 'on' : ''}`} onClick={() => setMode('equity')}>Equity</button>
          )}
          <button className={`ctab ${mode === 'pnl' ? 'on' : ''}`} onClick={() => setMode('pnl')}>PnL</button>
          {range === '24h' && (
            <span className="dim" style={{fontSize:10, marginLeft:8}}>
              · today (resets midnight)
              {activeMs != null && (() => {
                const mins = Math.max(1, Math.round(activeMs / 60000));
                const label = mins < 60 ? `${mins}m` : `${(mins / 60).toFixed(1)}h`;
                return ` · ${label} active`;
              })()}
            </span>
          )}
        </div>
        <div className="chart-ranges">
          {['24h','7d','30d','All'].map(r => {
            const key = r.toLowerCase();
            return (
              <button key={r} onClick={() => setRange(key)} className={`range-btn ${range === key ? 'on' : ''}`}>
                {r}
              </button>
            );
          })}
        </div>
        <div className="chart-right">
          <span className="lbl">all pnl ({range})</span>
          <span className={`chart-pnl ${allPnl >= 0 ? 'pos' : 'neg'}`}>
            {allPnl >= 0 ? '+' : '−'}${fmt(Math.abs(allPnl), 2)}
          </span>
        </div>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`}
           preserveAspectRatio="none"
           className="chart-svg"
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {/* crosshatch background */}
        <defs>
          <pattern id="grid" width="60" height="40" patternUnits="userSpaceOnUse">
            <path d="M60 0 H0 V40" fill="none" stroke="#12181d" strokeWidth="0.5"/>
          </pattern>
          {(() => {
            // Sign-aware vertical gradients — green above $0, red below.
            // Anchored to plot area in user-space so the split lands exactly
            // on the zero gridline regardless of y-range.
            const zeroPx = (isPnl && lo < 0 && hi > 0) ? y(0) : null;
            const zeroFrac = zeroPx != null ? (zeroPx - padT) / ih : null;
            const allPos = isPnl && lo >= 0;
            const allNeg = isPnl && hi <= 0;
            if (allPos || zeroFrac == null && lastY >= 0) {
              return (
                <>
                  <linearGradient id="fillg" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--pos)" stopOpacity="0.18"/>
                    <stop offset="100%" stopColor="var(--pos)" stopOpacity="0"/>
                  </linearGradient>
                  <linearGradient id="lineg" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--pos)"/>
                    <stop offset="100%" stopColor="var(--pos)"/>
                  </linearGradient>
                </>
              );
            }
            if (allNeg || zeroFrac == null) {
              return (
                <>
                  <linearGradient id="fillg" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--neg)" stopOpacity="0"/>
                    <stop offset="100%" stopColor="var(--neg)" stopOpacity="0.18"/>
                  </linearGradient>
                  <linearGradient id="lineg" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--neg)"/>
                    <stop offset="100%" stopColor="var(--neg)"/>
                  </linearGradient>
                </>
              );
            }
            const z = (zeroFrac * 100).toFixed(2) + '%';
            return (
              <>
                {/* Fill: green above $0 fading to transparent at $0,
                    red below $0 ramping back up to 0.18 at the bottom. */}
                <linearGradient id="fillg" x1="0" y1={padT} x2="0" y2={padT + ih}
                                gradientUnits="userSpaceOnUse">
                  <stop offset="0%"   stopColor="var(--pos)" stopOpacity="0.18"/>
                  <stop offset={z}    stopColor="var(--pos)" stopOpacity="0"/>
                  <stop offset={z}    stopColor="var(--neg)" stopOpacity="0"/>
                  <stop offset="100%" stopColor="var(--neg)" stopOpacity="0.18"/>
                </linearGradient>
                {/* Stroke: hard color flip at the zero line. */}
                <linearGradient id="lineg" x1="0" y1={padT} x2="0" y2={padT + ih}
                                gradientUnits="userSpaceOnUse">
                  <stop offset="0%"   stopColor="var(--pos)"/>
                  <stop offset={z}    stopColor="var(--pos)"/>
                  <stop offset={z}    stopColor="var(--neg)"/>
                  <stop offset="100%" stopColor="var(--neg)"/>
                </linearGradient>
              </>
            );
          })()}
        </defs>
        <rect x={padL} y={padT} width={iw} height={ih} fill="url(#grid)"/>

        {/* Y-axis ticks and gridlines — negative PnL labels render in red. */}
        {yVals.map((v, i) => {
          const cls = isPnl
            ? `axis-t ${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}`
            : 'axis-t';
          return (
            <g key={i}>
              <line x1={padL} y1={y(v)} x2={padL+iw} y2={y(v)} stroke="#151c22" strokeDasharray="2,3"/>
              <text x={padL - 8} y={y(v) + 3} textAnchor="end" className={cls}>
                {isPnl ? `${v >= 0 ? '+' : '−'}$${fmt(Math.abs(v), 0)}` : `$${fmt(v, 0)}`}
              </text>
            </g>
          );
        })}

        {/* Zero baseline — only drawn when $0 falls inside the visible
            y-range (excluded when the range is tightened around non-zero
            data to stop the flat-$0 prefix from dominating the scale). */}
        {isPnl && lo <= 0 && hi >= 0 && (
          <line x1={padL} y1={y(0)} x2={padL+iw} y2={y(0)} stroke="#3a4650" strokeWidth="0.75"/>
        )}

        {/* X-axis labels */}
        {labels.map((l, i) => {
          const xp = padL + (i / (labels.length - 1)) * iw;
          return (
            <g key={i}>
              <line x1={xp} y1={padT+ih} x2={xp} y2={padT+ih+3} stroke="#2a3339"/>
              <text x={xp} y={padT+ih+16} textAnchor="middle" className="axis-t">{l}</text>
            </g>
          );
        })}

        {/* Fill */}
        <path d={fill} fill="url(#fillg)"/>
        {/* Line — sign-aware: green above $0, red below. */}
        <path d={path} fill="none" stroke={isPnl ? 'url(#lineg)' : (lastY >= 0 ? 'var(--pos)' : 'var(--neg)')}
              strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"/>

        {/* Live-edge vertical dashed line — pinned at the right edge of
            the plot area. The ball rides along it at the live PnL level. */}
        <line x1={padL+iw} y1={padT} x2={padL+iw} y2={padT+ih} stroke={lastY >= 0 ? 'var(--pos)' : 'var(--neg)'} strokeDasharray="2,2" strokeOpacity="0.35"/>
        {/* End dot — the "ball" riding the vertical line at the right edge. */}
        <circle cx={padL+iw} cy={y(lastY)} r="3" fill={lastY >= 0 ? 'var(--pos)' : 'var(--neg)'}/>

        {/* Last value label on right */}
        <rect x={padL+iw+4} y={y(lastY)-9} width="64" height="18" fill="#0c1013" stroke={lastY >= 0 ? 'var(--pos)' : 'var(--neg)'}/>
        <text x={padL+iw+36} y={y(lastY)+4} textAnchor="middle"
              className={`axis-p ${lastY < 0 ? 'neg' : ''}`}>
          {isPnl
            ? `${lastY >= 0 ? '+' : '−'}$${fmt(Math.abs(lastY), 0)}`
            : `$${fmt(lastY, 0)}`}
        </text>

        {/* Hover crosshair — vertical + horizontal guide at the hovered
            sample. Coexists with the permanent live-value horizontal
            (which stays locked on the end dot). */}
        {hover && (() => {
          const hoverColor = isPnl
            ? (hover.v > 0 ? 'var(--pos)' : hover.v < 0 ? 'var(--neg)' : 'var(--dim)')
            : 'var(--pos)';
          const tipCls = isPnl
            ? `tip-v ${hover.v > 0 ? 'pos' : hover.v < 0 ? 'neg' : ''}`
            : 'tip-v';
          return (
          <g>
            <line x1={hover.x} y1={padT} x2={hover.x} y2={padT+ih} stroke="#3a4650" strokeDasharray="3,3"/>
            <line x1={padL} y1={hover.y} x2={hover.x} y2={hover.y} stroke="#3a4650" strokeDasharray="3,3"/>
            <circle cx={hover.x} cy={hover.y} r="4" fill="#07090b" stroke={hoverColor} strokeWidth="1.5"/>
            <g transform={`translate(${Math.min(hover.x + 10, padL + iw - 120)}, ${Math.max(hover.y - 38, padT + 4)})`}>
              <rect width="118" height="34" fill="#0c1013" stroke="#2a3339"/>
              <text x="8" y="14" className="tip-k">{isPnl ? 'pnl' : 'equity'}</text>
              <text x="110" y="14" textAnchor="end" className={tipCls}>
                {isPnl
                  ? `${hover.v >= 0 ? '+' : '−'}$${fmt(Math.abs(hover.v), 2)}`
                  : `$${fmt(hover.v, 2)}`}
              </text>
              <text x="8" y="27" className="tip-k">time</text>
              <text x="110" y="27" textAnchor="end" className="tip-v">
                {hover.t ? (() => {
                  const d = new Date(hover.t);
                  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
                })() : `#${hover.idx}`}
              </text>
            </g>
          </g>
          );
        })()}
      </svg>

      {/* Chart footer strip */}
      <div className="chart-foot">
        <div className="foot-k">high</div><div className="foot-v pos">${fmt(Math.max(...yValues))}</div>
        <div className="foot-k">low</div><div className="foot-v neg">${fmt(Math.min(...yValues))}</div>
        <div className="foot-k">range</div><div className="foot-v">${fmt(Math.max(...yValues) - Math.min(...yValues))}</div>
        <div className="foot-k">avg</div><div className="foot-v">${fmt(yValues.reduce((a,b)=>a+b,0)/yValues.length)}</div>
        <div className="foot-k">σ</div><div className="foot-v">{fmt(stddev(yValues))}</div>
        <div className="foot-k">ticks</div><div className="foot-v">{yValues.length}</div>
      </div>
    </div>
  );
}

function stddev(arr) {
  const m = arr.reduce((a,b)=>a+b,0) / arr.length;
  const v = arr.reduce((a,b)=>a + (b-m)**2, 0) / arr.length;
  return Math.sqrt(v);
}

Object.assign(window, { EquityChart });
