import { useEffect, useRef, useState } from 'preact/hooks';
import { isErrorResponse } from '../../api/client';
import { subscribeEvents } from '../../api/events';
import {
  fetchPreview,
  sendPreview,
  previewControl,
  type PreviewState,
  type PreviewMessage,
} from '../../api/gatewayPreview';
import './walkie.css';

/**
 * Walkie-Talkie — the in-app WhatsApp preview (issue #306).
 *
 * A clean, device-branded loopback: type a message and it runs through the SAME
 * Conversation Gateway core the real WhatsApp webhook uses, driving a real
 * Hypervisor turn, and comes back rendered exactly as WhatsApp would show it
 * (bubbles, tap-buttons, ≤4096 chunks). The "wire" disclosure on each message
 * reveals the raw Twilio/Meta payload it becomes on the wire. Only the transport
 * is simulated — the agent and the whole pipeline are real.
 */
function fmtWire(m: PreviewMessage): string {
  if (!m.wire) return '';
  const payload =
    m.direction === 'in' ? m.wire.inbound ?? {} : m.wire.payloads ?? [];
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

export function WalkieTalkie() {
  const [state, setState] = useState<PreviewState | null>(null);
  const [draft, setDraft] = useState('');
  const [busySend, setBusySend] = useState(false);
  const [error, setError] = useState('');
  const [openWire, setOpenWire] = useState<Record<number, boolean>>({});
  const bodyRef = useRef<HTMLDivElement>(null);
  const linkTried = useRef(false);

  async function refresh() {
    try {
      const s = await fetchPreview(0);
      if (isErrorResponse(s)) {
        setError(s.error);
        return;
      }
      setError('');
      setState(s);
    } catch {
      /* transient — the next tick retries */
    }
  }

  useEffect(() => {
    void refresh();
    const unsub = subscribeEvents((ev) => {
      if (ev.type === 'gateway.preview') void refresh();
    });
    // Safety poll: catches the async turn-complete final even if the SSE frame
    // is dropped, and reflects the "thinking" LED promptly.
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      unsub();
      clearInterval(timer);
    };
  }, []);

  // Auto-provision the internal link once, so the preview is usable immediately.
  // The pairing exchange still shows in the transcript (code → "✅ Linked").
  useEffect(() => {
    if (state && state.available && !state.linked && !linkTried.current) {
      linkTried.current = true;
      void previewControl('link').then(() => void refresh());
    }
  }, [state?.linked, state?.available]);

  // Keep pinned to the newest message.
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [state?.cursor]);

  async function send(text: string, button?: string) {
    const payload = (button ?? text).trim();
    if (!payload || busySend) return;
    setBusySend(true);
    try {
      await sendPreview(button ? '' : text, button);
      if (!button) setDraft('');
      await refresh();
    } finally {
      setBusySend(false);
    }
  }

  async function toggleSim() {
    if (!state) return;
    await previewControl('simulate', !state.simulate_out_of_window);
    await refresh();
  }

  async function reset() {
    linkTried.current = false;
    await previewControl('reset');
    await refresh();
  }

  const linked = !!state?.linked;
  const busy = !!state?.busy;
  const provider = (state?.provider || 'meta').toUpperCase();
  const signal = !state?.available
    ? 'off'
    : busy
      ? 'busy'
      : linked
        ? 'live'
        : 'down';
  const signalLabel = !state?.available
    ? 'OFFLINE'
    : busy
      ? 'THINKING…'
      : linked
        ? 'LINKED'
        : 'NOT LINKED';

  return (
    <div class="wt">
      <div class="wt-device" data-signal={signal}>
        {/* ── device head: antenna · speaker grille · LCD readout ── */}
        <div class="wt-head">
          <div class="wt-antenna" aria-hidden="true">
            <span class="wt-antenna-rod" />
            <span class="wt-antenna-tip" />
          </div>
          <div class="wt-lcd" role="status" aria-live="polite">
            <div class="wt-lcd-row">
              <span class="wt-lcd-label">CH</span>
              <span class="wt-lcd-value">WhatsApp</span>
              <span class={`wt-led wt-led-${signal}`} aria-hidden="true" />
              <span class="wt-lcd-status">{signalLabel}</span>
            </div>
            <div class="wt-lcd-row wt-lcd-sub">
              <span class="wt-lcd-label">PROVIDER</span>
              <span class="wt-lcd-value">{provider}</span>
              <span class="wt-lcd-sep">·</span>
              <span class="wt-lcd-label">WINDOW</span>
              <span class="wt-lcd-value">
                {state?.simulate_out_of_window ? 'CLOSED (sim)' : 'OPEN'}
              </span>
            </div>
          </div>
          <div class="wt-grille" aria-hidden="true" />
        </div>

        {/* ── screen: the conversation as WhatsApp would render it ── */}
        <div class="wt-screen" ref={bodyRef}>
          {error && <div class="wt-error">{error}</div>}
          {state && state.messages.length === 0 && (
            <div class="wt-empty">
              <p class="wt-empty-title">Press to talk to your workspace</p>
              <p class="wt-empty-sub">
                Messages run through the real WhatsApp gateway pipeline — locally.
                Expand <span class="wt-wire-chip">wire</span> on any bubble to see
                the exact provider payload.
              </p>
            </div>
          )}
          {state?.messages.map((m) => {
            if (m.kind === 'notice' && m.direction === 'in') return null;
            if (m.kind === 'notice') {
              return (
                <div key={m.seq} class="wt-notice">
                  {m.text}
                </div>
              );
            }
            const wireOpen = !!openWire[m.seq];
            const wire = fmtWire(m);
            return (
              <div
                key={m.seq}
                class={`wt-msg wt-msg-${m.direction} ${m.kind === 'template' ? 'wt-msg-template' : ''}`}
              >
                <div class="wt-bubble">
                  {m.kind === 'template' && (
                    <span class="wt-tag">TEMPLATE · out-of-window</span>
                  )}
                  <div class="wt-bubble-text">{m.text}</div>
                  {m.quick_replies.length > 0 && (
                    <div class="wt-replies">
                      {m.quick_replies.map((r, i) => (
                        <button
                          key={i}
                          type="button"
                          class="wt-reply"
                          disabled={busySend}
                          onClick={() => void send(r, r)}
                        >
                          {r}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                {wire && (
                  <div class="wt-wire">
                    <button
                      type="button"
                      class="wt-wire-toggle"
                      aria-expanded={wireOpen}
                      onClick={() =>
                        setOpenWire((o) => ({ ...o, [m.seq]: !o[m.seq] }))
                      }
                    >
                      {wireOpen ? '▾' : '▸'} wire
                      <span class="wt-wire-provider">{provider}</span>
                    </button>
                    {wireOpen && <pre class="wt-wire-body">{wire}</pre>}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* ── control deck: input + push-to-talk ── */}
        <div class="wt-deck">
          <input
            class="wt-input"
            value={draft}
            placeholder={linked ? 'Type a message…' : 'Linking…'}
            aria-label="Message"
            onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void send(draft);
              }
            }}
          />
          <button
            type="button"
            class="wt-ptt"
            disabled={busySend || !draft.trim()}
            onClick={() => void send(draft)}
            title="Push to talk"
          >
            <span class="wt-ptt-ring" />
            <span class="wt-ptt-label">PTT</span>
          </button>
        </div>

        {/* ── side controls ── */}
        <div class="wt-controls">
          <label class="wt-switch">
            <input
              type="checkbox"
              checked={!!state?.simulate_out_of_window}
              onChange={() => void toggleSim()}
            />
            <span class="wt-switch-track" aria-hidden="true">
              <span class="wt-switch-thumb" />
            </span>
            <span class="wt-switch-label">
              Simulate out-of-window
              <span class="wt-switch-hint">show the template path</span>
            </span>
          </label>
          <button type="button" class="wt-ctl-btn" onClick={() => void reset()}>
            Reset
          </button>
        </div>
      </div>
    </div>
  );
}
