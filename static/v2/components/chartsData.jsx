// Chart indicators — math helpers consumed by ChartTile.
// No fake candle generator; tiles render a "no data" state when HL is
// unreachable rather than fabricating a curve.

function sma(arr, period) {
  const out = [];
  for (let i = 0; i < arr.length; i++) {
    if (i < period - 1) { out.push(null); continue; }
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) s += arr[j];
    out.push(s / period);
  }
  return out;
}

function ema(arr, period) {
  const out = [];
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < arr.length; i++) {
    if (prev === null) { prev = arr[i]; out.push(prev); continue; }
    prev = arr[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

function bollinger(arr, period = 20, mult = 2) {
  const mid = sma(arr, period);
  const up = [], dn = [];
  for (let i = 0; i < arr.length; i++) {
    if (mid[i] === null) { up.push(null); dn.push(null); continue; }
    let sq = 0;
    for (let j = i - period + 1; j <= i; j++) sq += (arr[j] - mid[i]) ** 2;
    const std = Math.sqrt(sq / period);
    up.push(mid[i] + std * mult);
    dn.push(mid[i] - std * mult);
  }
  return { mid, up, dn };
}

function rsiSeries(closes, period = 14) {
  const out = [];
  let gain = 0, loss = 0;
  for (let i = 0; i < closes.length; i++) {
    if (i === 0) { out.push(50); continue; }
    const ch = closes[i] - closes[i - 1];
    if (i <= period) {
      gain += Math.max(0, ch); loss += Math.max(0, -ch);
      if (i < period) { out.push(50); continue; }
      gain /= period; loss /= period;
    } else {
      gain = (gain * (period - 1) + Math.max(0, ch)) / period;
      loss = (loss * (period - 1) + Math.max(0, -ch)) / period;
    }
    const rs = loss === 0 ? 100 : gain / loss;
    out.push(100 - 100 / (1 + rs));
  }
  return out;
}

window.CHARTS_DATA = { sma, ema, bollinger, rsiSeries };
