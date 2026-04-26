// Claude-powered command bar (⌘K) and insights panel
const { useState: useStateK, useEffect: useEffectK, useRef: useRefK } = React;

function ClaudeBar({ open, setOpen, context }) {
  const [q, setQ] = useStateK('');
  const [a, setA] = useStateK('');
  const [loading, setLoading] = useStateK(false);
  const [history, setHistory] = useStateK([]);
  const inputRef = useRefK(null);

  useEffectK(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  const suggestions = [
    'summarize today\'s session in 2 lines',
    'what\'s my biggest risk right now?',
    'why is ZEC outperforming?',
    'should I cut any position?',
    'explain my drawdown',
  ];

  const ask = async (question) => {
    if (!question.trim()) return;
    setLoading(true); setA('');
    const prompt = `You are an algo trading assistant embedded in a terminal. Be concise, direct, and use trader language. No preamble. 2-4 short sentences max. Use plain text, no markdown.\n\nAccount snapshot:\n${context}\n\nTrader asks: ${question}`;
    try {
      const resp = await window.claude.complete(prompt);
      setA(resp);
      setHistory(h => [{ q: question, a: resp }, ...h].slice(0, 5));
    } catch (e) {
      setA('claude unreachable — ' + (e.message || 'unknown'));
    }
    setLoading(false);
  };

  if (!open) return null;

  return (
    <div className="cbar-overlay" onClick={() => setOpen(false)}>
      <div className="cbar" onClick={e => e.stopPropagation()}>
        <div className="cbar-head">
          <span className="cbar-prompt">claude ›</span>
          <input
            ref={inputRef}
            className="cbar-input"
            placeholder="ask about your session, positions, risk…"
            value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') { ask(q); setQ(''); }
              if (e.key === 'Escape') setOpen(false);
            }}
          />
          <span className="cbar-kbd">ESC</span>
        </div>
        {loading && <div className="cbar-loading"><span className="cbar-dots">▊▊▊</span> thinking…</div>}
        {a && !loading && (
          <div className="cbar-answer">
            <div className="cbar-ar">▸</div>
            <div className="cbar-at">{a}</div>
          </div>
        )}
        {!a && !loading && (
          <div className="cbar-sugg">
            <div className="cbar-sugg-lbl">try</div>
            {suggestions.map((s, i) => (
              <button key={i} className="cbar-sugg-btn" onClick={() => ask(s)}>{s}</button>
            ))}
          </div>
        )}
        {history.length > 0 && (
          <div className="cbar-hist">
            <div className="cbar-sugg-lbl">recent</div>
            {history.map((h, i) => (
              <button key={i} className="cbar-hist-row" onClick={() => { setQ(h.q); }}>
                <span className="dim">›</span> {h.q}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ClaudeInsight({ context, tick }) {
  const [insight, setInsight] = useStateK('loading session read…');
  const [loading, setLoading] = useStateK(true);

  useEffectK(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const prompt = `You are an algo trading terminal assistant. Produce ONE tight sentence (max 20 words) giving the trader the single most important thing to notice about their current session. Trader-speak, lowercase ok, no markdown, no preamble.\n\nSnapshot:\n${context}`;
      try {
        const resp = await window.claude.complete(prompt);
        if (!cancelled) setInsight(resp.trim());
      } catch (e) {
        if (!cancelled) setInsight('claude offline — check manually');
      }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [tick]);

  return (
    <div className="insight">
      <div className="insight-l">
        <span className="insight-mark">◆</span>
        <span className="insight-lbl">CLAUDE</span>
      </div>
      <div className="insight-t">
        {loading ? <span className="insight-loading">▊▊▊ reading session…</span> : insight}
      </div>
    </div>
  );
}

function HotkeyOverlay({ open, setOpen }) {
  if (!open) return null;
  const keys = [
    ['⌘K / /', 'open claude command bar'],
    ['1–6', 'switch lower tabs'],
    ['r', 'toggle live ticking'],
    ['o', 'open order drawer'],
    ['t', 'cycle theme'],
    ['?', 'this overlay'],
    ['esc', 'close any overlay'],
  ];
  return (
    <div className="hk-overlay" onClick={() => setOpen(false)}>
      <div className="hk-box" onClick={e => e.stopPropagation()}>
        <div className="hk-title">KEYBOARD</div>
        {keys.map(([k, d], i) => (
          <div key={i} className="hk-row">
            <span className="hk-k">{k}</span>
            <span className="hk-d">{d}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { ClaudeBar, ClaudeInsight, HotkeyOverlay });
