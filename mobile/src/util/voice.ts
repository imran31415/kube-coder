/**
 * Voice helpers for the Hypervisor chat (issue #396) — the mobile counterpart
 * of the dashboard's web/src/routes/hypervisor/voice.ts. Speech-to-text goes
 * through the server (POST /api/hypervisor/transcribe — React Native has no
 * SpeechRecognition), text-to-speech is on-device via expo-speech. The pure
 * text logic (markdown stripping, sentence chunking) is kept line-for-line in
 * sync with the web module so both clients narrate replies identically.
 */
import * as Speech from 'expo-speech';
import type { HvBlock } from './hvTranscript';
import { getItem, setItem } from '../store/storage';

// Same key as the web dashboard's localStorage pref — a per-device
// presentation choice, not workspace state.
export const SPEAK_PREF_KEY = 'kc.hv.speak';

export async function readSpeakPref(): Promise<boolean> {
  return (await getItem(SPEAK_PREF_KEY)) === '1';
}

export async function writeSpeakPref(on: boolean): Promise<void> {
  await setItem(SPEAK_PREF_KEY, on ? '1' : '0');
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
export function speakableText(blocks: HvBlock[]): string {
  return blocks
    .filter((b): b is Extract<HvBlock, { kind: 'prose' }> => b.kind === 'prose')
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

/** Sentence-sized pieces for utterances — per-sentence queueing starts sooner
 *  and expo-speech queues successive speak() calls natively. */
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
  for (const sentence of splitSentences(text)) {
    try {
      Speech.speak(sentence);
    } catch {
      /* engine unavailable — stay silent rather than break the chat */
    }
  }
}

export function stopSpeaking(): void {
  try {
    void Speech.stop();
  } catch {
    /* noop */
  }
}
