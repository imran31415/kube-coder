import { describe, expect, it } from 'vitest';
import {
  transition,
  levelFromTimeDomain,
  levelFromDb,
  smoothLevel,
  orbCopy,
  orbMood,
  type VoicePhase,
} from './walkieVoice';

const OK = { available: true, linked: true, stt: true, micDenied: false };

describe('transition', () => {
  it('walks the happy path: idle → listening → transcribing → sending → thinking → speaking → idle', () => {
    let p: VoicePhase = 'idle';
    p = transition(p, 'press');
    expect(p).toBe('listening');
    p = transition(p, 'press');
    expect(p).toBe('transcribing');
    p = transition(p, 'captured');
    expect(p).toBe('sending');
    p = transition(p, 'sent');
    expect(p).toBe('thinking');
    p = transition(p, 'reply');
    expect(p).toBe('speaking');
    p = transition(p, 'quiet');
    expect(p).toBe('idle');
  });

  it('supports capture that ends on its own (silence timeout): listening + captured → sending', () => {
    expect(transition('listening', 'captured')).toBe('sending');
  });

  it('returns to idle when nothing was heard', () => {
    expect(transition('listening', 'empty')).toBe('idle');
    expect(transition('transcribing', 'empty')).toBe('idle');
  });

  it('barge-in: pressing while speaking starts listening', () => {
    expect(transition('speaking', 'press')).toBe('listening');
  });

  it('allows talking over a thinking agent', () => {
    expect(transition('thinking', 'press')).toBe('listening');
  });

  it('ignores presses while capture/send is settling (no double-send)', () => {
    expect(transition('transcribing', 'press')).toBe('transcribing');
    expect(transition('sending', 'press')).toBe('sending');
  });

  it('gateway busy pulls settled phases into thinking but never an active capture', () => {
    expect(transition('idle', 'busy')).toBe('thinking');
    expect(transition('speaking', 'busy')).toBe('thinking');
    expect(transition('listening', 'busy')).toBe('listening');
    expect(transition('transcribing', 'busy')).toBe('transcribing');
  });

  it('a reply that lands mid-capture does not steal the mic', () => {
    expect(transition('listening', 'reply')).toBe('listening');
    expect(transition('transcribing', 'reply')).toBe('transcribing');
    expect(transition('thinking', 'reply')).toBe('speaking');
  });

  it('quiet settles thinking (nothing to narrate) and speaking, not capture', () => {
    expect(transition('thinking', 'quiet')).toBe('idle');
    expect(transition('speaking', 'quiet')).toBe('idle');
    expect(transition('listening', 'quiet')).toBe('listening');
  });

  it('cancel resets any phase to idle', () => {
    const phases: VoicePhase[] = [
      'idle',
      'listening',
      'transcribing',
      'sending',
      'thinking',
      'speaking',
    ];
    for (const p of phases) expect(transition(p, 'cancel')).toBe('idle');
  });
});

describe('levelFromTimeDomain', () => {
  it('is 0 for silence (all samples at the 128 midline) and empty buffers', () => {
    expect(levelFromTimeDomain(new Uint8Array(0))).toBe(0);
    expect(levelFromTimeDomain(new Uint8Array(256).fill(128))).toBe(0);
  });

  it('clamps a full-scale square wave to 1', () => {
    const loud = new Uint8Array(256);
    for (let i = 0; i < loud.length; i++) loud[i] = i % 2 ? 255 : 0;
    expect(levelFromTimeDomain(loud)).toBe(1);
  });

  it('expands quiet speech into a visible range and keeps ordering', () => {
    const at = (amp: number) => {
      const buf = new Uint8Array(256);
      for (let i = 0; i < buf.length; i++) buf[i] = 128 + (i % 2 ? amp : -amp);
      return levelFromTimeDomain(buf);
    };
    const quiet = at(6); // ~0.05 RMS — soft speech
    const normal = at(26); // ~0.2 RMS — normal speech
    expect(quiet).toBeGreaterThan(0.25); // visible, not hugging zero
    expect(normal).toBeGreaterThan(quiet);
    expect(normal).toBeLessThanOrEqual(1);
  });
});

describe('levelFromDb', () => {
  it('maps the floor and below to 0 and full scale to 1', () => {
    expect(levelFromDb(-60)).toBe(0);
    expect(levelFromDb(-120)).toBe(0);
    expect(levelFromDb(0)).toBe(1);
    expect(levelFromDb(3)).toBe(1);
  });

  it('is linear in dB between floor and full scale', () => {
    expect(levelFromDb(-30)).toBeCloseTo(0.5);
    expect(levelFromDb(-15)).toBeCloseTo(0.75);
  });

  it('treats missing/non-finite meter values as silence', () => {
    expect(levelFromDb(null)).toBe(0);
    expect(levelFromDb(undefined)).toBe(0);
    expect(levelFromDb(Number.NaN)).toBe(0);
    expect(levelFromDb(-Infinity)).toBe(0);
  });
});

describe('smoothLevel', () => {
  it('rises faster than it falls', () => {
    const up = smoothLevel(0.2, 1);
    const down = smoothLevel(up, 0.2);
    expect(up - 0.2).toBeGreaterThan(up - down); // attack step > release step
    expect(up).toBeGreaterThan(0.2);
    expect(down).toBeLessThan(up);
  });

  it('converges to the target', () => {
    let v = 0;
    for (let i = 0; i < 50; i++) v = smoothLevel(v, 1);
    expect(v).toBeGreaterThan(0.99);
  });
});

describe('orbCopy', () => {
  it('degraded states win over the phase and explain themselves', () => {
    expect(orbCopy('idle', { ...OK, available: false }).disabled).toBe(true);
    expect(orbCopy('idle', { ...OK, available: false }).label).toBe('OFFLINE');
    expect(orbCopy('idle', { ...OK, linked: false }).label).toBe('LINKING');
    expect(orbCopy('idle', { ...OK, stt: false }).label).toBe('TYPE');
    expect(orbCopy('idle', { ...OK, stt: false }).hint).toMatch(/keyboard|type/i);
  });

  it('mic-denied stays pressable so the user can retry the permission prompt', () => {
    const c = orbCopy('idle', { ...OK, micDenied: true });
    expect(c.disabled).toBe(false);
    expect(c.hint).toMatch(/blocked|denied|allow/i);
  });

  it('mic-denied does not mask in-flight phases', () => {
    expect(orbCopy('thinking', { ...OK, micDenied: true }).label).toBe('WORKING');
  });

  it('each live phase has its own copy and only settling phases disable the orb', () => {
    expect(orbCopy('idle', OK)).toEqual({ label: 'TALK', hint: 'Tap, then speak', disabled: false });
    expect(orbCopy('listening', OK).label).toBe('LISTENING');
    expect(orbCopy('transcribing', OK).disabled).toBe(true);
    expect(orbCopy('sending', OK).disabled).toBe(true);
    expect(orbCopy('thinking', OK).disabled).toBe(false);
    expect(orbCopy('speaking', OK).hint).toMatch(/interrupt/i);
  });
});

describe('orbMood', () => {
  it('maps phases onto the four visual moods', () => {
    expect(orbMood('idle')).toBe('idle');
    expect(orbMood('listening')).toBe('input');
    expect(orbMood('transcribing')).toBe('processing');
    expect(orbMood('sending')).toBe('processing');
    expect(orbMood('thinking')).toBe('processing');
    expect(orbMood('speaking')).toBe('output');
  });
});
