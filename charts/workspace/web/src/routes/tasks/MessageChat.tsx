import { useEffect, useRef, useState } from 'preact/hooks';
import type { TaskStatus, PendingPrompt, PromptOption } from '../../api/tasks';
import { getTask } from '../../api/tasks';
import { sendFollowup } from '../../store/tasks';
import { pushToast } from '../../store/ui';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { serverMode } from '../../store/server-mode';
import { TerminalPane } from './TerminalPane';
import { getSessionSignals } from './sessionSignals';
import { imagesFromClipboard, isImageFile, uploadTaskImage } from './imageAttach';

export interface MessageChatProps {
  taskId: string;
  status: TaskStatus;
  /** Optional human-readable task name for the header line. */
  taskName?: string | null;
}

/** One image attached to the composer, tracked from paste/drop → upload. */
interface Attachment {
  id: string;
  name: string;
  /** Object URL for the local thumbnail preview. */
  previewUrl: string;
  /** Absolute on-disk path Claude Code will read; set once uploaded. */
  path?: string;
  status: 'uploading' | 'ready' | 'error';
}

/** How often the Send-message tab re-checks the live screen for an interactive
 *  prompt while the task is running. The global task poll is SSE-gated to a slow
 *  fallback (~45s), so we run a focused check here — only while this tab is
 *  mounted and visible — to surface quick-reply buttons within a few seconds. */
const PROMPT_POLL_MS = 4000;

/** Validate the server's (untrusted-shape) pending_prompt into a PendingPrompt,
 *  or null. Anything malformed → null so the plain composer stays in control.
 *  Exported for unit tests. */
export function readPendingPrompt(p: unknown): PendingPrompt | null {
  if (!p || typeof p !== 'object') return null;
  const o = p as Record<string, unknown>;
  if (o.kind !== 'choice' && o.kind !== 'yesno') return null;
  if (!Array.isArray(o.options)) return null;
  const options: PromptOption[] = [];
  for (const raw of o.options) {
    if (!raw || typeof raw !== 'object') continue;
    const { index, label } = raw as Record<string, unknown>;
    const okIndex = typeof index === 'number' || typeof index === 'string';
    if (!okIndex || typeof label !== 'string') continue;
    options.push({ index, label });
  }
  if (options.length < 2) return null;
  const question = typeof o.question === 'string' ? o.question : null;
  return { kind: o.kind, question, options };
}

/**
 * Send-message tab. Embeds the live ttyd terminal directly (same attach the
 * Session tab uses) so the user sees the real session — including the
 * auto-detected link badge TerminalPane renders — and pairs it with a friendly
 * composer box below. Typing in the box and hitting Send posts a follow-up to
 * the tmux session; the result streams back in the embedded terminal. This is
 * the mobile-friendly path: composing in a normal <textarea> beats typing into
 * the ttyd iframe on a phone.
 *
 * Images can be pasted (Cmd/Ctrl+V), dropped, or picked via the image button
 * (issue #179). Each is uploaded to the task's attachments dir and its saved
 * absolute path is appended to the outgoing prompt so Claude Code reads it —
 * see imageAttach.ts for why the tmux text path can't carry image bytes.
 */
export function MessageChat({ taskId, status }: MessageChatProps) {
  // Treat "waiting-for-input" as alive too — the task is paused on a prompt and
  // sending a follow-up is exactly how the user unblocks it.
  const isRunning = status === 'running' || status === 'waiting-for-input';

  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [prompt, setPrompt] = useState<PendingPrompt | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // Mirror attachments into a ref so the unmount cleanup can revoke every
  // object URL without capturing a stale closure (and without re-registering
  // the effect on every attachment change).
  const attachRef = useRef<Attachment[]>([]);
  attachRef.current = attachments;
  useEffect(() => () => {
    for (const a of attachRef.current) URL.revokeObjectURL(a.previewUrl);
  }, []);

  // Poll the task's live screen for an interactive prompt (numbered permission
  // menu / yes-no) so we can render quick-reply buttons. Only runs while the
  // task is alive and this tab is visible; a static screen keeps returning the
  // same prompt, and it clears the moment the task moves on. Fail-safe: any
  // fetch error keeps the last state rather than flashing buttons off.
  useEffect(() => {
    if (!isRunning) {
      setPrompt(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (typeof document === 'undefined' || !document.hidden) {
        try {
          const t = await getTask(taskId);
          if (!cancelled) setPrompt(readPendingPrompt(t.pending_prompt));
        } catch {
          /* transient fetch error — keep the last known prompt */
        }
      }
      if (!cancelled) timer = setTimeout(tick, PROMPT_POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [taskId, isRunning]);

  const readOnly = serverMode.value.readOnly;
  const uploading = attachments.some((a) => a.status === 'uploading');
  const readyImages = attachments.filter((a) => a.status === 'ready' && a.path);
  const canSend = !busy && !uploading && !readOnly && (!!msg.trim() || readyImages.length > 0);

  // Receive clipboard text from the TaskBar "Paste from clipboard" action and
  // drop it into the composer (appending to whatever's already typed) so the
  // user can review before sending.
  const session = getSessionSignals(taskId);
  const paste = session.pasteRequest.value;
  const lastPasteNonce = useRef<number>(paste?.nonce ?? 0);
  useEffect(() => {
    if (!paste || paste.nonce === lastPasteNonce.current) return;
    lastPasteNonce.current = paste.nonce;
    setMsg((m) => (m ? `${m}${m.endsWith('\n') ? '' : '\n'}${paste.text}` : paste.text));
    inputRef.current?.focus();
  }, [paste]);

  // Receive clipboard IMAGES from the same toolbar "Paste" action and run them
  // through the normal attach flow (upload → chip), matching a direct paste.
  const imgPaste = session.imagePasteRequest.value;
  const lastImgPasteNonce = useRef<number>(imgPaste?.nonce ?? 0);
  useEffect(() => {
    if (!imgPaste || imgPaste.nonce === lastImgPasteNonce.current) return;
    lastImgPasteNonce.current = imgPaste.nonce;
    addImageFiles(imgPaste.files);
    // addImageFiles is a stable function declaration; deps intentionally track
    // only the paste request (mirrors the text pasteRequest effect above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imgPaste]);

  // Upload each image and track it as a chip. Runs uploads in parallel; each
  // chip flips uploading → ready|error independently.
  function addImageFiles(files: File[]) {
    const imgs = files.filter(isImageFile);
    if (!imgs.length) return;
    if (readOnly) {
      pushToast('Read-only public demo — attachments are disabled.', { kind: 'warn' });
      return;
    }
    for (const file of imgs) {
      const id = `${Date.now().toString(36)}-${Math.round(Math.random() * 1e9).toString(36)}`;
      const previewUrl = URL.createObjectURL(file);
      const att: Attachment = { id, name: file.name || 'pasted image', previewUrl, status: 'uploading' };
      setAttachments((a) => [...a, att]);
      void (async () => {
        try {
          const path = await uploadTaskImage(taskId, file);
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, path, status: 'ready' } : x)));
        } catch (err) {
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: 'error' } : x)));
          pushToast(err instanceof Error ? err.message : 'Image upload failed', { kind: 'danger' });
        }
      })();
    }
  }

  function removeAttachment(id: string) {
    setAttachments((a) => {
      const gone = a.find((x) => x.id === id);
      if (gone) URL.revokeObjectURL(gone.previewUrl);
      return a.filter((x) => x.id !== id);
    });
  }

  function clearAttachments() {
    for (const a of attachRef.current) URL.revokeObjectURL(a.previewUrl);
    setAttachments([]);
  }

  function onPaste(e: ClipboardEvent) {
    const imgs = imagesFromClipboard(e.clipboardData);
    if (!imgs.length) return; // let normal text paste through
    e.preventDefault(); // don't also dump the image's binary into the textarea
    addImageFiles(imgs);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer ? Array.from(e.dataTransfer.files) : [];
    if (files.some(isImageFile)) addImageFiles(files);
  }

  function onDragOver(e: DragEvent) {
    // Only light up for actual file drags, not text selections.
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes('Files')) return;
    e.preventDefault();
    setDragOver(true);
  }

  function onPickImages(e: Event) {
    const input = e.target as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    if (files.length) addImageFiles(files);
    input.value = ''; // let the same file be picked again later
  }

  async function onSend(e: Event) {
    e.preventDefault();
    if (!canSend) return;
    const text = msg.trim();
    // Append each uploaded image's absolute path on its own line — Claude Code
    // detects the path and reads the image as vision input.
    const paths = readyImages.map((a) => a.path as string);
    const finalText = [text, ...paths].filter(Boolean).join('\n');
    if (!finalText) return;
    setBusy(true);
    setMsg('');
    clearAttachments();
    try {
      await sendFollowup(taskId, finalText);
    } finally {
      setBusy(false);
    }
  }

  // Tapping a quick-reply button sends the option's key ("2", "y", …) as a
  // normal follow-up (submit=true) — the same POST /message path typing would
  // use. Hide the buttons immediately (optimistic); the next poll confirms the
  // task moved on. See the parser in server.py for how options are detected.
  async function onChoose(opt: PromptOption) {
    if (busy || readOnly) return;
    setBusy(true);
    setPrompt(null);
    try {
      await sendFollowup(taskId, String(opt.index));
    } finally {
      setBusy(false);
    }
  }

  function onKey(e: KeyboardEvent) {
    // Cmd/Ctrl+Enter sends; plain Enter inserts a newline.
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void onSend(e);
    }
  }

  return (
    <div class="mc">
      <div class="mc-term">
        <TerminalPane taskId={taskId} />
      </div>

      <form
        class={`mc-composer${dragOver ? ' mc-composer--dragover' : ''}`}
        onSubmit={onSend}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={() => setDragOver(false)}
      >
        {prompt && !readOnly && (
          <div class="mc-choice" role="group" aria-label="Quick reply choices">
            {prompt.question && <div class="mc-choice-q">{prompt.question}</div>}
            <div class="mc-choice-opts">
              {prompt.options.map((o) => (
                <button
                  key={String(o.index)}
                  type="button"
                  class="mc-choice-opt"
                  disabled={busy}
                  onClick={() => void onChoose(o)}
                >
                  <span class="mc-choice-num">{o.index}</span>
                  <span class="mc-choice-text">{o.label}</span>
                </button>
              ))}
            </div>
            <div class="mc-choice-hint">Or type your own reply below.</div>
          </div>
        )}

        {attachments.length > 0 && (
          <div class="mc-attachments">
            {attachments.map((a) => (
              <div
                key={a.id}
                class={`mc-chip mc-chip--${a.status}`}
                title={a.status === 'error' ? 'Upload failed' : a.name}
              >
                <img class="mc-chip-thumb" src={a.previewUrl} alt={a.name} />
                {a.status === 'uploading' && <span class="mc-chip-spinner" />}
                {a.status === 'error' && <span class="mc-chip-badge">!</span>}
                <button
                  type="button"
                  class="mc-chip-remove"
                  title="Remove"
                  onClick={() => removeAttachment(a.id)}
                >
                  <Icon name="close" size={11} />
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={inputRef}
          class="mc-input"
          placeholder={
            readOnly
              ? 'Read-only public demo — sending follow-ups is disabled.'
              : isRunning
                ? (status === 'waiting-for-input'
                    ? 'Task is waiting for your input — reply here. (⌘/Ctrl+Enter to send)'
                    : 'Reply to the assistant…  Paste or drop an image to attach. (⌘/Ctrl+Enter to send)')
                : 'Task is no longer running; replies will be queued.'
          }
          value={msg}
          onInput={(e) => setMsg((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          // rows is just the floor — CSS min-height drives the real height so
          // it can collapse to a single line on mobile (maximizing the terminal
          // viewport) while staying multi-line on desktop.
          rows={1}
          disabled={busy || readOnly}
        />
        <div class="mc-composer-actions">
          <span class="muted mc-hint">
            {uploading ? 'Uploading image…' : 'Sent as a follow-up to the live session above.'}
          </span>
          <div class="mc-composer-buttons">
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              class="mc-file-input"
              onChange={onPickImages}
            />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              iconOnly
              disabled={busy || readOnly}
              title="Attach image"
              onClick={() => fileRef.current?.click()}
            >
              <Icon name="image" size={14} />
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={!canSend}
              title="Send (⌘/Ctrl+Enter)"
            >
              <Icon name="play" size={12} /> Send
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
