// Primitive building blocks
const { useState, useEffect, useRef, useMemo } = React;

function fmt(n, d = 2) {
  if (n === null || n === undefined) return '—';
  const abs = Math.abs(n);
  const dd = d ?? (abs >= 1000 ? 2 : abs >= 1 ? 4 : 6);
  return n.toLocaleString('en-US', { minimumFractionDigits: dd, maximumFractionDigits: dd });
}
function fmtUsd(n, d = 2, sign = false) {
  if (n === null || n === undefined) return '—';
  const s = (sign && n > 0 ? '+' : '') + '$' + fmt(Math.abs(n), d);
  return n < 0 ? '-$' + fmt(Math.abs(n), d) : s;
}
function fmtPct(n, d = 2) {
  if (n === null || n === undefined) return '—';
  return (n > 0 ? '+' : '') + n.toFixed(d) + '%';
}

function ColorNum({ value, children, kind = 'auto' }) {
  const cls = kind === 'auto'
    ? (value > 0 ? 'pos' : value < 0 ? 'neg' : 'dim')
    : kind;
  return <span className={`num ${cls}`}>{children}</span>;
}

// Tiny status dot with pulsing animation
function Pulse({ color = 'var(--pos)' }) {
  return <span className="pulse-dot" style={{ '--pulse': color }} />;
}

// SECTION label — small-caps dim
function Label({ children, right }) {
  return (
    <div className="lbl-row">
      <span className="lbl">{children}</span>
      {right && <span className="lbl-r">{right}</span>}
    </div>
  );
}

// Hairline divider
function HR({ v }) {
  return <div className={v ? 'hr-v' : 'hr-h'} />;
}

// Cross-hatch placeholder logo (original geometric mark)
function BrandMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" className="brand-mark">
      <rect x="1" y="1" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1"/>
      <path d="M1 1 L17 17 M17 1 L1 17" stroke="currentColor" strokeWidth="1"/>
      <rect x="6" y="6" width="6" height="6" fill="currentColor"/>
    </svg>
  );
}

Object.assign(window, { fmt, fmtUsd, fmtPct, ColorNum, Pulse, Label, HR, BrandMark });
