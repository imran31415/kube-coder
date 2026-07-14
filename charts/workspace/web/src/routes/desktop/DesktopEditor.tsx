import { useState } from 'preact/hooks';
import type { DesktopItem, DesktopItemDraft, DesktopActionType } from '../../api/desktop';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';

export function DesktopEditor({
  initial, onCancel, onSubmit,
}: {
  initial: DesktopItem;
  onCancel: () => void;
  onSubmit: (draft: DesktopItemDraft) => void | Promise<void>;
}) {
  const [label, setLabel] = useState(initial.label);
  const [icon, setIcon] = useState(initial.icon);
  const [hotkey, setHotkey] = useState(initial.hotkey ?? '');
  const [type, setType] = useState<DesktopActionType>(initial.action.type);
  // Per-type fields. We keep all three sets in component state so the
  // user can flip types without losing what they typed.
  const [taskPrompt, setTaskPrompt] = useState(initial.action.type === 'task' ? initial.action.prompt : '');
  const [taskWorkdir, setTaskWorkdir] = useState(initial.action.type === 'task' ? (initial.action.workdir ?? '/home/dev/kube-coder') : '/home/dev/kube-coder');
  const [taskAssistant, setTaskAssistant] = useState(initial.action.type === 'task' ? (initial.action.assistant ?? '') : '');
  const [urlValue, setUrlValue] = useState(initial.action.type === 'url' ? initial.action.url : '');
  const [urlTarget, setUrlTarget] = useState<'blank' | 'self'>(initial.action.type === 'url' ? initial.action.target : 'blank');
  const [shellCmd, setShellCmd] = useState(initial.action.type === 'shell' ? initial.action.command : '');
  const [shellTimeout, setShellTimeout] = useState<string>(initial.action.type === 'shell' && initial.action.timeout ? String(initial.action.timeout) : '30');
  const [busy, setBusy] = useState(false);

  function buildDraft(): DesktopItemDraft | null {
    const cleanLabel = label.trim();
    const cleanIcon = icon.trim();
    if (!cleanLabel || !cleanIcon) return null;
    const draft: DesktopItemDraft = {
      label: cleanLabel,
      icon: cleanIcon,
      action: { type: 'task', prompt: '' } as DesktopItemDraft['action'],
    };
    const cleanHotkey = hotkey.trim();
    if (cleanHotkey) draft.hotkey = cleanHotkey;
    if (type === 'task') {
      if (!taskPrompt.trim()) return null;
      draft.action = {
        type: 'task',
        prompt: taskPrompt.trim(),
        workdir: taskWorkdir.trim() || '/home/dev',
      };
      if (taskAssistant.trim()) (draft.action as { assistant?: string }).assistant = taskAssistant.trim();
    } else if (type === 'url') {
      if (!urlValue.trim()) return null;
      draft.action = { type: 'url', url: urlValue.trim(), target: urlTarget };
    } else {
      if (!shellCmd.trim()) return null;
      draft.action = {
        type: 'shell',
        command: shellCmd.trim(),
        timeout: Math.max(1, Math.min(300, parseInt(shellTimeout, 10) || 30)),
      };
    }
    return draft;
  }

  const draft = buildDraft();
  const canSubmit = !!draft && !busy;

  async function handleSubmit(e: Event) {
    e.preventDefault();
    if (!draft) return;
    setBusy(true);
    try { await onSubmit(draft); } finally { setBusy(false); }
  }

  return (
    <form class="dt-editor" onSubmit={handleSubmit}>
      <div class="dt-editor-row dt-editor-row-pair">
        <label class="dt-editor-field dt-editor-field-icon">
          <span class="dt-editor-label">Icon</span>
          <input
            class="dt-editor-icon-input"
            value={icon}
            onInput={(e) => setIcon((e.target as HTMLInputElement).value)}
            maxLength={8}
            placeholder="✨"
            aria-label="Icon (single emoji or short character)"
          />
        </label>
        <label class="dt-editor-field dt-editor-field-grow">
          <span class="dt-editor-label">Label</span>
          <Input
            fullWidth
            value={label}
            onInput={(e) => setLabel((e.target as HTMLInputElement).value)}
            placeholder="Refactor auth"
            maxLength={80}
          />
        </label>
      </div>

      <label class="dt-editor-field">
        <span class="dt-editor-label">Hotkey <span class="muted">(optional)</span></span>
        <Input
          fullWidth
          value={hotkey}
          onInput={(e) => setHotkey((e.target as HTMLInputElement).value)}
          placeholder="cmd+shift+1"
        />
        <span class="dt-editor-hint muted">
          Combine cmd / ctrl / shift / alt with a single key. Examples: <code>cmd+shift+1</code>, <code>ctrl+l</code>.
        </span>
      </label>

      <fieldset class="dt-editor-action">
        <legend class="dt-editor-label">Action</legend>
        <div class="dt-editor-action-type">
          {(['task', 'url', 'shell'] as DesktopActionType[]).map((t) => (
            <label key={t} class={`dt-editor-radio ${type === t ? 'is-active' : ''}`}>
              <input
                type="radio"
                name="dt-action-type"
                value={t}
                checked={type === t}
                onChange={() => setType(t)}
              />
              <span>{t === 'task' ? 'Build' : t === 'url' ? 'URL' : 'Shell command'}</span>
            </label>
          ))}
        </div>

        {type === 'task' && (
          <div class="dt-editor-fields">
            <label class="dt-editor-field">
              <span class="dt-editor-label">Prompt</span>
              <textarea
                class="dt-editor-textarea"
                value={taskPrompt}
                onInput={(e) => setTaskPrompt((e.target as HTMLTextAreaElement).value)}
                placeholder="What should the build do?"
                rows={4}
              />
            </label>
            <label class="dt-editor-field">
              <span class="dt-editor-label">Workdir</span>
              <Input
                fullWidth
                value={taskWorkdir}
                onInput={(e) => setTaskWorkdir((e.target as HTMLInputElement).value)}
                placeholder="/home/dev/kube-coder"
              />
            </label>
            <label class="dt-editor-field">
              <span class="dt-editor-label">Assistant <span class="muted">(optional)</span></span>
              <Input
                fullWidth
                value={taskAssistant}
                onInput={(e) => setTaskAssistant((e.target as HTMLInputElement).value)}
                placeholder="claude  (or ante / codex / opencode-openrouter)"
              />
            </label>
          </div>
        )}

        {type === 'url' && (
          <div class="dt-editor-fields">
            <label class="dt-editor-field">
              <span class="dt-editor-label">URL</span>
              <Input
                fullWidth
                value={urlValue}
                onInput={(e) => setUrlValue((e.target as HTMLInputElement).value)}
                placeholder="https://example.com  or  /oauth/vscode/?folder=/home/dev"
              />
            </label>
            <div class="dt-editor-radio-row">
              <label class={`dt-editor-radio ${urlTarget === 'blank' ? 'is-active' : ''}`}>
                <input type="radio" name="dt-url-target" checked={urlTarget === 'blank'} onChange={() => setUrlTarget('blank')} />
                <span>New tab</span>
              </label>
              <label class={`dt-editor-radio ${urlTarget === 'self' ? 'is-active' : ''}`}>
                <input type="radio" name="dt-url-target" checked={urlTarget === 'self'} onChange={() => setUrlTarget('self')} />
                <span>Current tab</span>
              </label>
            </div>
          </div>
        )}

        {type === 'shell' && (
          <div class="dt-editor-fields">
            <label class="dt-editor-field">
              <span class="dt-editor-label">Command</span>
              <textarea
                class="dt-editor-textarea mono"
                value={shellCmd}
                onInput={(e) => setShellCmd((e.target as HTMLTextAreaElement).value)}
                placeholder="make test  or  git status"
                rows={3}
              />
              <span class="dt-editor-hint muted">
                Runs as <code>bash -lc &lt;command&gt;</code> with cwd <code>/home/dev</code>. Output surfaces as a toast.
              </span>
            </label>
            <label class="dt-editor-field">
              <span class="dt-editor-label">Timeout (seconds)</span>
              <Input
                fullWidth
                type="number"
                value={shellTimeout}
                onInput={(e) => setShellTimeout((e.target as HTMLInputElement).value)}
                min={1}
                max={300}
              />
            </label>
          </div>
        )}
      </fieldset>

      <div class="dt-editor-footer">
        <Button variant="ghost" type="button" onClick={onCancel}>Cancel</Button>
        <Button variant="primary" type="submit" disabled={!canSubmit}>
          {busy ? 'Saving…' : initial.id ? 'Save changes' : 'Create icon'}
        </Button>
      </div>
    </form>
  );
}
