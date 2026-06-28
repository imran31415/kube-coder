/**
 * Design tokens for the kube-coder mobile app. Mirrors the dark aesthetic of
 * the dashboard SPA (charts/workspace/web) so the two feel like one product.
 */
export const colors = {
  bg: '#0b0d10',
  bgElevated: '#14171c',
  card: '#171b21',
  cardHover: '#1d222a',
  border: '#262c35',
  borderStrong: '#333b47',
  text: '#e7ebf0',
  textMuted: '#9aa4b2',
  textFaint: '#5f6b7a',
  accent: '#4f9cf9',
  accentText: '#ffffff',
  success: '#3fb950',
  warning: '#d29922',
  danger: '#f85149',
  // task status colours
  running: '#4f9cf9',
  waiting: '#d29922',
  done: '#3fb950',
  error: '#f85149',
  killed: '#8b949e',
} as const;

export const space = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = {
  sm: 8,
  md: 12,
  lg: 16,
  pill: 999,
} as const;

export const font = {
  size: {
    xs: 11,
    sm: 13,
    md: 15,
    lg: 18,
    xl: 22,
    xxl: 28,
  },
  mono: 'monospace',
} as const;

export function statusColor(status: string): string {
  switch (status) {
    case 'running':
      return colors.running;
    case 'waiting':
    case 'waiting_input':
      return colors.waiting;
    case 'done':
    case 'completed':
      return colors.done;
    case 'error':
      return colors.error;
    case 'killed':
      return colors.killed;
    default:
      return colors.textMuted;
  }
}
