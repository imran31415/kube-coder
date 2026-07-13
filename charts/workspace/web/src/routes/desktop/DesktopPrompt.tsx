import { useEffect, useRef, useState } from 'preact/hooks';
import { startBuildFromPrompt, startChatFromPrompt } from '../../store/desktop';
import { listWorkdirs, type WorkdirOption } from '../../api/tasks';
import { Icon } from '../../components/Icon';

/**
 * Hero prompt composer pinned to the top of the Desktop route — the first
 * thing a user sees on a fresh workspace. Type an idea, press Enter, and a
 * Hypervisor chat starts immediately: the prompt seeds a new chat thread and
 * we jump straight into it. A quiet "Start a build instead" toggle flips the
 * composer to the classic build path (hand the prompt to the assistant and
 * open the new build's terminal) for users who want that directly.
 *
 * Kept deliberately minimal — one growing input, a send button, and a quiet
 * footer with a working-directory chip + keyboard hint. Enter submits,
 * Shift+Enter inserts a newline. Hidden entirely in read-only mode (visitors
 * can't start chats or builds); the parent gates on serverMode before
 * rendering us.
 */
const DEFAULT_WORKDIR = '/home/dev';
const MAX_TEXTAREA_PX = 180;

type Mode = 'chat' | 'build';

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
        <div class="dt-composer-main">
          <span class="dt-composer-spark" aria-hidden="true">
            <Icon name={isChat ? 'chat' : 'play'} size={18} />
          </span>
          <textarea
            ref={taRef}
            class="dt-composer-input"
            value={prompt}
            rows={1}
            placeholder={
              isChat
                ? 'Ask your workspace anything…  e.g. “what’s running and how do I deploy?”'
                : 'Describe a build to run…  e.g. “add a dark-mode toggle to the settings page”'
            }
            disabled={busy}
            aria-label={isChat ? 'Chat message' : 'Build prompt'}
            onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
            onKeyDown={onKeyDown}
          />
          <button
            type="submit"
            class="dt-composer-send"
            disabled={!canSend}
            aria-label={isChat ? 'Start chat' : 'Start build'}
            title={isChat ? 'Start chat (Enter)' : 'Start build (Enter)'}
          >
            {busy ? <span class="dt-composer-spinner" /> : <Icon name="play" size={16} />}
          </button>
        </div>

        <div class="dt-composer-foot">
          <label
            class="dt-composer-workdir"
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
            class="dt-composer-mode"
            disabled={busy}
            onClick={() => setMode((m) => (m === 'chat' ? 'build' : 'chat'))}
            title={isChat ? 'Switch to starting a build' : 'Switch to starting a chat'}
          >
            <Icon name={isChat ? 'play' : 'chat'} size={12} />
            {isChat ? 'Start a build instead' : 'Start a chat instead'}
          </button>
          <span class="dt-composer-hint">
            <kbd>Enter</kbd> to {isChat ? 'send' : 'start'} · <kbd>Shift</kbd>+<kbd>Enter</kbd> for
            newline
          </span>
        </div>
      </form>
    </section>
  );
}
