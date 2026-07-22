import { describe, expect, it } from 'vitest';
import {
  stripForSpeech,
  speakableText,
  sentenceChunks,
  splitSentences,
  speakReplies,
  setSpeakReplies,
  SPEAK_KEY,
} from './voice';
import type { Block } from './transcript';

describe('stripForSpeech', () => {
  it('drops fenced code blocks entirely', () => {
    const out = stripForSpeech('Run this:\n```bash\nls -la\n```\nThen check.');
    expect(out).not.toContain('ls -la');
    expect(out).toContain('Run this:');
    expect(out).toContain('Then check.');
  });

  it('drops an unterminated (still-streaming) fence', () => {
    const out = stripForSpeech('Here you go:\n```python\nprint(1)');
    expect(out).toBe('Here you go:');
  });

  it('collapses links and images to their text', () => {
    expect(stripForSpeech('See [the docs](https://x.y/z).')).toBe('See the docs.');
    expect(stripForSpeech('![screenshot](img.png) done')).toBe('screenshot done');
  });

  it('unwraps inline code and emphasis', () => {
    expect(stripForSpeech('Use `make ship` to **deploy** _now_')).toBe(
      'Use make ship to deploy now',
    );
  });

  it('silences headings, bullets, blockquotes and table pipes', () => {
    const md = '## Plan\n- first step\n1. second step\n> quoted\n| a | b |\n---';
    const out = stripForSpeech(md);
    expect(out).not.toContain('#');
    expect(out).not.toContain('|');
    expect(out).not.toContain('---');
    expect(out).toContain('Plan');
    expect(out).toContain('first step');
    expect(out).toContain('second step');
    expect(out).toContain('quoted');
  });
});

describe('speakableText', () => {
  it('speaks prose blocks only — tool activity and embeds are visual', () => {
    const blocks: Block[] = [
      { kind: 'prose', text: 'Deployed the app.' },
      { kind: 'activity', label: 'Bash', detail: 'kubectl get pods' },
      { kind: 'embed', port: 3000 },
      { kind: 'prose', text: 'It is live on port 3000.' },
    ];
    expect(speakableText(blocks)).toBe('Deployed the app.\nIt is live on port 3000.');
  });

  it('is empty for a tool-only turn', () => {
    expect(speakableText([{ kind: 'activity', label: 'Read', detail: 'x' }])).toBe('');
  });
});

describe('sentenceChunks', () => {
  it('holds back a trailing partial sentence', () => {
    const { complete, remainder } = sentenceChunks('First done. Second is still typ');
    expect(complete).toBe('First done. ');
    expect(remainder).toBe('Second is still typ');
  });

  it('treats end-of-input punctuation as a boundary', () => {
    const { complete, remainder } = sentenceChunks('All finished!');
    expect(complete).toBe('All finished!');
    expect(remainder).toBe('');
  });

  it('does not split on decimal points or versions', () => {
    const { complete, remainder } = sentenceChunks('Upgrade to v1.2 tomorrow');
    expect(complete).toBe('');
    expect(remainder).toBe('Upgrade to v1.2 tomorrow');
  });

  it('splits at newlines even without punctuation', () => {
    const { complete } = sentenceChunks('line one\nline two');
    expect(complete).toBe('line one\n');
  });
});

describe('splitSentences', () => {
  it('yields one entry per sentence', () => {
    expect(splitSentences('One. Two! Three?')).toEqual(['One.', 'Two!', 'Three?']);
  });

  it('keeps an unterminated tail as its own entry', () => {
    expect(splitSentences('Done. still going')).toEqual(['Done.', 'still going']);
  });

  it('splits on newlines and drops blanks', () => {
    expect(splitSentences('alpha\n\nbeta')).toEqual(['alpha', 'beta']);
  });

  it('is empty for whitespace-only input', () => {
    expect(splitSentences('   \n ')).toEqual([]);
  });
});

describe('speakReplies preference', () => {
  it('persists the toggle to localStorage', () => {
    setSpeakReplies(true);
    expect(speakReplies.value).toBe(true);
    expect(localStorage.getItem(SPEAK_KEY)).toBe('1');
    setSpeakReplies(false);
    expect(speakReplies.value).toBe(false);
    expect(localStorage.getItem(SPEAK_KEY)).toBe('0');
  });
});
