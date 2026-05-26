/** Small display formatters for the metrics panel. */

export function fmtBytes(b: number | null): string {
  if (b == null) return '—';
  if (b >= 1e9) return (b / 1e9).toFixed(2) + ' GB';
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB';
  if (b >= 1e3) return (b / 1e3).toFixed(0) + ' KB';
  return Math.round(b) + ' B';
}

export function fmtCores(c: number | null): string {
  if (c == null) return '—';
  return c >= 0.1 ? c.toFixed(2) + ' cores' : Math.round(c * 1000) + ' mcores';
}

export function fmtPct(p: number | null): string {
  return p == null ? '' : Math.round(p) + '%';
}

export function fmtUptime(s: number | null): string {
  if (s == null) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

export function fmtRate(b: number | null): string {
  if (b == null) return '—';
  if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB/s';
  if (b >= 1e3) return (b / 1e3).toFixed(0) + ' KB/s';
  return Math.round(b) + ' B/s';
}

export function fmtUsd(n: number): string {
  return '$' + n.toFixed(2);
}

/** Threshold colour for a utilisation %, mirroring the workspace dashboard. */
export function tone(pct: number | null, warn: number, crit: number): 'ok' | 'warn' | 'crit' {
  if (pct == null) return 'ok';
  if (pct >= crit) return 'crit';
  if (pct >= warn) return 'warn';
  return 'ok';
}
