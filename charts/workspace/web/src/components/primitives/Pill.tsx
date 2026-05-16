import type { ComponentChildren } from 'preact';
import './Pill.css';

type Tone = 'neutral' | 'success' | 'warn' | 'danger' | 'info' | 'accent';

export function Pill({ tone = 'neutral', children, mono = false }: { tone?: Tone; mono?: boolean; children: ComponentChildren }) {
  return <span class={`pill pill-${tone} ${mono ? 'pill-mono' : ''}`}>{children}</span>;
}
