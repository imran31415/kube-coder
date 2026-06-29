/**
 * Design tokens for the kube-coder mobile app. A deep, premium dark theme that
 * mirrors the dashboard SPA but is tuned for an OLED-friendly native feel.
 */
export const colors = {
  bg: '#08090c',
  bgElevated: '#101319',
  card: '#13171e',
  cardHover: '#1a1f28',
  border: '#222833',
  borderStrong: '#323a47',
  text: '#eef1f5',
  textMuted: '#98a2b3',
  textFaint: '#5b6675',
  accent: '#5b8def',
  accentAlt: '#7c6cf0',
  accentText: '#ffffff',
  success: '#3fb950',
  warning: '#e3a008',
  danger: '#f0556a',
  // task status colours
  running: '#5b8def',
  waiting: '#e3a008',
  done: '#3fb950',
  error: '#f0556a',
  killed: '#7d8694',
} as const;

/** Gradients (consumed by expo-linear-gradient). */
export const gradients = {
  primary: ['#5b8def', '#7c6cf0'] as const,
  header: ['#13171e', '#0b0d12'] as const,
  brand: ['#5b8def', '#7c6cf0'] as const,
};

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
  xl: 22,
  pill: 999,
} as const;

export const font = {
  size: {
    xs: 11,
    sm: 13,
    md: 15,
    lg: 18,
    xl: 22,
    xxl: 30,
  },
  mono: 'monospace',
} as const;

export const shadow = {
  card: {
    shadowColor: '#000',
    shadowOpacity: 0.35,
    shadowRadius: 14,
    shadowOffset: { width: 0, height: 6 },
    elevation: 4,
  },
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
