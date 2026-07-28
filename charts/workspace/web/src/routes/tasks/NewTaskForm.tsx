import { useEffect, useState } from 'preact/hooks';
import { createTask, selectTask } from '../../store/tasks';
import { listAssistants, listWorkdirs, renameTask, type AssistantOption, type WorkdirOption } from '../../api/tasks';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Icon } from '../../components/Icon';
import { randomBuildName } from '../../util/randomName';
import { navigate, currentPath } from '../../store/router';
import { pushToast, sheetOpen } from '../../store/ui';
import {
  deleteTemplate,
  promptTemplates,
  saveTemplate,
  suggestTemplateName,
} from '../../store/promptTemplates';
import { useIsMobile } from '../../hooks/useMediaQuery';
import './new-task.css';

/**
 * "New build" composer. The user names the session (defaults to e.g.
 * funny-kitty-37) and can seed the first prompt right here — it's sent with
 * the create call so the agent starts working immediately (#94). Leaving the
 * prompt empty is still supported: server.py accepts an empty prompt and the
 * assistant boots into an interactive REPL the user types into.
 *
 * Prompts worth reusing can be saved as templates — client-side, in
 * localStorage (see store/promptTemplates.ts).
 */
export function NewTaskForm({ onClose }: { onClose: () => void }) {
  const isMobile = useIsMobile();
  const [name, setName] = useState(() => randomBuildName());
  const [prompt, setPrompt] = useState('');
  const [templateName, setTemplateName] = useState<string | null>(null);
  const [workdir, setWorkdir] = useState('/home/dev');
  const [assistant, setAssistant] = useState('');
  const [dirs, setDirs] = useState<WorkdirOption[]>([]);
  const [assistants, setAssistants] = useState<AssistantOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listWorkdirs().then(setDirs).catch(() => setDirs([]));
    listAssistants().then((list) => {
      setAssistants(list);
      const def = list.find((a) => a.default) ?? list[0];
      if (def) setAssistant(def.id);
    }).catch(() => setAssistants([]));
  }, []);

  async function onSubmit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    // A non-empty prompt boots the session already working on it; an empty
    // one drops the user into an interactive REPL and their first message is
    // whatever they type in the terminal. Both are valid server-side.
    // createTask catches API errors internally and returns null.
    let task = null;
    try {
      task = await createTask({
        prompt: prompt.trim(),
        workdir,
        assistant: assistant || undefined,
        disable_memory_injection: false,
      });
    } finally {
      setBusy(false);
    }
    if (!task || !task.task_id) {
      setError('Could not start the build — check the workspace server and try again.');
      return;
    }
    // The create endpoint takes no name, so the rename is a second call.
    // Await it and say so when it fails — previously this was fire-and-forget
    // and the user silently kept the random name (#94).
    if (name && name !== task.name) {
      const renamed = await renameTask(task.task_id, name)
        .then(() => true)
        .catch(() => false);
      if (!renamed) {
        pushToast(`Build started, but couldn't rename it to "${name}".`, { kind: 'warn' });
      }
    }
    // Drop the user straight into the new build's terminal:
    //   - if they're on a different route, navigate to /tasks first
    //   - on mobile, open the task-detail BottomSheet (the master/detail
    //     pane that auto-renders on desktop doesn't exist on phones)
    //   - select the task so TaskDetail mounts on the Terminal tab
    //     (running tasks default to Terminal per TaskDetail's useEffect)
    if (!currentPath.value.startsWith('/tasks')) {
      navigate('/tasks');
    }
    selectTask(task.task_id);
    if (isMobile) sheetOpen.value = 'task-detail';
    onClose();
  }

  function reroll() {
    setName(randomBuildName());
  }

  function beginSaveTemplate() {
    setTemplateName(suggestTemplateName(prompt));
  }

  function commitTemplate(e: Event) {
    // Nested inside the build <form>, so this is a plain button + handler:
    // saving a template must never submit (and start) the build.
    e.preventDefault();
    if (saveTemplate(templateName ?? '', prompt)) setTemplateName(null);
  }

  // The currently-selected assistant (for the free/training-disclosure note),
  // and the label to show as the default before the list resolves. Falls back
  // to 'claude' only when the server list hasn't loaded yet.
  const selectedAssistant = assistants.find((a) => a.id === assistant);
  const defaultAssistantLabel =
    (assistants.find((a) => a.default) ?? assistants[0])?.label ?? 'claude';

  return (
    <form class="ntf" onSubmit={onSubmit}>
      <label class="ntf-field">
        <span class="ntf-label">
          Build name
          <button
            type="button"
            class="ntf-reroll"
            onClick={reroll}
            title="Generate a new random name"
            aria-label="Generate a new random name"
          >
            ↻
          </button>
        </span>
        <Input
          fullWidth
          value={name}
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
          placeholder="funny-kitty-37"
          autoFocus
          maxLength={64}
        />
        <span class="ntf-hint muted">
          Display only — pick anything memorable, or use the random suggestion.
        </span>
      </label>

      <div class="ntf-row">
        <label class="ntf-field">
          <span class="ntf-label">Working directory</span>
          {dirs.length > 0 ? (
            <select
              class="ntf-select"
              value={workdir}
              onChange={(e) => setWorkdir((e.target as HTMLSelectElement).value)}
            >
              {dirs.map((d) => (
                <option key={d.path} value={d.path}>
                  {d.label ?? d.path}
                  {d.is_git ? '  (git)' : ''}
                </option>
              ))}
            </select>
          ) : (
            <Input
              fullWidth
              value={workdir}
              onInput={(e) => setWorkdir((e.target as HTMLInputElement).value)}
            />
          )}
        </label>

        <label class="ntf-field">
          <span class="ntf-label">Assistant</span>
          <select
            class="ntf-select"
            value={assistant}
            onChange={(e) => setAssistant((e.target as HTMLSelectElement).value)}
          >
            {assistants.length === 0 && <option value="">{defaultAssistantLabel} (default)</option>}
            {assistants.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label || a.id}
                {a.free ? ' · free' : ''}
                {a.default ? ' · default' : ''}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label class="ntf-field">
        <span class="ntf-label">First prompt <span class="muted">(optional)</span></span>
        <textarea
          class="ntf-textarea"
          value={prompt}
          onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
          placeholder="e.g. Run the test suite in ./api, fix whatever fails, and open a PR."
          rows={5}
          aria-label="First prompt"
        />
        <span class="ntf-hint muted">
          Sent as the build's first message. Leave it empty to land in the terminal and type there.
        </span>
      </label>

      <div class="ntf-templates">
        <div class="ntf-templates-head">
          <span class="ntf-label">Saved prompts</span>
          {templateName === null ? (
            <button
              type="button"
              class="ntf-tpl-save"
              onClick={beginSaveTemplate}
              disabled={!prompt.trim()}
              title={
                prompt.trim()
                  ? 'Save this prompt for reuse'
                  : 'Type a prompt above to save it as a template'
              }
            >
              <Icon name="plus" size={12} /> Save as template
            </button>
          ) : (
            <button type="button" class="ntf-tpl-save" onClick={() => setTemplateName(null)}>
              Cancel
            </button>
          )}
        </div>

        {templateName !== null && (
          <div class="ntf-tpl-namerow">
            <Input
              fullWidth
              value={templateName}
              onInput={(e) => setTemplateName((e.target as HTMLInputElement).value)}
              placeholder="Template name"
              aria-label="Template name"
              maxLength={60}
              autoFocus
            />
            <Button
              variant="secondary"
              size="sm"
              type="button"
              onClick={commitTemplate}
              disabled={!templateName.trim()}
            >
              Save
            </Button>
          </div>
        )}

        {promptTemplates.value.length === 0 ? (
          <span class="ntf-hint muted">
            No templates yet — save a prompt you run often and it'll show up here.
          </span>
        ) : (
          <ul class="ntf-tpl-list">
            {promptTemplates.value.map((t) => (
              <li key={t.id} class="ntf-tpl">
                <button
                  type="button"
                  class="ntf-tpl-apply"
                  onClick={() => setPrompt(t.prompt)}
                  title={t.prompt}
                >
                  {t.name}
                </button>
                <button
                  type="button"
                  class="ntf-tpl-del"
                  onClick={() => deleteTemplate(t.id)}
                  aria-label={`Delete template ${t.name}`}
                  title={`Delete template ${t.name}`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <p class="ntf-note muted">
        {prompt.trim() ? (
          <>
            <strong>{assistant || defaultAssistantLabel}</strong> starts on this prompt right away —
            you can watch and steer it from the build's terminal.
          </>
        ) : (
          <>
            You'll be dropped straight into a live <strong>{assistant || defaultAssistantLabel}</strong> terminal —
            type your first prompt there.
          </>
        )}
      </p>

      {selectedAssistant?.trainingDisclosure && (
        <p class="ntf-note ntf-disclosure" role="note">
          ⚠️ Free {selectedAssistant.label} models may use your prompts and code to
          improve the model — avoid sending confidential data. Add your own key in
          Settings → Provider keys to switch providers.
        </p>
      )}

      {error && (
        <p class="ntf-error" role="alert">
          {error}
        </p>
      )}

      <div class="ntf-actions">
        <Button variant="ghost" onClick={onClose} type="button">Cancel</Button>
        <Button variant="primary" type="submit" disabled={busy}>
          <Icon name="play" size={14} /> {busy ? 'Starting…' : 'Start build'}
        </Button>
      </div>
    </form>
  );
}
