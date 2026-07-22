import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
import { EmptyState } from '../../components/primitives/EmptyState';
import {
  events,
  activeThreadId,
  activeStatus,
  sending,
  stopping,
  chatError,
  selectedAssistant,
  config,
  threads,
  transcriptSource,
  sendMessage,
  stopMessage,
} from '../../store/hypervisor';
import type { HypervisorCommand } from '../../api/hypervisor';
import { supportsSlash, slashToken, matchCommands } from './slashPicker';
import { WorkspaceContext } from './WorkspaceContext';
import { ActivityPanel } from './ActivityPanel';
import { buildTurns, renderMarkdown, turnCopyText, type Block } from './transcript';
import { proxyUrl } from '../../api/apps';
import { navigate, routeHref } from '../../store/router';
import { withOauthPrefix } from '../../api/client';
import { previewFile, fileRawUrl, fileViewUrl, downloadFile, type FilePreview } from '../../api/files';
import {
  isImageFile,
  isAllowedFile,
  isVideoFile,
  filesFromClipboard,
  uploadTaskFile,
  ATTACH_ACCEPT,
} from '../tasks/imageAttach';
import {
  recognitionCtor,
  sttSupported,
  speakReplies,
  speakableText,
  sentenceChunks,
  speakText,
  stopSpeaking,
  type SpeechRecognitionLike,
} from './voice';

/** A user-attached file being uploaded to the workspace so the agent can read
 *  it — same mechanism as the Build tab: upload, then the file's absolute path
 *  is appended to the outgoing message and Claude reads it by path. Images get
 *  an object-URL thumbnail; other allowed types (pdf/md/txt/csv/code/…) render
 *  as an icon + filename chip. */
interface Attachment {
  id: string;
  name: string;
  kind: 'image' | 'file';
  /** Object URL for the thumbnail — images only. */
  previewUrl?: string;
  path?: string;
  status: 'uploading' | 'ready' | 'error';
}

/**
 * The chat transcript + composer. The backend delivers a canonical event stream
 * (assistant prose, tool calls/results, errors); buildTurns() groups it into
 * user bubbles + agent turns, and we render prose as markdown and tool runs as
 * compact activity chips — so the conversation reads as the *Kube-Coder*
 * workspace, not a raw Claude/OpenCode terminal. No screen scraping.
 */

const SUGGESTIONS = [
  "What's running and how much CPU am I using?",
  'Spin up a task to run the tests',
  'Remember that I deploy with `make ship`',
];

/** One tool/command run — collapsed by default, expandable to the raw detail.
 *  A resolved call shows a ✓/✗ outcome so tool activity reads distinctly from
 *  plain messages (and from a still-running call). */
function ActivityChip({ label, detail, error, ok }: { label: string; detail: string; error?: boolean; ok?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div class={`hv-activity ${open ? 'is-open' : ''} ${error ? 'is-error' : ''} ${ok ? 'is-ok' : ''}`}>
      <button type="button" class="hv-activity-head" onClick={() => setOpen((v) => !v)}>
        <span class="hv-activity-icon">
          <Icon name={error ? 'close' : ok ? 'check' : 'terminal'} size={12} />
        </span>
        <span class="hv-activity-label">{label}</span>
        <Icon name="chevron-down" size={13} class="hv-activity-caret" />
      </button>
      {open && detail && <pre class="hv-activity-detail">{detail}</pre>}
    </div>
  );
}

/** Live preview of a running app, embedded via the app-proxy iframe (same
 *  machinery as the Apps page). Falls back to an "Open in Apps" link when the
 *  frame can't authenticate (non-oauth2 deployments). */
function EmbedBlock({ port, title, height }: { port: number; title?: string; height?: number }) {
  const [key, setKey] = useState(0);
  const h = height && height >= 80 ? height : 280;
  return (
    <figure class="hv-embed">
      <figcaption class="hv-embed-head">
        <span class="hv-embed-title">{title || `App on :${port}`}</span>
        <span class="hv-embed-actions">
          <button type="button" class="hv-embed-btn" onClick={() => setKey((k) => k + 1)}>
            Reload
          </button>
          <a
            class="hv-embed-btn"
            href={routeHref(`/apps/${port}`)}
            onClick={(e) => {
              // Left-click → SPA navigation to the app's Apps-view page (keeps
              // dashboard state, ingress-prefix aware). Modifier-clicks fall
              // through to the browser so "open in new tab" still works.
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
              e.preventDefault();
              navigate(`/apps/${port}`);
            }}
          >
            Open in Apps <Icon name="link" size={11} />
          </a>
        </span>
      </figcaption>
      <iframe
        key={key}
        class="hv-embed-frame"
        style={{ height: `${h}px` }}
        src={proxyUrl(port)}
        title={title || `Application on port ${port}`}
        sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-downloads"
      />
    </figure>
  );
}

/** An inline image or video. Workspace files go through the authed
 *  /api/files/raw endpoint; external URLs are used directly. */
function MediaBlock({
  mediaKind,
  path,
  url,
  title,
  height,
}: {
  mediaKind: 'image' | 'video';
  path?: string;
  url?: string;
  title?: string;
  height?: number;
}) {
  const src = url || (path ? `${withOauthPrefix('/api/files/raw')}?path=${encodeURIComponent(path)}` : '');
  if (!src) return null;
  const maxH = height && height >= 40 ? height : 420;
  return (
    <figure class="hv-media">
      {mediaKind === 'video' ? (
        <video class="hv-media-el" src={src} controls preload="metadata" style={{ maxHeight: `${maxH}px` }} />
      ) : (
        <img class="hv-media-el" src={src} alt={title || 'image'} loading="lazy" style={{ maxHeight: `${maxH}px` }} />
      )}
      {title && <figcaption class="hv-media-cap">{title}</figcaption>}
    </figure>
  );
}

const MARKDOWN_RE = /\.(md|markdown|mdx)$/i;

/** A document/file the agent asked to show (via show_file). We classify it with
 *  /api/files/preview and render inline: markdown formatted, text/code in a
 *  scroll box, image/video like MediaBlock, and PDF/HTML/SVG in a sandboxed
 *  <iframe> served by /api/files/view. Anything else offers a download. */
function FileBlock({ path, title, height }: { path: string; title?: string; height?: number }) {
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setPreview(null);
    setError(null);
    previewFile(path)
      .then((p) => live && setPreview(p))
      .catch((e) => live && setError(e?.message || 'could not load'));
    return () => {
      live = false;
    };
  }, [path]);

  const name = path.split('/').pop() || path;
  const frameH = height && height >= 80 ? height : 420;
  const mime = preview?.mime || '';
  const isPdf = mime === 'application/pdf';
  const inFrame =
    isPdf || ['text/html', 'application/xhtml+xml', 'image/svg+xml', 'text/xml', 'application/xml'].includes(mime);

  const head = (
    <figcaption class="hv-file-head">
      <span class="hv-file-name">
        <Icon name="files" size={12} /> {title || name}
      </span>
      <button type="button" class="hv-file-btn" onClick={() => downloadFile(path, name)}>
        Download <Icon name="download" size={11} />
      </button>
    </figcaption>
  );

  let body;
  if (error) {
    body = <div class="hv-file-msg hv-file-err">Couldn’t load {name}: {error}</div>;
  } else if (!preview) {
    body = <div class="hv-file-msg">Loading {name}…</div>;
  } else if (preview.kind === 'image') {
    body = <img class="hv-file-media" src={fileRawUrl(path)} alt={title || name} loading="lazy" />;
  } else if (preview.kind === 'video') {
    body = <video class="hv-file-media" src={fileRawUrl(path)} controls preload="metadata" />;
  } else if (inFrame) {
    // PDF needs no sandbox attr (the browser's viewer works framed and can't
    // script the parent); HTML/SVG/XML get an empty sandbox + the server's
    // `CSP: sandbox` so they render but can't touch the dashboard origin.
    body = (
      <iframe
        class="hv-file-frame"
        style={{ height: `${frameH}px` }}
        src={fileViewUrl(path)}
        title={title || name}
        {...(isPdf ? {} : { sandbox: '' as const })}
      />
    );
  } else if (preview.kind === 'text') {
    if (MARKDOWN_RE.test(path) || mime === 'text/markdown') {
      body = (
        <div
          class="hv-prose hv-file-md"
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: renderMarkdown(preview.content) }}
        />
      );
    } else {
      body = <pre class="hv-file-code mono">{preview.content}</pre>;
    }
  } else {
    body = (
      <div class="hv-file-msg">
        {name} is a binary file — <button type="button" class="hv-file-link" onClick={() => downloadFile(path, name)}>download it</button> to view.
      </div>
    );
  }

  const truncated = preview?.kind === 'text' && preview.truncated;
  return (
    <figure class="hv-file">
      {head}
      {body}
      {truncated && <div class="hv-file-note">Preview truncated — download for the full file.</div>}
    </figure>
  );
}

/** A multiple-choice prompt the agent emitted (a ```choice block). Options are
 *  clickable buttons; clicking one sends it as the next message — no need to
 *  type "1". Only the latest turn's picker is `interactive`; historical ones
 *  render disabled so you can't re-answer a resolved question. The composer
 *  stays open for "none of these — let me type my own answer". */
function ChoiceBlock({
  question,
  options,
  interactive,
  onChoose,
}: {
  question?: string;
  options: string[];
  interactive: boolean;
  onChoose: (text: string) => void;
}) {
  const disabled = !interactive || sending.value || activeStatus.value === 'running';
  return (
    <div class="hv-choice">
      {question && (
        <div
          class="hv-choice-q"
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: renderMarkdown(question) }}
        />
      )}
      <div class="hv-choice-opts">
        {options.map((o, i) => (
          <button
            key={i}
            type="button"
            class="hv-choice-opt"
            disabled={disabled}
            onClick={() => onChoose(o)}
          >
            <span class="hv-choice-num">{i + 1}</span>
            <span class="hv-choice-text">{o}</span>
          </button>
        ))}
      </div>
      {interactive && <div class="hv-choice-hint">Or type your own answer below.</div>}
    </div>
  );
}

function AgentBlocks({
  blocks,
  interactive,
  onChoose,
}: {
  blocks: Block[];
  interactive: boolean;
  onChoose: (text: string) => void;
}) {
  return (
    <>
      {blocks.map((b, i) => {
        switch (b.kind) {
          case 'prose':
            return (
              <div
                key={i}
                class="hv-prose"
                // eslint-disable-next-line react/no-danger
                dangerouslySetInnerHTML={{ __html: renderMarkdown(b.text) }}
              />
            );
          case 'embed':
            return <EmbedBlock key={i} port={b.port} title={b.title} height={b.height} />;
          case 'media':
            return (
              <MediaBlock
                key={i}
                mediaKind={b.mediaKind}
                path={b.path}
                url={b.url}
                title={b.title}
                height={b.height}
              />
            );
          case 'file':
            return <FileBlock key={i} path={b.path} title={b.title} height={b.height} />;
          case 'choice':
            return (
              <ChoiceBlock
                key={i}
                question={b.question}
                options={b.options}
                interactive={interactive}
                onChoose={onChoose}
              />
            );
          default:
            return <ActivityChip key={i} label={b.label} detail={b.detail} error={b.error} ok={b.ok} />;
        }
      })}
    </>
  );
}

/** The `/` autocomplete popover: skills + custom slash commands filtered by the
 *  typed prefix. Presentational — open/close, filtering and keyboard nav live in
 *  Chat's composer. Renders above the composer (both desktop and mobile, so it
 *  clears the on-screen keyboard) and stays tap- and keyboard-navigable for the
 *  Expo-wrapped app, which has no hover. */
function SlashMenu({
  items,
  activeIndex,
  onHover,
  onPick,
}: {
  items: HypervisorCommand[];
  activeIndex: number;
  onHover: (i: number) => void;
  onPick: (c: HypervisorCommand) => void;
}) {
  return (
    <div class="hv-slash-menu" role="listbox" aria-label="Slash commands and skills">
      <div class="hv-slash-menu-head">
        Slash commands &amp; skills
        <span class="hv-slash-menu-hint">↑↓ to move · Enter to insert</span>
      </div>
      {items.map((c, i) => (
        <button
          key={`${c.kind}:${c.name}`}
          type="button"
          role="option"
          aria-selected={i === activeIndex}
          class={`hv-slash-item ${i === activeIndex ? 'is-active' : ''}`}
          // onMouseDown (not onClick): fires before the textarea's blur so the
          // caret/focus isn't lost mid-pick; preventDefault keeps focus put.
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(c);
          }}
          onMouseEnter={() => onHover(i)}
        >
          <span class="hv-slash-item-main">
            <span class="hv-slash-name">/{c.name}</span>
            {c.argument_hint && <span class="hv-slash-args">{c.argument_hint}</span>}
            <span class={`hv-slash-kind hv-slash-kind-${c.kind}`}>{c.kind}</span>
          </span>
          {c.description && <span class="hv-slash-desc">{c.description}</span>}
        </button>
      ))}
    </div>
  );
}

export function Chat() {
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // Inline feedback when the user tries to attach something we can't send
  // (video, or an unsupported type) — never a silent drop.
  const [attachError, setAttachError] = useState<string | null>(null);
  // Slash-command / skill picker (issue #302): index of the highlighted entry
  // and a per-Esc dismiss flag (cleared on the next keystroke so re-typing the
  // token reopens it). The menu's *open* state is otherwise derived from the
  // draft — no separate "open" boolean to keep in sync.
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // Whether the view is pinned to the bottom. We only auto-scroll on new events
  // while pinned — so scrolling up to read history isn't yanked back down by the
  // 2s poll. Starts true; the scroll handler flips it as the user scrolls.
  const pinnedRef = useRef(true);
  // Per-message copy feedback (issue #351): key of the message whose button
  // currently reads "Copied", reverted after a beat.
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const copiedTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
    },
    [],
  );

  // ── Voice input (issue #396, tier 0) ───────────────────────────────────────
  // Push-to-talk via the browser's SpeechRecognition: tap to record, tap again
  // to stop. Final transcripts accumulate into the draft (interims show live),
  // so the user can review/edit before the existing send path fires — voice is
  // an input method, not a separate pipeline.
  const [listening, setListening] = useState(false);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const recRef = useRef<SpeechRecognitionLike | null>(null);
  // Draft text present when recording started, and finalized speech so far —
  // each result event re-renders draft as base + finals + current interim.
  const micBaseRef = useRef('');
  const micFinalRef = useRef('');

  function stopMic() {
    try {
      recRef.current?.stop();
    } catch {
      /* already stopped */
    }
  }

  function toggleMic() {
    if (listening) {
      stopMic();
      return;
    }
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang = navigator.language || 'en-US';
    rec.interimResults = true;
    rec.continuous = true;
    micBaseRef.current = draft.trim();
    micFinalRef.current = '';
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const piece = r[0]?.transcript ?? '';
        if (r.isFinal) micFinalRef.current = `${micFinalRef.current} ${piece}`.trim();
        else interim += piece;
      }
      setDraft(
        [micBaseRef.current, micFinalRef.current, interim.trim()].filter(Boolean).join(' '),
      );
    };
    rec.onerror = (e) => {
      // 'no-speech' (silence timeout) and 'aborted' are routine, not failures.
      if (e.error && e.error !== 'no-speech' && e.error !== 'aborted') {
        setVoiceError(
          e.error === 'not-allowed' || e.error === 'service-not-allowed'
            ? 'Microphone access was denied. Voice input needs mic permission and an HTTPS page.'
            : `Voice input failed: ${e.error}`,
        );
      }
    };
    // The engine also ends itself (silence timeout, tab switch) — reflect that.
    rec.onend = () => {
      recRef.current = null;
      setListening(false);
      taRef.current?.focus();
    };
    recRef.current = rec;
    setVoiceError(null);
    try {
      rec.start();
      setListening(true);
    } catch {
      recRef.current = null;
    }
  }

  // Kill the mic and any queued speech when the chat unmounts.
  useEffect(
    () => () => {
      try {
        recRef.current?.abort();
      } catch {
        /* noop */
      }
      stopSpeaking();
    },
    [],
  );

  async function copyMessage(key: string, text: string) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopiedKey(null), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }

  // Fenced-code Copy buttons live inside sanitized markdown HTML (see
  // renderMarkdown), so clicks are delegated from the transcript container
  // instead of bound per-button. Feedback mutates the button directly — it's
  // static HTML outside the vdom, so there's no state to render from.
  function onTranscriptClick(e: MouseEvent) {
    const btn = (e.target as HTMLElement).closest?.('.hv-code-copy');
    if (!(btn instanceof HTMLButtonElement)) return;
    const code = btn.parentElement?.querySelector('pre')?.textContent ?? '';
    void (async () => {
      try {
        await navigator.clipboard.writeText(code.replace(/\n$/, ''));
        btn.textContent = 'Copied';
        btn.classList.add('is-copied');
        setTimeout(() => {
          btn.textContent = 'Copy';
          btn.classList.remove('is-copied');
        }, 1500);
      } catch {
        /* clipboard unavailable */
      }
    })();
  }

  function addFiles(files: File[]) {
    if (!files.length) return;
    const allowed = files.filter(isAllowedFile);
    const rejected = files.filter((f) => !isAllowedFile(f));
    // Surface a clear reason for anything we drop — video gets its own message
    // since it's a "not yet" rather than a "never".
    if (rejected.length) {
      setAttachError(
        rejected.some(isVideoFile)
          ? "Video can't be read by the Hypervisor yet."
          : "This file type can't be sent to the Hypervisor.",
      );
    } else if (allowed.length) {
      setAttachError(null);
    }
    for (const file of allowed) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const kind = isImageFile(file) ? 'image' : 'file';
      // Only images get an object-URL thumbnail; other types show an icon chip.
      const previewUrl = kind === 'image' ? URL.createObjectURL(file) : undefined;
      const name = file.name || (kind === 'image' ? 'image' : 'file');
      setAttachments((a) => [...a, { id, name, kind, previewUrl, status: 'uploading' }]);
      // Reuse the Build tab's uploader; 'hypervisor' → .claude-tasks/hypervisor/attachments.
      void uploadTaskFile('hypervisor', file)
        .then((path) =>
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, path, status: 'ready' } : x))),
        )
        .catch(() =>
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: 'error' } : x))),
        );
    }
  }

  function removeAttachment(id: string) {
    setAttachments((a) => {
      const x = a.find((t) => t.id === id);
      if (x?.previewUrl) URL.revokeObjectURL(x.previewUrl);
      return a.filter((t) => t.id !== id);
    });
  }

  const active = activeThreadId.value;
  const status = activeStatus.value;
  const evts = events.value;

  const turns = useMemo(() => buildTurns(evts), [evts]);
  const hasAgentTail = turns.length > 0 && turns[turns.length - 1].role === 'agent';

  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    // Resize inside rAF so the intermediate height:auto (needed to measure
    // scrollHeight) is never painted — Firefox otherwise flashes the composer
    // between the two heights on every keystroke, resizing the transcript
    // above it in the same frame (#348).
    const raf = requestAnimationFrame(() => {
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    });
    return () => cancelAnimationFrame(raf);
  }, [draft]);

  // The pin write below dispatches a real `scroll` event; this flag keeps
  // onTranscriptScroll from re-measuring off that echo — mid-composer-resize
  // it read a stale clientHeight and closed the jiggle feedback loop (#348).
  const programmaticScrollRef = useRef(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    const before = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    // Only arm the guard when the viewport actually moved — an unmoved
    // scrollTop fires no event, and a stale flag would swallow the user's
    // next real scroll.
    if (el.scrollTop !== before) programmaticScrollRef.current = true;
  }, [turns]);

  // Track pin state: pinned when within ~80px of the bottom. Scrolling up
  // unpins (so polls stop yanking down); scrolling back to the bottom re-pins.
  function onTranscriptScroll() {
    if (programmaticScrollRef.current) {
      programmaticScrollRef.current = false;
      return;
    }
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  // A freshly opened thread starts pinned to the bottom.
  useEffect(() => {
    pinnedRef.current = true;
  }, [active]);

  function submit(text?: string) {
    if (blocked) return;
    stopMic(); // sending finalizes dictation — don't keep transcribing into the next draft
    pinnedRef.current = true; // sending your own message re-pins to the bottom
    const value = (text ?? draft).trim();
    // Append each uploaded image's absolute path on its own line — Claude Code
    // reads the image by path (same as the Build tab composer).
    const paths = attachments
      .filter((a) => a.status === 'ready' && a.path)
      .map((a) => a.path as string);
    if (!value && paths.length === 0) return;
    const finalText = [value, ...paths].filter(Boolean).join('\n');
    setDraft('');
    attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    setAttachments([]);
    setAttachError(null);
    void sendMessage(finalText);
    taRef.current?.focus();
  }

  const busy = sending.value;
  const working = status === 'running';
  // Input is locked whenever a turn is in flight — not just during the brief
  // send request — so the user can't queue a message the server would reject
  // (409 "assistant is still responding"). Stop is the only action then.
  const blocked = busy || working;
  const readOnly = config.value?.readOnly;
  const empty = !active && evts.length === 0;
  const cli = selectedAssistant.value || 'agent';
  // Show the thinking indicator while the agent is working, or right after we
  // sent and no assistant turn has landed yet.
  const thinking = working || (busy && active !== null && !hasAgentTail);
  const canSend = !!draft.trim() || attachments.some((a) => a.status === 'ready');

  // ── Spoken replies (issue #396, tier 0) ────────────────────────────────────
  // When the topbar "speak replies" toggle is on, read the agent's prose aloud
  // via speechSynthesis. Text is enqueued at sentence boundaries as it streams
  // in, so playback starts before the turn completes; the remainder flushes
  // when the turn goes idle. Tracking is per-turn: a turn first seen while NOT
  // live (history loading on thread open, or the toggle just flipped on) is
  // marked already-spoken so we never narrate the past.
  const speakOn = speakReplies.value;
  const live = working || busy;
  const spokenRef = useRef({ key: '', chars: 0 });
  useEffect(() => {
    if (!speakOn) return;
    const last = turns[turns.length - 1];
    if (!last || last.role !== 'agent') return;
    const key = `${active ?? ''}#${turns.length - 1}`;
    const full = speakableText(last.blocks);
    if (spokenRef.current.key !== key) {
      spokenRef.current = { key, chars: live ? 0 : full.length };
    }
    if (full.length <= spokenRef.current.chars) return;
    const fresh = full.slice(spokenRef.current.chars);
    if (live) {
      const { complete } = sentenceChunks(fresh);
      if (!complete) return;
      speakText(complete);
      spokenRef.current.chars += complete.length;
    } else {
      speakText(fresh);
      spokenRef.current.chars = full.length;
    }
  }, [turns, speakOn, live, active]);

  // ── Slash-command / skill picker (issue #302) ──────────────────────────────
  // The agent a message will actually run under: the active thread's fixed
  // assistant, or — for a not-yet-created chat — the sidebar selection. The
  // picker only appears when that agent expands `/name` (Claude today).
  const activeThread = threads.value.find((t) => t.id === active);
  const effectiveAssistant = active ? activeThread?.assistant ?? '' : selectedAssistant.value;
  const slashEnabled = supportsSlash(effectiveAssistant) && !readOnly;
  const commands = config.value?.commands ?? [];
  const token = slashEnabled ? slashToken(draft) : null;
  const matches = useMemo(
    () => (token === null ? [] : matchCommands(commands, token)),
    [token, commands],
  );
  const showMenu = token !== null && !menuDismissed && !blocked && matches.length > 0;
  // Keep the highlight in range as the filtered list shrinks/grows.
  useEffect(() => {
    setMenuIndex((i) => (i >= matches.length ? 0 : i));
  }, [matches.length]);

  function pickCommand(cmd: HypervisorCommand) {
    // Insert `/<name> ` — the trailing space both completes the token (closing
    // the menu, since slashToken() now returns null) and positions the caret to
    // type arguments. Command stays the first token so Claude expands it.
    setDraft(`/${cmd.name} `);
    setMenuDismissed(false);
    setMenuIndex(0);
    taRef.current?.focus();
  }

  function onDraftInput(value: string) {
    setDraft(value);
    // Any edit clears a prior Esc-dismiss so re-typing the token reopens the menu.
    if (menuDismissed) setMenuDismissed(false);
  }

  function onKeyDown(e: KeyboardEvent) {
    if (showMenu) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setMenuIndex((i) => (i + 1) % matches.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setMenuIndex((i) => (i - 1 + matches.length) % matches.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        pickCommand(matches[Math.min(menuIndex, matches.length - 1)]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMenuDismissed(true);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div class="hv-chat">
      {active && <WorkspaceContext />}
      {active && <ActivityPanel threadId={active} running={working} />}

      <div class="hv-transcript" ref={scrollRef} onScroll={onTranscriptScroll} onClick={onTranscriptClick}>
        {empty ? (
          <div class="hv-welcome-host">
            <EmptyState
              icon={<Icon name="hypervisor" size={26} />}
              title="Kube-Coder"
              description={
                <>
                  Ask about your workspace or tell it what to do — it reads live
                  state and acts on it through your tools.
                </>
              }
            />
            <div class="hv-suggests">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  class="hv-suggest"
                  onClick={() => submit(s)}
                  disabled={blocked}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div class="hv-transcript-flow">
            {transcriptSource.value === 'session_log' && (
              <div class="hv-source-chip" title="Rendered from Claude Code's own JSONL session log — the complete, structured record of this thread.">
                <Icon name="check" size={11} /> Structured transcript · session log
              </div>
            )}
            {turns.map((t, i) =>
              t.role === 'user' ? (
                <div key={i} class="hv-msg hv-msg-user">
                  <div class="hv-bubble">{t.text}</div>
                  <button
                    type="button"
                    class="hv-copy-btn hv-msg-copy"
                    onClick={() => void copyMessage(`u${i}`, t.text)}
                    title="Copy message"
                    aria-label="Copy message"
                  >
                    <Icon name={copiedKey === `u${i}` ? 'check' : 'copy'} size={12} />
                    {copiedKey === `u${i}` ? 'Copied' : 'Copy'}
                  </button>
                </div>
              ) : (
                <div key={i} class="hv-turn">
                  <div class="hv-avatar" aria-hidden="true">
                    <Icon name="hypervisor" size={15} />
                  </div>
                  <div class="hv-turn-body">
                    <div class="hv-turn-head">
                      <span class="hv-turn-name">Kube-Coder</span>
                      <span class="hv-turn-via">via {cli}</span>
                      {thinking && i === turns.length - 1 && (
                        <span class="hv-typing" aria-label="working">
                          <i />
                          <i />
                          <i />
                        </span>
                      )}
                      {turnCopyText(t.blocks) && (
                        <button
                          type="button"
                          class="hv-copy-btn hv-turn-copy"
                          onClick={() => void copyMessage(`a${i}`, turnCopyText(t.blocks))}
                          title="Copy message"
                          aria-label="Copy message"
                        >
                          <Icon name={copiedKey === `a${i}` ? 'check' : 'copy'} size={12} />
                          {copiedKey === `a${i}` ? 'Copied' : 'Copy'}
                        </button>
                      )}
                    </div>
                    <AgentBlocks
                      blocks={t.blocks}
                      interactive={i === turns.length - 1 && !working}
                      onChoose={submit}
                    />
                  </div>
                </div>
              ),
            )}

            {/* Agent is working but hasn't emitted its turn block yet. */}
            {active && thinking && !hasAgentTail && (
              <div class="hv-turn">
                <div class="hv-avatar" aria-hidden="true">
                  <Icon name="hypervisor" size={15} />
                </div>
                <div class="hv-turn-body">
                  <div class="hv-turn-head">
                    <span class="hv-turn-name">Kube-Coder</span>
                    <span class="hv-turn-via">via {cli}</span>
                    <span class="hv-typing" aria-label="working">
                      <i />
                      <i />
                      <i />
                    </span>
                  </div>
                  <div class="hv-prose hv-prose-muted">Working…</div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {chatError.value && <div class="hv-banner hv-banner-error">{chatError.value}</div>}

      {attachError && (
        <div class="hv-banner hv-banner-error" role="alert">
          {attachError}
          <button
            type="button"
            class="hv-banner-x"
            onClick={() => setAttachError(null)}
            aria-label="Dismiss"
          >
            <Icon name="close" size={11} />
          </button>
        </div>
      )}

      {voiceError && (
        <div class="hv-banner hv-banner-error" role="alert">
          {voiceError}
          <button
            type="button"
            class="hv-banner-x"
            onClick={() => setVoiceError(null)}
            aria-label="Dismiss"
          >
            <Icon name="close" size={11} />
          </button>
        </div>
      )}

      {attachments.length > 0 && (
        <div class="hv-attachments">
          {attachments.map((a) => (
            <div
              key={a.id}
              class={`hv-attachment is-${a.status} ${a.kind === 'file' ? 'is-doc' : ''}`}
              title={a.name}
            >
              {a.kind === 'image' && a.previewUrl ? (
                <img src={a.previewUrl} alt={a.name} />
              ) : (
                <span class="hv-attachment-doc">
                  <Icon name="files" size={16} />
                  <span class="hv-attachment-name">{a.name}</span>
                </span>
              )}
              <button
                type="button"
                class="hv-attachment-x"
                onClick={() => removeAttachment(a.id)}
                title="Remove"
              >
                <Icon name="close" size={10} />
              </button>
            </div>
          ))}
        </div>
      )}

      <form
        class="hv-composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        onDrop={(e) => {
          const files = Array.from(e.dataTransfer?.files || []);
          if (files.length) {
            e.preventDefault();
            addFiles(files);
          }
        }}
        onDragOver={(e) => e.preventDefault()}
      >
        {showMenu && (
          <SlashMenu
            items={matches}
            activeIndex={Math.min(menuIndex, matches.length - 1)}
            onHover={setMenuIndex}
            onPick={pickCommand}
          />
        )}
        <input
          ref={fileRef}
          type="file"
          accept={ATTACH_ACCEPT}
          multiple
          style={{ display: 'none' }}
          onChange={(e) => {
            const input = e.target as HTMLInputElement;
            addFiles(Array.from(input.files || []));
            input.value = '';
          }}
        />
        <button
          type="button"
          class="hv-attach-btn"
          onClick={() => fileRef.current?.click()}
          disabled={blocked}
          title="Attach a file (image, pdf, text, code…)"
          aria-label="Attach a file"
        >
          <Icon name="upload" size={16} />
        </button>
        {slashEnabled && commands.length > 0 && (
          <button
            type="button"
            class="hv-attach-btn hv-slash-btn"
            onClick={() => {
              // Tap-only affordance (mobile): seed the token so the menu opens
              // without a physical `/` key. No-op if a command is already typed.
              if (slashToken(draft) === null) onDraftInput('/');
              setMenuDismissed(false);
              taRef.current?.focus();
            }}
            disabled={blocked}
            title="Slash commands & skills"
            aria-label="Slash commands and skills"
          >
            <span class="hv-slash-glyph">/</span>
          </button>
        )}
        {sttSupported() && !readOnly && (
          <button
            type="button"
            class={`hv-attach-btn hv-mic-btn ${listening ? 'is-listening' : ''}`}
            onClick={toggleMic}
            disabled={blocked}
            title={listening ? 'Stop recording' : 'Dictate a message (push to talk)'}
            aria-label={listening ? 'Stop recording' : 'Dictate a message'}
            aria-pressed={listening}
          >
            <Icon name="mic" size={16} />
          </button>
        )}
        <textarea
          ref={taRef}
          class="hv-composer-input"
          value={draft}
          placeholder={
            readOnly
              ? 'Read-only workspace — you can still ask about state'
              : working
                ? 'Kube-Coder is working… press Stop to interrupt'
                : listening
                  ? 'Listening… tap the mic again to stop'
                  : 'Message Kube-Coder…  (paste, attach, or / for commands)'
          }
          onInput={(e) => onDraftInput((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
          onPaste={(e) => {
            const files = filesFromClipboard(e.clipboardData);
            if (files.length) {
              e.preventDefault();
              addFiles(files);
            }
          }}
          rows={1}
          disabled={blocked}
        />
        {working ? (
          <Button
            type="button"
            variant="danger"
            onClick={() => void stopMessage()}
            disabled={stopping.value}
            title="Stop execution"
          >
            <Icon name="close" size={12} /> {stopping.value ? 'Stopping…' : 'Stop'}
          </Button>
        ) : (
          <Button type="submit" variant="primary" disabled={blocked || !canSend} title="Send (Enter)">
            <Icon name="play" size={12} /> Send
          </Button>
        )}
      </form>
    </div>
  );
}
