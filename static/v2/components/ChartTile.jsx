// Rich chart tile — feeds from /api/chart_data/<sym> (a dashboard proxy to
// scripts/chart_server.py) so every tile carries the SAME overlay data the
// legacy chart server renders: EMA9/21/50, BB bands, RSI, 1h pivots,
// validated pivots (SELL/BUY stars), regime ribbon, BOS trigger levels,
// and open-position entry/SL/TP lines.
//
// Interaction: TradingView-style drag pan + wheel zoom + y-axis stretch
// + x-axis squish + dbl-click reset (same as before).

const { useMemo, useState: useStateT, useEffect: useEffectT, useRef: useRefT } = React;

function ChartTile({ meta, onExpand, big, interval = '5m' }) {
  const [data, setData] = useStateT(null);         // full /api/chart_data payload
  const [liveMid, setLiveMid] = useStateT(null);   // live price tick via HL WS
  const [pxFlash, setPxFlash] = useStateT(0);
  const prevPxRef = useRefT(null);
  const svgRef = useRefT(null);

  const MAX_BARS_VIEW = big ? 500 : 80;
  const [view, setView] = useStateT(null);   // { start, end } indices
  const [autoFollow, setAutoFollow] = useStateT(true);
  const [yStretch, setYStretch] = useStateT(1);   // 1 = auto pad; drag to scale
  const panRef = useRefT(null);
  const axisDragRef = useRefT(null);

  // Fetch rich chart data on mount + whenever interval changes. Poll every 20s
  // so overlays (regime, pivots, BOS, position) stay fresh.
  useEffectT(() => {
    let alive = true;
    const fetch_ = async () => {
      try {
        const r = await fetch(`/api/chart_data/${encodeURIComponent(meta.sym)}?interval=${interval}`, { cache: 'no-store' });
        const j = await r.json();
        if (!alive || !j.ok) return;
        setData(j);
        if (!view) {
          const n = (j.candles || []).length;
          setView({ start: Math.max(0, n - 80), end: n });
        }
      } catch (e) {}
    };
    fetch_();
    const id = setInterval(fetch_, 20000);
    return () => { alive = false; clearInterval(id); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta.sym, interval]);

  // Live mid price — keeps the rightmost bar wick moving while we wait for the
  // next 20s REST refresh.
  useEffectT(() => {
    if (!window.HL) return;
    const unsub = window.HL.ws.subscribeMid(meta.sym, (px) => {
      setLiveMid(px);
      const prev = prevPxRef.current;
      if (prev != null && px !== prev) {
        setPxFlash(px > prev ? 1 : -1);
        setTimeout(() => setPxFlash(0), 300);
      }
      prevPxRef.current = px;
    });
    return () => unsub && unsub();
  }, [meta.sym]);

  const W = big ? 1400 : 440, H = big ? 780 : 300;
  const priceH = big ? 560 : 200, volH = big ? 70 : 30, rsiH = big ? 120 : 50;
  const padL = 8, padR = big ? 60 : 60, padT = 6;
  const iw = W - padL - padR;

  const fmtP = (v) => {
    if (v == null) return '—';
    if (Math.abs(v) >= 1000) return v.toFixed(0);
    if (Math.abs(v) >= 10) return v.toFixed(2);
    if (Math.abs(v) >= 1) return v.toFixed(3);
    return v.toFixed(5);
  };

  // Loading state — no candles yet.
  if (!data || !data.candles || data.candles.length < 20 || !view) {
    return (
      <div className={`chart-tile ${big ? 'chart-tile-big' : ''}`}>
        <div className="tile-head">
          <div className="tile-head-l">
            <span className="tile-sym">{meta.sym}</span>
            <span className="tile-status tile-status-loading">◌ LOADING</span>
          </div>
          <div className="tile-head-r"><span className="dim">fetching…</span></div>
        </div>
        <div className="tile-loading">
          <span className="dim">
            {data && data.error ? `chart_server: ${data.error}` : `fetching ${meta.sym} ${interval}…`}
          </span>
        </div>
      </div>
    );
  }

  const candles = data.candles;  // [{time, open, high, low, close}]
  const vStart = Math.max(0, Math.min(view.start, candles.length - 2));
  const vEnd = Math.max(vStart + 2, Math.min(view.end, candles.length + Math.round((view.end - view.start) * 0.2)));
  const visibleCandles = candles.slice(vStart, Math.min(vEnd, candles.length));

  // Y range from visible candles + bands
  const toArr = (series, key = 'value') => series
    .filter(p => p.time >= (visibleCandles[0]?.time || 0) && p.time <= (visibleCandles[visibleCandles.length - 1]?.time || 0))
    .map(p => p[key]);
  const highs = visibleCandles.map(c => c.high);
  const lows = visibleCandles.map(c => c.low);
  const bbU = toArr(data.bb_upper || []);
  const bbL = toArr(data.bb_lower || []);
  let allHigh = Math.max(...highs, ...bbU);
  let allLow = Math.min(...lows, ...bbL);
  if (liveMid != null && !Number.isNaN(liveMid)) {
    allHigh = Math.max(allHigh, liveMid);
    allLow = Math.min(allLow, liveMid);
  }
  const basePad = (allHigh - allLow) * 0.05;
  // yStretch: drag the right gutter vertically to expand/compress the y band.
  const pad = basePad * yStretch;
  const extraCenter = ((allHigh - allLow) * (yStretch - 1)) / 2;
  const hi = allHigh + pad + extraCenter;
  const lo = allLow - pad - extraCenter;

  // X scaling — time-based across the visible window. When view.end exceeds
  // candles.length the user has panned into "future" whitespace; extend tMax
  // so that empty area actually renders.
  const candleInterval = candles.length >= 2
    ? (candles[candles.length - 1].time - candles[candles.length - 2].time) || 300
    : 300;
  const tMin = visibleCandles[0].time;
  const futureBars = Math.max(0, view.end - candles.length);
  const tMax = visibleCandles[visibleCandles.length - 1].time + futureBars * candleInterval;
  const tSpan = Math.max(1, tMax - tMin);
  const xt = (t) => padL + ((t - tMin) / tSpan) * iw;
  const y = (v) => padT + priceH - ((v - lo) / (hi - lo || 1)) * priceH;
  const viewLen = Math.max(1, view.end - view.start);
  const cw = Math.max(1, (iw / viewLen) * 0.7);

  // Line-series path builders (time-based)
  const linePath = (series) => {
    if (!series || series.length === 0) return '';
    const pts = series
      .filter(p => p.time >= tMin && p.time <= tMax && p.value != null && !Number.isNaN(p.value));
    return pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${xt(p.time).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ');
  };

  // Volume & RSI ranges
  const volMax = 1;    // no volume series in chart_server payload — skip pane
  const rsiY0 = padT + priceH + 8;
  const rsiSeriesFiltered = (data.rsi || []).filter(p => p.time >= tMin && p.time <= tMax);
  const rsiY = (v) => rsiY0 + rsiH - (v / 100) * rsiH;
  const rsiPath = rsiSeriesFiltered
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${xt(p.time).toFixed(1)},${rsiY(p.value).toFixed(1)}`)
    .join(' ');
  const rsiLast = rsiSeriesFiltered.length
    ? rsiSeriesFiltered[rsiSeriesFiltered.length - 1].value
    : (data.meta?.rsi ?? 50);

  // Live price line — color by sign of current candle (close vs open)
  const lastCandle = candles[candles.length - 1];
  const lastPx = liveMid != null ? liveMid : lastCandle.close;
  const lastPxY = y(lastPx);
  const livePosCol = '#4ade80', liveNegCol = '#f87171';
  const liveCol = lastPx >= lastCandle.open ? livePosCol : liveNegCol;

  // Regime ribbon segments — group consecutive bars of the same state.
  const regimeBars = (data.regime || []).filter(r => r.time >= tMin && r.time <= tMax);
  const ribbonY0 = rsiY0 - 6;
  const ribbonH = 4;

  // Position overlay lines (entry / SL / TPs)
  const pos = data.position;
  const inBand = (v) => v != null && v > lo && v < hi;

  // Per-symbol filter/RSI header data
  const filterDir = data.meta?.filter_dir || null;
  const filterTag = data.meta?.filter_variant || null;
  const dirColor = filterDir === 'up' ? '#4ade80' : filterDir === 'dn' ? '#f87171' : '#9ca3af';

  return (
    <div className={`chart-tile ${big ? 'chart-tile-big' : ''}`}>
      <div className="tile-head">
        <div className="tile-head-l">
          <span className="tile-sym">{meta.sym}</span>
          <span className={`tile-status tile-status-live`}>● LIVE</span>
          <span className={`tile-px ${pxFlash > 0 ? 'flash-up' : pxFlash < 0 ? 'flash-dn' : ''}`}>
            ${fmtP(lastPx)}
          </span>
          <span className="tile-rsi"><span className="dim">RSI</span> {rsiLast.toFixed(1)}</span>
          {filterTag && (
            <span className="dim small" style={{color: dirColor}}>
              {filterTag}={filterDir || '?'}
            </span>
          )}
          {pos && (
            <span className={`tile-side ${pos.side === 'long' ? 'pos' : 'neg'}`}>
              {pos.side.toUpperCase()}
            </span>
          )}
        </div>
        <div className="tile-head-r">
          {onExpand && <button className="tile-tv" title="Expand" onClick={onExpand}>⤢</button>}
        </div>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio={big ? "none" : "xMidYMid meet"}
        className="tile-svg"
        ref={svgRef}
        onWheel={(e) => {
          e.preventDefault();
          if (!view || !candles.length) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const fx = (e.clientX - rect.left) / rect.width;
          const len = view.end - view.start;
          const zoom = e.deltaY > 0 ? 1.15 : 1 / 1.15;
          const newLen = Math.max(10, Math.min(candles.length, Math.round(len * zoom)));
          const anchorIdx = view.start + fx * len;
          let ns = Math.round(anchorIdx - fx * newLen);
          let ne = ns + newLen;
          if (ns < 0) { ne -= ns; ns = 0; }
          setView({ start: ns, end: ne });
          setAutoFollow(ne >= candles.length);
        }}
        onMouseDown={(e) => {
          if (!view) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const sx = ((e.clientX - rect.left) / rect.width) * W;
          const sy = ((e.clientY - rect.top) / rect.height) * H;
          // Right gutter → drag vertically to stretch/compress y range.
          if (sx > W - padR) {
            axisDragRef.current = { kind: 'y', startY: e.clientY, origStretch: yStretch };
            return;
          }
          // Bottom strip (below priceH + rsiH) → drag horizontally to zoom x.
          if (sy > padT + priceH + rsiH - 4) {
            axisDragRef.current = {
              kind: 'x', startX: e.clientX, w: rect.width,
              origLen: view.end - view.start, origEnd: view.end,
            };
            return;
          }
          panRef.current = {
            startX: e.clientX, origStart: view.start, origEnd: view.end, w: rect.width,
          };
        }}
        onMouseMove={(e) => {
          if (axisDragRef.current && view) {
            const a = axisDragRef.current;
            if (a.kind === 'y') {
              // Down = more headroom (factor > 1); up = tighter (< 1).
              const dy = e.clientY - a.startY;
              setYStretch(Math.max(0.2, Math.min(6, a.origStretch * Math.pow(1.01, dy))));
            } else if (a.kind === 'x') {
              // Left = zoom in (fewer bars wider); right = zoom out.
              const dx = e.clientX - a.startX;
              const factor = Math.pow(1.005, dx);
              const newLen = Math.max(10, Math.min(candles.length * 2, Math.round(a.origLen * factor)));
              const ne = a.origEnd;
              setView({ start: Math.max(0, ne - newLen), end: ne });
              setAutoFollow(ne >= candles.length);
            }
            return;
          }
          if (!panRef.current || !view) return;
          const len = panRef.current.origEnd - panRef.current.origStart;
          const dx = e.clientX - panRef.current.startX;
          const shift = -Math.round((dx / panRef.current.w) * len);
          let ns = panRef.current.origStart + shift;
          let ne = panRef.current.origEnd + shift;
          if (ns < 0) { ne -= ns; ns = 0; }
          // Future whitespace — up to 1× view length past the latest bar.
          const maxFuture = panRef.current.origEnd - panRef.current.origStart;
          const hardRight = candles.length + maxFuture;
          if (ne > hardRight) { ns -= (ne - hardRight); ne = hardRight; }
          ns = Math.max(0, ns);
          setView({ start: ns, end: ne });
          setAutoFollow(ne >= candles.length);
        }}
        onMouseUp={() => { panRef.current = null; axisDragRef.current = null; }}
        onMouseLeave={() => { panRef.current = null; axisDragRef.current = null; }}
        onDoubleClick={(e) => {
          if (!candles || !candles.length) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const sx = ((e.clientX - rect.left) / rect.width) * W;
          // Dbl-click y-gutter → reset y stretch. Elsewhere → full reset.
          if (sx > W - padR) { setYStretch(1); return; }
          const ne = candles.length;
          setView({ start: Math.max(0, ne - 80), end: ne });
          setAutoFollow(true);
          setYStretch(1);
        }}
        style={{ cursor: panRef.current ? 'grabbing' : 'grab', userSelect: 'none', touchAction: 'none' }}
      >
        <defs>
          <pattern id={`grid-${meta.sym}`} width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M40 0 H0 V40" fill="none" stroke="#11181f" strokeWidth="0.5"/>
          </pattern>
        </defs>
        <rect x={padL} y={padT} width={iw} height={priceH} fill={`url(#grid-${meta.sym})`}/>

        {/* Regime ribbon between price pane and RSI pane */}
        {regimeBars.map((r, i) => {
          const nxt = regimeBars[i + 1];
          const x0 = xt(r.time);
          const x1 = nxt ? xt(nxt.time) : padL + iw;
          const col = r.state === 'up' ? 'rgba(34,197,94,0.55)'
                    : r.state === 'dn' ? 'rgba(239,68,68,0.55)'
                    : 'rgba(107,114,128,0.18)';
          return <rect key={i} x={x0} y={ribbonY0} width={Math.max(0.5, x1 - x0)} height={ribbonH} fill={col}/>;
        })}

        {/* Bollinger bands — dotted gray */}
        <path d={linePath(data.bb_upper)} fill="none" stroke="rgba(107,114,128,0.6)" strokeWidth="0.6" strokeDasharray="2,2"/>
        <path d={linePath(data.bb_lower)} fill="none" stroke="rgba(107,114,128,0.6)" strokeWidth="0.6" strokeDasharray="2,2"/>

        {/* EMAs */}
        <path d={linePath(data.ema9)}  fill="none" stroke="#f59e0b" strokeWidth="1"/>
        <path d={linePath(data.ema21)} fill="none" stroke="#60a5fa" strokeWidth="1"/>
        <path d={linePath(data.ema50)} fill="none" stroke="#a78bfa" strokeWidth="1"/>

        {/* BOS trigger levels — horizontal dashdot lines */}
        {inBand(data.bos?.long) && (
          <g>
            <line x1={padL} y1={y(data.bos.long)} x2={padL + iw} y2={y(data.bos.long)}
                  stroke="#fbbf24" strokeWidth="1" strokeDasharray="6,3,1,3"/>
            <text x={padL + 4} y={y(data.bos.long) - 3} fontSize="9" fill="#fbbf24">
              BOS↑ {fmtP(data.bos.long)}
            </text>
          </g>
        )}
        {inBand(data.bos?.short) && (
          <g>
            <line x1={padL} y1={y(data.bos.short)} x2={padL + iw} y2={y(data.bos.short)}
                  stroke="#a78bfa" strokeWidth="1" strokeDasharray="6,3,1,3"/>
            <text x={padL + 4} y={y(data.bos.short) + 11} fontSize="9" fill="#a78bfa">
              BOS↓ {fmtP(data.bos.short)}
            </text>
          </g>
        )}

        {/* Candles — darker green/red (matches chart_server.py) and the
            LAST candle re-wicks live: its close becomes the latest mid-price
            tick, and high/low expand to include the tick so the body and
            wick grow visibly on every price move. */}
        {visibleCandles.map((c, i) => {
          const xi = xt(c.time);
          const isLast = i === visibleCandles.length - 1 && liveMid != null;
          const cClose = isLast ? liveMid : c.close;
          const cHigh = isLast ? Math.max(c.high, liveMid) : c.high;
          const cLow  = isLast ? Math.min(c.low, liveMid)  : c.low;
          const up = cClose >= c.open;
          const col = up ? '#16a34a' : '#dc2626';
          const bodyTop = y(Math.max(c.open, cClose));
          const bodyBot = y(Math.min(c.open, cClose));
          return (
            <g key={c.time}>
              <line x1={xi} y1={y(cHigh)} x2={xi} y2={y(cLow)} stroke={col} strokeWidth={big ? 1 : 0.8}/>
              <rect x={xi - cw/2} y={bodyTop} width={cw} height={Math.max(1, bodyBot - bodyTop)} fill={col}/>
            </g>
          );
        })}

        {/* 1h pivot markers — small triangles */}
        {(data.pivots?.highs || []).filter(p => p.time >= tMin && p.time <= tMax && inBand(p.price)).map((p, i) => (
          <polygon key={`ph-${i}`}
            points={`${xt(p.time) - 4},${y(p.price) - 7} ${xt(p.time) + 4},${y(p.price) - 7} ${xt(p.time)},${y(p.price) - 1}`}
            fill="#ef4444"/>
        ))}
        {(data.pivots?.lows || []).filter(p => p.time >= tMin && p.time <= tMax && inBand(p.price)).map((p, i) => (
          <polygon key={`pl-${i}`}
            points={`${xt(p.time) - 4},${y(p.price) + 7} ${xt(p.time) + 4},${y(p.price) + 7} ${xt(p.time)},${y(p.price) + 1}`}
            fill="#22c55e"/>
        ))}

        {/* VALIDATED pivots — big SELL/BUY star markers */}
        {(data.valid_pivots?.highs || []).filter(p => p.time >= tMin && p.time <= tMax && inBand(p.price)).map((p, i) => (
          <g key={`vh-${i}`}>
            <circle cx={xt(p.time)} cy={y(p.price) - 10} r="6" fill="#fb7185" stroke="#fff" strokeWidth="1"/>
            <text x={xt(p.time)} y={y(p.price) - 18} textAnchor="middle" fontSize="9" fill="#fb7185" fontWeight="600">SELL</text>
          </g>
        ))}
        {(data.valid_pivots?.lows || []).filter(p => p.time >= tMin && p.time <= tMax && inBand(p.price)).map((p, i) => (
          <g key={`vl-${i}`}>
            <circle cx={xt(p.time)} cy={y(p.price) + 10} r="6" fill="#34d399" stroke="#fff" strokeWidth="1"/>
            <text x={xt(p.time)} y={y(p.price) + 22} textAnchor="middle" fontSize="9" fill="#34d399" fontWeight="600">BUY</text>
          </g>
        ))}

        {/* Position overlays — each level has:
              · horizontal line across the plot
              · left-side text label (e.g. "ENTRY 85.30") above the line
              · right-gutter tag block (matches the live-price chip style)
            Colors: entry=neutral white, SL=red, TP=green. */}
        {pos && (() => {
          const levelTag = (level, label, color, dash) => {
            if (!inBand(level)) return null;
            const ly = y(level);
            return (
              <g>
                <line x1={padL} y1={ly} x2={padL + iw} y2={ly}
                      stroke={color} strokeWidth="1" strokeDasharray={dash}/>
                <text x={padL + 4} y={ly - 3} fontSize="9" fill={color}>
                  {label} {fmtP(level)}
                </text>
                <rect x={padL + iw + 2} y={ly - 7} width={padR - 4} height="14"
                      fill="#0c1013" stroke={color} strokeWidth="0.75"/>
                <text x={padL + iw + padR/2} y={ly + 3} textAnchor="middle"
                      fill={color} fontSize="9">{fmtP(level)}</text>
              </g>
            );
          };
          return (
            <g>
              {levelTag(pos.entry, 'ENTRY', '#e5e7eb', '')}
              {levelTag(pos.sl, 'SL', '#ef4444', '4,3')}
              {pos.tp1 != null && !pos.tp1_hit && levelTag(pos.tp1, 'TP1', '#22c55e', '2,3')}
              {pos.tp2 != null && !pos.tp2_hit && levelTag(pos.tp2, 'TP2', '#22c55e', '2,3')}
              {pos.tp3 != null && !pos.tp3_hit && levelTag(pos.tp3, 'TP3', '#22c55e', '2,3')}
              {pos.trail_offset > 0 && (
                pos.trail_active && pos.best_price
                  ? levelTag(
                      pos.side === 'long'
                        ? pos.best_price - pos.trail_offset
                        : pos.best_price + pos.trail_offset,
                      'TRAIL', '#38bdf8', '5,2,1,2'
                    )
                  : levelTag(
                      pos.side === 'long'
                        ? pos.entry + pos.trail_offset
                        : pos.entry - pos.trail_offset,
                      'trail arm', '#0891b2', '1,3'
                    )
              )}
            </g>
          );
        })()}

        {/* Live price line + tag */}
        <line x1={padL} y1={lastPxY} x2={padL + iw} y2={lastPxY}
              stroke={liveCol} strokeWidth="0.75" strokeDasharray="2,3" opacity="0.9"/>
        <rect x={padL + iw + 2} y={lastPxY - 7} width={padR - 4} height="14"
              fill={liveCol === livePosCol ? '#0f2a1a' : '#2a1010'} stroke={liveCol} strokeWidth="0.75"/>
        <text x={padL + iw + padR/2} y={lastPxY + 3} textAnchor="middle"
              className="tile-tag-t" fill={liveCol}>{fmtP(lastPx)}</text>

        {/* RSI pane */}
        <rect x={padL} y={rsiY0} width={iw} height={rsiH} fill="#0c1013" opacity="0.5"/>
        {[30, 50, 70].map(v => (
          <line key={v} x1={padL} y1={rsiY(v)} x2={padL + iw} y2={rsiY(v)}
                stroke="#1f2937" strokeDasharray="2,3" strokeWidth="0.5"/>
        ))}
        <path d={rsiPath} fill="none" stroke="#a78bfa" strokeWidth="0.8"/>
      </svg>
    </div>
  );
}

Object.assign(window, { ChartTile });
