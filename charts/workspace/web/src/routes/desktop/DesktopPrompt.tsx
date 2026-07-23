import { useEffect, useRef, useState } from 'preact/hooks';
import { startBuildFromPrompt, startChatFromPrompt } from '../../store/desktop';
import { listWorkdirs, type WorkdirOption } from '../../api/tasks';
import { Icon } from '../../components/Icon';

/**
 * Centered "new chat" composer — the hero of the Desktop route (#433). One
 * calm rounded container in the ChatGPT/Claude shape: an auto-growing
 * textarea on top, and a quiet control row inside the box's bottom edge
 * holding the workdir picker and the chat/build mode as small pills, with
 * the send button at the right. Enter submits, Shift+Enter inserts a
 * newline; the keyboard hint only appears while the composer is focused.
 * Hidden entirely in read-only mode (the parent gates on serverMode).
 */
const DEFAULT_WORKDIR = '/home/dev';
const MAX_TEXTAREA_PX = 180;

type Mode = 'chat' | 'build';

/** Suggestion chips under the composer — common ways to start. */
const SUGGESTIONS: { label: string; prompt: string }[] = [
  { label: 'Fix a bug', prompt: 'Fix this bug: ' },
  { label: 'Clone a repo', prompt: 'Clone the repo ' },
  { label: 'New app', prompt: 'Scaffold a new app that ' },
];

export function DesktopPrompt() {
  const [prompt, setPrompt] = useState('');
  const [workdir, setWorkdir] = useState(DEFAULT_WORKDIR);
  const [dirs, setDirs] = useState<WorkdirOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<Mode>('chat');
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    listWorkdirs()
      .then((list) => {
        setDirs(list);
        // Prefer the repo checkout if the server offers one, else keep the
        // workspace root default. Never override a dir the user already picked.
        const preferred =
          list.find((d) => d.path === DEFAULT_WORKDIR) ?? list.find((d) => d.is_git) ?? list[0];
        if (preferred) setWorkdir((cur) => (cur === DEFAULT_WORKDIR ? preferred.path : cur));
      })
      .catch(() => setDirs([]));
  }, []);

  // Auto-grow the textarea with content up to a cap, then let it scroll.
  function autosize() {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }
  useEffect(autosize, [prompt]);

  async function submit() {
    if (busy) return;
    const text = prompt.trim();
    if (!text) return;
    setBusy(true);
    const ok =
      mode === 'chat'
        ? await startChatFromPrompt(text, workdir)
        : await startBuildFromPrompt(text, workdir);
    setBusy(false);
    if (ok) setPrompt('');
  }

  function onKeyDown(e: KeyboardEvent) {
    // Enter submits; Shift+Enter (and IME composition) insert a newline.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      void submit();
    }
  }

  function applySuggestion(text: string) {
    setPrompt(text);
    taRef.current?.focus();
  }

  const canSend = prompt.trim().length > 0 && !busy;
  const isChat = mode === 'chat';

  return (
    <section
      class="dt-composer"
      data-dt-stop="true"
      aria-label={isChat ? 'Start a chat' : 'Start a build'}
    >
      <form
        class={`dt-composer-box ${busy ? 'is-busy' : ''}`}
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <textarea
          ref={taRef}
          class="dt-composer-input"
          value={prompt}
          rows={1}
          placeholder={
            isChat
              ? 'Ask anything or start a build…'
              : 'Describe a build to run…  e.g. “add a dark-mode toggle to the settings page”'
          }
          disabled={busy}
          aria-label={isChat ? 'Chat message' : 'Build prompt'}
          onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
        />
        <div class="dt-composer-controls">
          <label
            class="dt-composer-pill dt-composer-workdir"
            title={isChat ? 'Working directory for the chat' : 'Working directory for the build'}
          >
            <Icon name="files" size={12} />
            {dirs.length > 0 ? (
              <select
                value={workdir}
                disabled={busy}
                onChange={(e) => setWorkdir((e.target as HTMLSelectElement).value)}
              >
                {dirs.map((d) => (
                  <option key={d.path} value={d.path}>
                    {d.label ?? d.path}
                  </option>
                ))}
              </select>
            ) : (
              <span class="dt-composer-workdir-static">{workdir}</span>
            )}
          </label>
          <button
            type="button"
            class="dt-composer-pill dt-composer-mode"
            disabled={busy}
            aria-label={isChat ? 'Mode: chat — switch to build' : 'Mode: build — switch to chat'}
            onClick={() => setMode((m) => (m === 'chat' ? 'build' : 'chat'))}
            title={isChat ? 'Switch to starting a build' : 'Switch to starting a chat'}
          >
            <Icon name={isChat ? 'chat' : 'play'} size={12} />
            {isChat ? 'Chat' : 'Build'}
            <Icon name="chevron-down" size={10} />
          </button>
          <span class="dt-composer-hint">
            <kbd>Enter</kbd> to {isChat ? 'send' : 'start'} · <kbd>Shift</kbd>+<kbd>Enter</kbd> for
            newline
          </span>
          <button
            type="submit"
            class="dt-composer-send"
            disabled={!canSend}
            aria-label={isChat ? 'Start chat' : 'Start build'}
            title={isChat ? 'Start chat (Enter)' : 'Start build (Enter)'}
          >
            {busy ? <span class="dt-composer-spinner" /> : <Icon name="arrow-up" size={16} />}
          </button>
        </div>
      </form>

      <div class="dt-composer-chips" aria-label="Suggestions">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.label}
            type="button"
            class="dt-composer-chip"
            disabled={busy}
            onClick={() => applySuggestion(s.prompt)}
          >
            {s.label}
          </button>
        ))}
      </div>
    </section>
  );
}
