/**
 * Design tokens for the kube-coder mobile app.
 *
 * Mirrors the dashboard SPA's "Editorial" system (charts/workspace/web/src/
 * styles/tokens.css): a high-contrast neutral palette with NO chromatic accent —
 * white is the accent in this dark theme — 1px hairline borders, near-flat radii
 * (crisp sheets, not rounded chips), and almost no shadow (elevation is carried
 * by surface-colour steps + borders). Hierarchy comes from typography + space.
 */
import { Platform } from 'react-native';

export const colors = {
  bg: '#18181a', // page background (soft charcoal, not pure black, so surfaces sit above it)
  bgElevated: '#1f1f22', // surface — cards/panels/headers
  card: '#1f1f22', // surface
  cardHover: '#26262a', // surface-2 — inputs, hover fill, elevated
  surface2: '#26262a', // input bg / hover
  surface3: '#2f2f34', // highest surface / track
  border: '#2c2c30', // hairline border / divider
  borderStrong: '#424248', // stronger border (hover, emphasis)
  text: '#ededee', // primary text (off-white, not pure #fff)
  textMuted: '#aeaeb2', // secondary
  textFaint: '#82828a', // tertiary / placeholder / captions
  // The "accent" is achromatic: off-white fill with dark text on top. This is
  // the crux of the black/white theme — primary actions are inked rectangles.
  accent: '#ededee',
  accentAlt: '#ffffff', // accent-strong (hover / CTA border)
  accentSoft: 'rgba(237,237,238,0.08)', // selection / active-row wash
  accentText: '#18181a', // on-accent — text/icon sitting on an accent fill
  // Semantic colours: used as text + border tints, rarely as fills.
  success: '#34d399',
  warning: '#fbbf24',
  danger: '#f87171',
  info: '#60a5fa',
  // task status colours (the one place chromatic hues appear, as tints)
  running: '#60a5fa',
  waiting: '#fbbf24',
  done: '#34d399',
  error: '#f87171',
  killed: '#82828a',
} as const;

/**
 * Gradients (consumed by expo-linear-gradient). The editorial theme has no
 * gradient fills, so `primary`/`brand` are flattened to a solid accent — every
 * existing <LinearGradient> CTA/brand mark renders as a clean inked panel with
 * no call-site changes. `header` is a barely-there surface step.
 */
export const gradients = {
  primary: ['#ededee', '#ededee'] as const,
  header: ['#1f1f22', '#18181a'] as const,
  brand: ['#ededee', '#ededee'] as const,
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
  sm: 2, // inputs, chips, small buttons, segments
  md: 3, // buttons, cards, rows, toasts
  lg: 5, // big panels, sections, empty-state icon
  xl: 8, // modals / bottom sheets (the one softer corner)
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
  // Floating chrome: bottom sheets, modals, toasts (web --shadow-2).
  float: {
    shadowColor: '#000',
    shadowOpacity: 0.45,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 8 },
    elevation: 12,
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
