// Hyperliquid live data — REST snapshot + WS stream
// Maps our demo symbols to real HL perp coins
window.HL_SYMBOLS = ['HYPE', 'BTC', 'ETH', 'SOL', 'ARB', 'AVAX'];

const HL_REST = 'https://api.hyperliquid.xyz/info';
const HL_WS = 'wss://api.hyperliquid.xyz/ws';

// Fetch initial candle snapshot
async function hlFetchCandles(coin, interval = '5m', lookbackMs = 7 * 60 * 60 * 1000) {
  const now = Date.now();
  try {
    const r = await fetch(HL_REST, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: 'candleSnapshot',
        req: { coin, interval, startTime: now - lookbackMs, endTime: now }
      })
    });
    if (!r.ok) throw new Error(`HL ${r.status}`);
    const data = await r.json();
    return data.map(c => ({
      t: c.t, T: c.T,
      o: parseFloat(c.o), h: parseFloat(c.h),
      l: parseFloat(c.l), c: parseFloat(c.c),
      v: parseFloat(c.v), n: c.n,
    }));
  } catch (e) {
    console.warn('[hl] fetch', coin, e.message);
    return null;
  }
}

// WS manager — singleton, subscribes to multiple candle streams
class HLWebSocket {
  constructor() {
    this.ws = null;
    this.subs = new Map(); // "COIN|interval" -> Set of callbacks
    this.midSubs = new Map(); // coin -> Set of callbacks (live price ticks)
    this.midsSubscribed = false;
    this.ready = false;
    this.pending = [];
    this.connect();
  }
  connect() {
    try {
      this.ws = new WebSocket(HL_WS);
      this.ws.onopen = () => {
        this.ready = true;
        // Resubscribe everything
        for (const key of this.subs.keys()) {
          const [coin, interval] = key.split('|');
          this._send({ method: 'subscribe', subscription: { type: 'candle', coin, interval } });
        }
        if (this.midsSubscribed) {
          this._send({ method: 'subscribe', subscription: { type: 'allMids' } });
        }
        while (this.pending.length) this._send(this.pending.shift());
      };
      this.ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.channel === 'allMids' && msg.data && msg.data.mids) {
            const mids = msg.data.mids;
            for (const [coin, cbs] of this.midSubs.entries()) {
              const px = mids[coin];
              if (px !== undefined) {
                const p = parseFloat(px);
                cbs.forEach(cb => cb(p));
              }
            }
          }
          if (msg.channel === 'candle' && msg.data) {
            const d = msg.data;
            const key = `${d.s}|${d.i}`;
            const callbacks = this.subs.get(key);
            if (callbacks) {
              const candle = {
                t: d.t, T: d.T,
                o: parseFloat(d.o), h: parseFloat(d.h),
                l: parseFloat(d.l), c: parseFloat(d.c),
                v: parseFloat(d.v), n: d.n,
              };
              callbacks.forEach(cb => cb(candle));
            }
          }
        } catch (e) {}
      };
      this.ws.onclose = () => {
        this.ready = false;
        setTimeout(() => this.connect(), 2000);
      };
      this.ws.onerror = () => {};
    } catch (e) {
      console.warn('[hl] ws', e);
    }
  }
  _send(msg) {
    if (this.ready && this.ws.readyState === 1) this.ws.send(JSON.stringify(msg));
    else this.pending.push(msg);
  }
  subscribeMid(coin, cb) {
    if (!this.midsSubscribed) {
      this.midsSubscribed = true;
      this._send({ method: 'subscribe', subscription: { type: 'allMids' } });
    }
    if (!this.midSubs.has(coin)) this.midSubs.set(coin, new Set());
    this.midSubs.get(coin).add(cb);
    return () => {
      const s = this.midSubs.get(coin);
      if (s) { s.delete(cb); if (s.size === 0) this.midSubs.delete(coin); }
    };
  }
  subscribe(coin, interval, cb) {
    const key = `${coin}|${interval}`;
    if (!this.subs.has(key)) {
      this.subs.set(key, new Set());
      this._send({ method: 'subscribe', subscription: { type: 'candle', coin, interval } });
    }
    this.subs.get(key).add(cb);
    return () => {
      const s = this.subs.get(key);
      if (s) {
        s.delete(cb);
        if (s.size === 0) {
          this.subs.delete(key);
          this._send({ method: 'unsubscribe', subscription: { type: 'candle', coin, interval } });
        }
      }
    };
  }
}

window.HL = {
  fetchCandles: hlFetchCandles,
  ws: new HLWebSocket(),
};
