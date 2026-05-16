import { useEffect, useState } from 'preact/hooks';
import { createTask } from '../../store/tasks';
import { listAssistants, listWorkdirs, type AssistantOption, type WorkdirOption } from '../../api/tasks';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Icon } from '../../components/Icon';
import './new-task.css';

export function NewTaskForm({ onClose }: { onClose: () => void }) {
  const [prompt, setPrompt] = useState('');
  const [workdir, setWorkdir] = useState('/home/dev');
  const [assistant, setAssistant] = useState('');
  const [disableMem, setDisableMem] = useState(false);
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
    if (!prompt.trim()) return;
    setBusy(true);
    const task = await createTask({
      prompt: prompt.trim(),
      workdir,
      assistant: assistant || undefined,
      disable_memory_injection: disableMem,
    });
    setBusy(false);
    if (task) onClose();
  }

  return (
    <form class="ntf" onSubmit={onSubmit}>
      <label class="ntf-field">
        <span class="ntf-label">Prompt</span>
        <textarea
          class="ntf-textarea"
          placeholder="What should Claude do?"
          value={prompt}
          onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
          rows={5}
          required
          autoFocus
        />
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

      <label class="ntf-checkbox">
        <input
          type="checkbox"
          checked={disableMem}
          onChange={(e) => setDisableMem((e.target as HTMLInputElement).checked)}
        />
        <span>Don't inject persistent memory into this task's prompt</span>
      </label>

      <div class="ntf-actions">
        <Button variant="ghost" onClick={onClose} type="button">Cancel</Button>
        <Button variant="primary" type="submit" disabled={busy || !prompt.trim()}>
          <Icon name="play" size={14} /> Create task
        </Button>
      </div>
    </form>
  );
}
