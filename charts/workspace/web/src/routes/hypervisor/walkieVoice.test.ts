import { describe, expect, it } from 'vitest';
import {
  transition,
  levelFromTimeDomain,
  levelFromDb,
  smoothLevel,
  createEndpointer,
  ENDPOINT_OPTS,
  orbCopy,
  orbMood,
  type VoicePhase,
} from './walkieVoice';

const OK = {
  available: true,
  linked: true,
  stt: true,
  micDenied: false,
  handsFree: false,
  voiceWake: false,
};

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

  it('voice-start reaches exactly as far as a press from settled phases', () => {
    expect(transition('idle', 'voice-start')).toBe('listening');
    expect(transition('speaking', 'voice-start')).toBe('listening'); // voice barge-in
    expect(transition('thinking', 'voice-start')).toBe('listening');
  });

  it('voice-start never disturbs an active capture or in-flight send', () => {
    expect(transition('listening', 'voice-start')).toBe('listening');
    expect(transition('transcribing', 'voice-start')).toBe('transcribing');
    expect(transition('sending', 'voice-start')).toBe('sending');
  });

  it('voice-end only ends an active capture', () => {
    expect(transition('listening', 'voice-end')).toBe('transcribing');
    for (const p of ['idle', 'transcribing', 'sending', 'thinking', 'speaking'] as const) {
      expect(transition(p, 'voice-end')).toBe(p);
    }
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

describe('createEndpointer', () => {
  // Explicit constants so the tests read as timelines, independent of preset
  // tuning: speech is ≥ 0.5, silence is < 0.25, 200ms to confirm speech,
  // 1000ms of silence to end it, 10s utterance cap.
  const EP = {
    onsetThreshold: 0.5,
    releaseThreshold: 0.25,
    minSpeechMs: 200,
    hangoverMs: 1000,
    maxUtteranceMs: 10000,
  };

  it('stays quiet on silence and never fires an event', () => {
    const ep = createEndpointer(EP);
    for (let t = 0; t <= 5000; t += 50) expect(ep.feed(0.05, t)).toBeNull();
    expect(ep.speaking()).toBe(false);
  });

  it('rejects blips shorter than minSpeechMs (clicks, coughs)', () => {
    const ep = createEndpointer(EP);
    expect(ep.feed(0.9, 0)).toBeNull();
    expect(ep.feed(0.9, 100)).toBeNull(); // still under 200ms
    expect(ep.feed(0.05, 150)).toBeNull(); // died before confirming
    expect(ep.feed(0.05, 2000)).toBeNull(); // and no late speech-end either
    expect(ep.speaking()).toBe(false);
  });

  it('fires speech-start once after sustained speech', () => {
    const ep = createEndpointer(EP);
    expect(ep.feed(0.9, 0)).toBeNull();
    expect(ep.feed(0.9, 100)).toBeNull();
    expect(ep.feed(0.9, 200)).toBe('speech-start');
    expect(ep.feed(0.9, 300)).toBeNull(); // no repeat while speech continues
    expect(ep.speaking()).toBe(true);
  });

  it('hysteresis: the band between release and onset neither starts nor ends a run', () => {
    const ep = createEndpointer(EP);
    // Mid-band from quiet: not an onset.
    expect(ep.feed(0.35, 0)).toBeNull();
    expect(ep.feed(0.35, 500)).toBeNull();
    expect(ep.speaking()).toBe(false);
    // But mid-band inside a candidate run keeps it alive to confirmation.
    expect(ep.feed(0.9, 1000)).toBeNull();
    expect(ep.feed(0.35, 1100)).toBeNull();
    expect(ep.feed(0.35, 1200)).toBe('speech-start');
  });

  it('a pause shorter than the hangover continues the same capture', () => {
    const ep = createEndpointer(EP);
    ep.feed(0.9, 0);
    expect(ep.feed(0.9, 200)).toBe('speech-start');
    expect(ep.feed(0.05, 500)).toBeNull(); // pause begins
    expect(ep.feed(0.05, 1400)).toBeNull(); // 900ms — still inside hangover
    expect(ep.feed(0.9, 1450)).toBeNull(); // resumed: same capture, no event
    expect(ep.speaking()).toBe(true);
    // The hangover clock restarts from the next pause, not the first one.
    expect(ep.feed(0.05, 2000)).toBeNull();
    expect(ep.feed(0.05, 2900)).toBeNull();
    expect(ep.feed(0.05, 3100)).toBe('speech-end'); // 1100ms after 2000
  });

  it('fires speech-end exactly once and can start a fresh capture after', () => {
    const ep = createEndpointer(EP);
    ep.feed(0.9, 0);
    expect(ep.feed(0.9, 250)).toBe('speech-start');
    ep.feed(0.05, 300);
    expect(ep.feed(0.05, 1400)).toBe('speech-end');
    expect(ep.speaking()).toBe(false);
    expect(ep.feed(0.05, 1500)).toBeNull(); // no double end
    ep.feed(0.9, 2000);
    expect(ep.feed(0.9, 2200)).toBe('speech-start'); // new utterance works
  });

  it('caps runaway utterances at maxUtteranceMs, even mid-speech', () => {
    const ep = createEndpointer(EP);
    ep.feed(0.9, 0);
    expect(ep.feed(0.9, 200)).toBe('speech-start');
    expect(ep.feed(0.9, 9000)).toBeNull();
    expect(ep.feed(0.9, 10000)).toBe('speech-end');
    expect(ep.speaking()).toBe(false);
  });

  it('reset drops an in-progress run without an event', () => {
    const ep = createEndpointer(EP);
    ep.feed(0.9, 0);
    expect(ep.feed(0.9, 200)).toBe('speech-start');
    ep.reset();
    expect(ep.speaking()).toBe(false);
    expect(ep.feed(0.05, 5000)).toBeNull(); // no orphaned speech-end
  });

  it('defaults to the listen preset, and presets keep sane relationships', () => {
    const ep = createEndpointer(); // ENDPOINT_OPTS.listen
    let t = 0;
    let started = false;
    for (; t <= ENDPOINT_OPTS.listen.minSpeechMs + 50 && !started; t += 50) {
      started = ep.feed(0.9, t) === 'speech-start';
    }
    expect(started).toBe(true);
    for (const mode of ['listen', 'wake', 'barge'] as const) {
      const o = ENDPOINT_OPTS[mode];
      expect(o.releaseThreshold).toBeLessThan(o.onsetThreshold);
      expect(o.minSpeechMs).toBeLessThan(o.hangoverMs);
      expect(o.hangoverMs).toBeLessThan(o.maxUtteranceMs);
    }
    // Barge-in (TTS echo in the room) must be strictly harder to trigger than
    // wake, which in turn is at least as strict as plain endpointing.
    expect(ENDPOINT_OPTS.barge.onsetThreshold).toBeGreaterThan(ENDPOINT_OPTS.wake.onsetThreshold);
    expect(ENDPOINT_OPTS.barge.minSpeechMs).toBeGreaterThan(ENDPOINT_OPTS.wake.minSpeechMs);
    expect(ENDPOINT_OPTS.wake.onsetThreshold).toBeGreaterThanOrEqual(
      ENDPOINT_OPTS.listen.onsetThreshold,
    );
    expect(ENDPOINT_OPTS.wake.minSpeechMs).toBeGreaterThanOrEqual(ENDPOINT_OPTS.listen.minSpeechMs);
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

  it('hands-free listening explains the auto-send instead of asking for a tap', () => {
    const c = orbCopy('listening', { ...OK, handsFree: true });
    expect(c.label).toBe('LISTENING');
    expect(c.hint).toMatch(/pause/i);
    expect(c.hint).not.toMatch(/tap again/i);
  });

  it('a live open mic invites talking without a tap; a dead one keeps tap copy', () => {
    const live = { ...OK, handsFree: true, voiceWake: true };
    expect(orbCopy('idle', live).hint).toMatch(/start talking/i);
    expect(orbCopy('speaking', live).hint).toMatch(/speak to interrupt/i);
    expect(orbCopy('thinking', live).hint).toMatch(/speak/i);
    // Mode on but the mic isn't live (tab hidden, no AEC, mobile phase 1):
    // never promise voice wake-up the client can't deliver.
    const dead = { ...OK, handsFree: true, voiceWake: false };
    expect(orbCopy('idle', dead).hint).toBe('Tap, then speak');
    expect(orbCopy('speaking', dead).hint).toMatch(/tap to interrupt/i);
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
