/**
 * Design tokens for the kube-coder mobile app.
 *
 * Mirrors the dashboard SPA's "Slate" system (charts/workspace/web/src/
 * styles/tokens.css, dark mode): a dark slate palette whose surfaces step
 * visibly apart, a single emerald accent, 1px hairline borders, 4–8px radii and
 * soft glows on live status. The desktop app renders this same UI, so these
 * values reach it too. Keep them in step with tokens.css.
 */
import { Platform } from 'react-native';

export const colors = {
  bg: '#12171f', // page background (slate, not pure black, so surfaces sit above it)
  bgElevated: '#1a202b', // surface — cards/panels/headers
  card: '#1a202b', // surface
  cardHover: '#222935', // surface-2 — inputs, hover fill, elevated
  surface2: '#222935', // input bg / hover
  surface3: '#2c3442', // highest surface / track
  border: '#323b4a', // hairline border / divider
  borderStrong: '#454f61', // stronger border (hover, emphasis)
  text: '#e8ebf1', // primary text
  textMuted: '#b4bccb', // secondary
  textFaint: '#97a0b1', // tertiary / placeholder / captions (≥4.5:1 on surface3)
  // Emerald accent: primary actions are emerald fills with near-black text.
  accent: '#10b981',
  accentAlt: '#34d399', // accent-strong (hover / CTA border)
  accentSoft: 'rgba(16,185,129,0.12)', // selection / active-row wash
  accentText: '#03140d', // on-accent — text/icon sitting on an accent fill
  // Semantic colours: used as text + border tints, rarely as fills.
  success: '#10b981',
  warning: '#f59e0b',
  danger: '#f87171',
  info: '#60a5fa',
  // Soft wash + hairline for the semantic hues above, so a tinted card or
  // badge never spells an rgba() literal at the call site — the same role
  // accentSoft plays for the achromatic accent. Keep the channels in step
  // with the hexes above if those ever change.
  dangerSoft: 'rgba(248,113,113,0.06)', // danger wash (#f87171)
  dangerBorder: 'rgba(248,113,113,0.35)',
  infoSoft: 'rgba(96,165,250,0.10)', // info wash (#60a5fa)
  infoBorder: 'rgba(96,165,250,0.45)',
  // task status colours (the one place chromatic hues appear, as tints)
  running: '#60a5fa',
  waiting: '#f59e0b',
  done: '#10b981',
  error: '#f87171',
  killed: '#97a0b1',
} as const;

/**
 * Gradients (consumed by expo-linear-gradient). The theme has no gradient
 * fills, so `primary`/`brand` are flattened to the solid emerald accent — every
 * existing <LinearGradient> CTA/brand mark renders as a flat accent panel with
 * no call-site changes. `header` is a barely-there surface step.
 */
export const gradients = {
  primary: ['#10b981', '#10b981'] as const,
  header: ['#1a202b', '#12171f'] as const,
  brand: ['#10b981', '#10b981'] as const,
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
  sm: 4, // inputs, chips, small buttons, segments
  md: 6, // buttons, cards, rows, toasts
  lg: 8, // big panels, sections, empty-state icon
  xl: 10, // modals / bottom sheets (the one softer corner)
  pill: 999,
} as const;

export const font = {
  size: {
    xs: 11,
    sm: 13,
    md: 15,
    lg: 18,
    xl: 20,
    xxl: 24,
  },
  // No custom sans bundled yet — the system sans (SF / Roboto) is a close
  // neutral stand-in for Geist. Mono is used decoratively (titles, counts,
  // paths); Menlo reads far better than bare 'monospace' on iOS.
  mono: Platform.select({ ios: 'Menlo', default: 'monospace' }) as string,
} as const;

export const shadow = {
  // Cards lean on hairline borders, not shadows — this is intentionally faint,
  // reserved for the few elements that genuinely float above the page.
  card: {
    shadowColor: '#000',
    shadowOpacity: 0.3,
    shadowRadius: 2,
    shadowOffset: { width: 0, height: 1 },
    elevation: 1,
  },
  // Glowing status dot (web --glow-*): pass the status colour as shadowColor.
  // iOS renders the halo; Android ignores coloured shadows, which is fine.
  glow: (color: string) => ({
    shadowColor: color,
    shadowOpacity: 0.65,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 0 },
  }),
  // Floating chrome: bottom sheets, modals, toasts (web --shadow-2).
  float: {
    shadowColor: '#000',
    shadowOpacity: 0.45,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 8 },
    elevation: 12,
  },
} as const;

/** Statuses that want attention or are in flight — their dots glow (web --glow-*). */
export function isLiveStatus(status: string): boolean {
  return ['running', 'waiting', 'waiting_input', 'error'].includes(status);
}

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
