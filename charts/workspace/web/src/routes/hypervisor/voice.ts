import { signal } from '@preact/signals';
import type { Block } from './transcript';

/**
 * Voice layer for the Hypervisor chat (issue #396) — tier 0, browser-only:
 * `SpeechRecognition` for push-to-talk input and `speechSynthesis` for spoken
 * replies. No server round-trip, no API key. Everything is feature-detected,
 * so browsers without the APIs (or a plain-HTTP page, where the mic is
 * blocked) simply don't show the controls. Provider-backed STT/TTS (tier 1)
 * can layer on top later without changing this surface.
 */

// ── SpeechRecognition (STT) ──────────────────────────────────────────────────
// lib.dom ships SpeechSynthesis but not SpeechRecognition (still prefixed in
// Chromium), so we carry minimal structural types of our own.

export interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}

export interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}

export interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onerror: ((e: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

/** The browser's recognition constructor, or null when unsupported. */
export function recognitionCtor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as Record<string, unknown>;
  const ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  return typeof ctor === 'function' ? (ctor as new () => SpeechRecognitionLike) : null;
}

export function sttSupported(): boolean {
  return recognitionCtor() !== null;
}

// ── speak-replies preference ─────────────────────────────────────────────────

export const SPEAK_KEY = 'kc.hv.speak';

function readSpeakPref(): boolean {
  try {
    return localStorage.getItem(SPEAK_KEY) === '1';
  } catch {
    return false;
  }
}

/** Whether agent replies are read aloud. Persists per browser, like the
 *  sidebar width — a device-local presentation choice, not workspace state. */
export const speakReplies = signal<boolean>(readSpeakPref());

export function setSpeakReplies(on: boolean): void {
  speakReplies.value = on;
  try {
    localStorage.setItem(SPEAK_KEY, on ? '1' : '0');
  } catch {
    /* private mode — the toggle still works for this page */
  }
  if (!on) stopSpeaking();
}

// ── speechSynthesis (TTS) ────────────────────────────────────────────────────

export function ttsSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window;
}

/** Reduce markdown to something worth hearing: code blocks are dropped (their
 *  prose context already narrates them), links/images collapse to their text,
 *  and list/heading/emphasis markers go silent. */
export function stripForSpeech(md: string): string {
  return (
    md
      .replace(/```[\s\S]*?(?:```|$)/g, ' ')
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/`([^`]*)`/g, '$1')
      // Emphasis/strike markers around words — keep the words.
      .replace(/(\*\*|__|~~)(.*?)\1/g, '$2')
      .replace(/(^|\s)[*_]([^*_]+)[*_](?=\s|[.,!?;:]|$)/g, '$1$2')
      // Line-leading structure: headings, blockquotes, bullets, numbering.
      .replace(/^[ \t]*#{1,6}[ \t]+/gm, '')
      .replace(/^[ \t]*>[ \t]?/gm, '')
      .replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+/gm, '')
      // Table pipes and horizontal rules read as noise.
      .replace(/^[ \t]*[-=_*]{3,}[ \t]*$/gm, ' ')
      .replace(/\|/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .trim()
  );
}

/** The spoken text of an agent turn: its prose blocks only — tool activity,
 *  embeds and file previews are visual. */
export function speakableText(blocks: Block[]): string {
  return blocks
    .filter((b): b is Extract<Block, { kind: 'prose' }> => b.kind === 'prose')
    .map((b) => stripForSpeech(b.text))
    .filter(Boolean)
    .join('\n');
}

// A sentence ends at ./!/?/… (plus any closing quote/bracket) followed by
// whitespace or end-of-input, or at a newline. "v1.2" doesn't match because
// the dot isn't followed by whitespace.
const SENTENCE_END = /[.!?…]["')\]]*(?:\s+|$)|\n+/g;

/** Split streaming text at the last sentence boundary, so playback can start
 *  before the turn finishes: `complete` is safe to speak now, `remainder` is a
 *  still-growing tail to hold back until more text (or the end) arrives. */
export function sentenceChunks(text: string): { complete: string; remainder: string } {
  SENTENCE_END.lastIndex = 0;
  let cut = 0;
  for (let m = SENTENCE_END.exec(text); m !== null; m = SENTENCE_END.exec(text)) {
    cut = m.index + m[0].length;
  }
  return { complete: text.slice(0, cut), remainder: text.slice(cut) };
}

/** Sentence-sized pieces for utterances — some engines cut long single
 *  utterances off mid-stream, and per-sentence queueing starts sooner. */
export function splitSentences(text: string): string[] {
  const out: string[] = [];
  SENTENCE_END.lastIndex = 0;
  let prev = 0;
  for (let m = SENTENCE_END.exec(text); m !== null; m = SENTENCE_END.exec(text)) {
    const piece = text.slice(prev, m.index + m[0].length).trim();
    if (piece) out.push(piece);
    prev = m.index + m[0].length;
  }
  const tail = text.slice(prev).trim();
  if (tail) out.push(tail);
  return out;
}

/** Queue text for speech. Additive — call repeatedly as sentences complete. */
export function speakText(text: string): void {
  if (!ttsSupported()) return;
  for (const sentence of splitSentences(text)) {
    try {
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(sentence));
    } catch {
      /* engine unavailable — stay silent rather than break the chat */
    }
  }
}

export function stopSpeaking(): void {
  if (!ttsSupported()) return;
  try {
    window.speechSynthesis.cancel();
  } catch {
    /* noop */
  }
}
