import { navigate } from '../../store/router';
import { sheetOpen, theme } from '../../store/ui';
import { Icon, type IconName } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
import { createTerminalTask, terminalUrl, vscodeUrl } from '../../api/tasks';
import { refreshTasks } from '../../store/tasks';
import { serverMode } from '../../store/server-mode';
import './more.css';

interface MoreEntry {
  label: string;
  icon: IconName;
  onSelect: () => void;
  hint?: string;
}

async function openNewTerminalMobile() {
  const win = window.open('about:blank', '_blank');
  if (win) win.opener = null;
  try {
    await createTerminalTask();
    void refreshTasks();
  } catch {
    /* fall through, still open ttyd */
  }
  const url = terminalUrl();
  if (win && !win.closed) win.location.replace(url);
  else window.location.href = url;
  sheetOpen.value = null;
}

export function MoreSheet() {
  // Read-only public demo hides the two "open a new editor session" entries.
  // Visitors can browse but not spawn new terminals / VS Code instances.
  const mutatingEntries: MoreEntry[] = serverMode.value.readOnly ? [] : [
    {
      label: 'New terminal',
      icon: 'play',
      onSelect: () => { void openNewTerminalMobile(); },
      hint: 'Register a bash task and open ttyd in a new tab',
    },
    {
      label: 'Open VS Code',
      icon: 'files',
      onSelect: () => {
        window.open(vscodeUrl('/home/dev'), '_blank');
        sheetOpen.value = null;
      },
      hint: 'code-server at /home/dev',
    },
  ];
  const entries: MoreEntry[] = [
    ...mutatingEntries,
    // BottomNav surfaces Desktop / Build / Memory; everything below is
    // the "secondary" set the More sheet absorbs. Triggers moved here
    // when BottomNav switched its third slot to Desktop.
    {
      label: 'Walkie-Talkie',
      icon: 'walkie',
      onSelect: () => { navigate('/walkie'); sheetOpen.value = null; },
      hint: 'Chat with your workspace over the internal loopback preview',
    },
    {
      label: 'Apps',
      icon: 'apps',
      onSelect: () => { navigate('/apps'); sheetOpen.value = null; },
      hint: 'Locally-listening services on this workspace',
    },
    {
      label: 'Triggers',
      icon: 'triggers',
      onSelect: () => { navigate('/triggers'); sheetOpen.value = null; },
      hint: 'Webhooks + crons that fire builds',
    },
    {
      label: 'Files',
      icon: 'files',
      onSelect: () => { navigate('/files'); sheetOpen.value = null; },
    },
    {
      label: 'Docs',
      icon: 'docs',
      onSelect: () => { navigate('/docs'); sheetOpen.value = null; },
      hint: 'Learn how every feature works',
    },
    {
      label: 'Settings',
      icon: 'settings',
      onSelect: () => { navigate('/settings'); sheetOpen.value = null; },
    },
    {
      label: theme.value === 'light' ? 'Switch to dark' : 'Switch to light',
      icon: theme.value === 'light' ? 'moon' : 'sun',
      onSelect: () => {
        theme.value = theme.value === 'light' ? 'dark' : 'light';
        sheetOpen.value = null;
      },
    },
  ];
  return (
    <div class="more">
      <ul class="more-list">
        {entries.map((e) => (
          <li key={e.label}>
            <button class="more-item" type="button" onClick={e.onSelect}>
              <span class="more-item-icon"><Icon name={e.icon} size={18} /></span>
              <div class="more-item-text">
                <span class="more-item-label">{e.label}</span>
                {e.hint && <span class="more-item-hint muted">{e.hint}</span>}
              </div>
              <span class="more-item-chev"><Icon name="chevron-right" size={14} /></span>
            </button>
          </li>
        ))}
      </ul>
      <div class="more-footer">
        <Button
          variant="secondary"
          onClick={() => (sheetOpen.value = null)}
          style={{ width: '100%' }}
          title="Dismiss this menu"
        >
          <Icon name="close" size={14} /> Done
        </Button>
      </div>
    </div>
  );
}
