import type { JSX } from 'preact';

/** Minimal icon system — single-color stroke icons, 16px default.
 *  Inline so we don't ship an icon font/package.
 *  Stroke uses `currentColor`; size via the `size` prop.
 */
export type IconName =
  | 'tasks'
  | 'memory'
  | 'skills'
  | 'triggers'
  | 'files'
  | 'settings'
  | 'docs'
  | 'search'
  | 'more'
  | 'close'
  | 'check'
  | 'sun'
  | 'moon'
  | 'plus'
  | 'play'
  | 'kill'
  | 'github'
  | 'chevron-right'
  | 'chevron-left'
  | 'chevron-down'
  | 'fullscreen'
  | 'fullscreen-exit'
  | 'inbox'
  | 'chat'
  | 'terminal'
  | 'link'
  | 'unlink'
  | 'download'
  | 'upload'
  | 'apps'
  | 'image'
  | 'trash'
  | 'pencil'
  | 'info'
  | 'hypervisor'
  | 'walkie'
  | 'desktop';

const PATHS: Record<IconName, JSX.Element> = {
  tasks: (
    <>
      <path d="M3 6h14M3 10h14M3 14h9" />
      <circle cx="14" cy="14" r="2" />
    </>
  ),
  memory: (
    <>
      <rect x="4" y="3" width="12" height="14" rx="2" />
      <path d="M7 7h6M7 10h6M7 13h4" />
    </>
  ),
  // Four-point spark — "capability" — distinct from triggers' bolt shape.
  skills: (
    <>
      <path d="M10 2l1.8 6.2L18 10l-6.2 1.8L10 18l-1.8-6.2L2 10l6.2-1.8z" />
    </>
  ),
  triggers: (
    <>
      <path d="M10 2v6M10 12v6" />
      <path d="M3 7l3 3-3 3M17 7l-3 3 3 3" />
    </>
  ),
  files: (
    <>
      <path d="M4 4h5l2 2h5v9a1 1 0 0 1-1 1H4z" />
    </>
  ),
  settings: (
    <>
      <circle cx="10" cy="10" r="2.5" />
      <path d="M10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.5 1.5M13.5 13.5L15 15M5 15l1.5-1.5M13.5 6.5L15 5" />
    </>
  ),
  docs: (
    <>
      <path d="M4 3h7l3 3v11H4z" />
      <path d="M11 3v3h3" />
      <path d="M6 9h6M6 12h6M6 15h4" />
    </>
  ),
  search: (
    <>
      <circle cx="9" cy="9" r="5" />
      <path d="M13 13l4 4" />
    </>
  ),
  more: (
    <>
      <circle cx="4" cy="10" r="1.2" />
      <circle cx="10" cy="10" r="1.2" />
      <circle cx="16" cy="10" r="1.2" />
    </>
  ),
  close: (
    <>
      <path d="M5 5l10 10M15 5L5 15" />
    </>
  ),
  check: (
    <>
      <path d="M4 10l4 4 8-8" />
    </>
  ),
  sun: (
    <>
      <circle cx="10" cy="10" r="3" />
      <path d="M10 2v2M10 16v2M2 10h2M16 10h2M5 5l1.5 1.5M13.5 13.5L15 15M5 15l1.5-1.5M13.5 6.5L15 5" />
    </>
  ),
  moon: (
    <>
      <path d="M16 12a6 6 0 1 1-8-8 6 6 0 0 0 8 8z" />
    </>
  ),
  plus: (
    <>
      <path d="M10 4v12M4 10h12" />
    </>
  ),
  play: (
    <>
      <path d="M6 4l9 6-9 6z" />
    </>
  ),
  kill: (
    <>
      <rect x="5" y="5" width="10" height="10" rx="1" />
    </>
  ),
  github: (
    <>
      <path d="M10 2a8 8 0 0 0-2.5 15.6c.4.1.5-.2.5-.4v-1.4c-2.2.5-2.7-1-2.7-1-.4-.9-1-1.2-1-1.2-.8-.5.1-.5.1-.5.9.1 1.3.9 1.3.9.8 1.3 2 1 2.5.7.1-.6.3-1 .6-1.2-1.8-.2-3.6-.9-3.6-3.9 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8a7.5 7.5 0 0 1 4 0c1.5-1 2.2-.8 2.2-.8.4 1.1.2 1.9.1 2.1.5.5.8 1.2.8 2.1 0 3-1.8 3.7-3.6 3.9.3.2.5.7.5 1.4v2.1c0 .2.1.5.6.4A8 8 0 0 0 10 2z" />
    </>
  ),
  'chevron-right': (
    <>
      <path d="M8 5l5 5-5 5" />
    </>
  ),
  'chevron-left': (
    <>
      <path d="M12 5l-5 5 5 5" />
    </>
  ),
  'chevron-down': (
    <>
      <path d="M5 8l5 5 5-5" />
    </>
  ),
  fullscreen: (
    <>
      <path d="M3 7V3h4M17 7V3h-4M3 13v4h4M17 13v4h-4" />
    </>
  ),
  'fullscreen-exit': (
    <>
      <path d="M7 3v4H3M13 3v4h4M7 17v-4H3M13 17v-4h4" />
    </>
  ),
  inbox: (
    <>
      <path d="M3 11h4l1 2h4l1-2h4M3 11l2-6h10l2 6v6H3z" />
    </>
  ),
  chat: (
    <>
      <path d="M4 4h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H8l-4 3z" />
    </>
  ),
  terminal: (
    <>
      <rect x="3" y="4" width="14" height="12" rx="1" />
      <path d="M6 9l2 2-2 2M10 13h4" />
    </>
  ),
  // Chip/CPU glyph with pins — the "workspace hypervisor" mark.
  hypervisor: (
    <>
      <rect x="5" y="5" width="10" height="10" rx="2" />
      <rect x="8" y="8" width="4" height="4" rx="1" />
      <path d="M8 2v3M12 2v3M8 15v3M12 15v3M2 8h3M2 12h3M15 8h3M15 12h3" />
    </>
  ),
  link: (
    <>
      <path d="M9 11l-2 2a3 3 0 0 1-4-4l2-2M11 9l2-2a3 3 0 0 1 4 4l-2 2M7 13l6-6" />
    </>
  ),
  // Handheld radio — antenna + body + PTT grille — the Walkie-Talkie mark.
  walkie: (
    <>
      <path d="M13 3l3 3" />
      <rect x="5" y="6" width="8" height="12" rx="1.5" />
      <path d="M7 9h4M9 12v4" />
    </>
  ),
  unlink: (
    <>
      <path d="M8 12l-1 1a3 3 0 0 1-4-4l2-2M12 8l1-1a3 3 0 0 1 4 4l-1 1M14 3v2M17 6h-2M3 17l2-2" />
    </>
  ),
  download: (
    <>
      <path d="M10 3v9M6 9l4 4 4-4M4 16h12" />
    </>
  ),
  upload: (
    <>
      <path d="M10 13V4M6 8l4-4 4 4M4 16h12" />
    </>
  ),
  desktop: (
    <>
      <rect x="3" y="4" width="14" height="9" rx="1" />
      <path d="M7 17h6M10 13v4" />
    </>
  ),
  apps: (
    <>
      <rect x="3" y="3" width="6" height="6" rx="1" />
      <rect x="11" y="3" width="6" height="6" rx="1" />
      <rect x="3" y="11" width="6" height="6" rx="1" />
      <rect x="11" y="11" width="6" height="6" rx="1" />
    </>
  ),
  image: (
    <>
      <rect x="3" y="4" width="14" height="12" rx="1.5" />
      <circle cx="7.5" cy="8.5" r="1.5" />
      <path d="M4 15l4-4 3 3 3-3 3 3" />
    </>
  ),
  trash: (
    <>
      <path d="M4 6h12M8 6V4h4v2M6 6l1 10h6l1-10" />
    </>
  ),
  pencil: (
    <>
      <path d="M4 16l1-3 8-8 2 2-8 8-3 1z" />
    </>
  ),
  // Circled "i" — a lightweight help/info affordance.
  info: (
    <>
      <circle cx="10" cy="10" r="7.5" />
      <path d="M10 13.5V9" />
      <path d="M10 6.6h0.01" />
    </>
  ),
};

export function Icon({ name, size = 16, stroke = 1.6, class: klass }: { name: IconName; size?: number; stroke?: number; class?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      stroke-width={stroke}
      stroke-linecap="round"
      stroke-linejoin="round"
      class={klass}
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}
