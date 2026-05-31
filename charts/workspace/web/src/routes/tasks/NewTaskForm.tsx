import { useEffect, useState } from 'preact/hooks';
import { createTask, selectTask } from '../../store/tasks';
import { listAssistants, listWorkdirs, renameTask, type AssistantOption, type WorkdirOption } from '../../api/tasks';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Icon } from '../../components/Icon';
import { randomBuildName } from '../../util/randomName';
import { navigate, currentPath } from '../../store/router';
import { sheetOpen } from '../../store/ui';
import { useIsMobile } from '../../hooks/useMediaQuery';
import './new-task.css';

/**
 * "New build" composer — intentionally minimal. The user gives the session a
 * memorable name (defaults to e.g. funny-kitty-37) and we drop them straight
 * into the live Claude/OpenCode terminal — they type their actual prompt
 * there. Server.py allows an empty prompt for exactly this flow.
 */
export function NewTaskForm({ onClose }: { onClose: () => void }) {
  const isMobile = useIsMobile();
  const [name, setName] = useState(() => randomBuildName());
  const [workdir, setWorkdir] = useState('/home/dev');
  const [assistant, setAssistant] = useState('');
  const [dirs, setDirs] = useState<WorkdirOption[]>([]);
  const [assistants, setAssistants] = useState<AssistantOption[]>([]);
  const [busy, setBusy] = useState(false);

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
    // Empty prompt — the assistant boots into an interactive REPL; the
    // user's first message is whatever they type in the terminal.
    const task = await createTask({
      prompt: '',
      workdir,
      assistant: assistant || undefined,
      disable_memory_injection: false,
    });
    setBusy(false);
    if (task && task.task_id) {
      // If the server accepted no name, leave as-is; otherwise rename in the
      // background (best-effort — failure is OK, the random name still shows).
      if (name && name !== task.name) {
        void renameTask(task.task_id, name).catch(() => undefined);
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
  }

  function reroll() {
    setName(randomBuildName());
  }

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
            {assistants.length === 0 && <option value="">claude (default)</option>}
            {assistants.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label || a.id}
                {a.default ? ' · default' : ''}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p class="ntf-note muted">
        You'll be dropped straight into a live <strong>{assistant || 'claude'}</strong> terminal —
        type your first prompt there.
      </p>

      <div class="ntf-actions">
        <Button variant="ghost" onClick={onClose} type="button">Cancel</Button>
        <Button variant="primary" type="submit" disabled={busy}>
          <Icon name="play" size={14} /> {busy ? 'Starting…' : 'Start build'}
        </Button>
      </div>
    </form>
  );
}
