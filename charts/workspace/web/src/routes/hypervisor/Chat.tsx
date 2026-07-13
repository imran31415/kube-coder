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
  sendMessage,
  stopMessage,
} from '../../store/hypervisor';
import { WorkspaceContext } from './WorkspaceContext';
import { buildTurns, renderMarkdown, type Block } from './transcript';
import { proxyUrl } from '../../api/apps';
import { withOauthPrefix } from '../../api/client';
import { isImageFile, imagesFromClipboard, uploadTaskImage } from '../tasks/imageAttach';

/** A user-attached image being uploaded to the workspace so the agent can read
 *  it — same mechanism as the Build tab: upload, then the file's absolute path
 *  is appended to the outgoing message and Claude reads it by path. */
interface Attachment {
  id: string;
  previewUrl: string;
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

/** One tool/command run — collapsed by default, expandable to the raw detail. */
function ActivityChip({ label, detail, error }: { label: string; detail: string; error?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div class={`hv-activity ${open ? 'is-open' : ''} ${error ? 'is-error' : ''}`}>
      <button type="button" class="hv-activity-head" onClick={() => setOpen((v) => !v)}>
        <span class="hv-activity-icon">
          <Icon name="terminal" size={12} />
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
          <a class="hv-embed-btn" href={`/apps/${port}`}>
            Open <Icon name="link" size={11} />
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

function AgentBlocks({ blocks }: { blocks: Block[] }) {
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
          default:
            return <ActivityChip key={i} label={b.label} detail={b.detail} error={b.error} />;
        }
      })}
    </>
  );
}

export function Chat() {
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  function addFiles(files: File[]) {
    const imgs = files.filter(isImageFile);
    for (const file of imgs) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const previewUrl = URL.createObjectURL(file);
      setAttachments((a) => [...a, { id, previewUrl, status: 'uploading' }]);
      // Reuse the Build tab's uploader; 'hypervisor' → .claude-tasks/hypervisor/attachments.
      void uploadTaskImage('hypervisor', file)
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
      if (x) URL.revokeObjectURL(x.previewUrl);
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
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  function submit(text?: string) {
    if (blocked) return;
    const value = (text ?? draft).trim();
    // Append each uploaded image's absolute path on its own line — Claude Code
    // reads the image by path (same as the Build tab composer).
    const paths = attachments
      .filter((a) => a.status === 'ready' && a.path)
      .map((a) => a.path as string);
    if (!value && paths.length === 0) return;
    const finalText = [value, ...paths].filter(Boolean).join('\n');
    setDraft('');
    attachments.forEach((a) => URL.revokeObjectURL(a.previewUrl));
    setAttachments([]);
    void sendMessage(finalText);
    taRef.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
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

  return (
    <div class="hv-chat">
      {active && <WorkspaceContext />}

      <div class="hv-transcript" ref={scrollRef}>
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
            {turns.map((t, i) =>
              t.role === 'user' ? (
                <div key={i} class="hv-msg hv-msg-user">
                  <div class="hv-bubble">{t.text}</div>
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
                    </div>
                    <AgentBlocks blocks={t.blocks} />
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

      {attachments.length > 0 && (
        <div class="hv-attachments">
          {attachments.map((a) => (
            <div key={a.id} class={`hv-attachment is-${a.status}`}>
              <img src={a.previewUrl} alt="attachment" />
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
          if (files.some(isImageFile)) {
            e.preventDefault();
            addFiles(files);
          }
        }}
        onDragOver={(e) => e.preventDefault()}
      >
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
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
          title="Attach image"
          aria-label="Attach image"
        >
          <Icon name="image" size={16} />
        </button>
        <textarea
          ref={taRef}
          class="hv-composer-input"
          value={draft}
          placeholder={
            readOnly
              ? 'Read-only workspace — you can still ask about state'
              : working
                ? 'Kube-Coder is working… press Stop to interrupt'
                : 'Message Kube-Coder…  (paste or attach an image, Enter to send)'
          }
          onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
          onPaste={(e) => {
            const imgs = imagesFromClipboard(e.clipboardData);
            if (imgs.length) {
              e.preventDefault();
              addFiles(imgs);
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
