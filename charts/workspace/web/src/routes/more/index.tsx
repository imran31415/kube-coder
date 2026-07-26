import { navigate, visibleNavGroups, navLabel } from '../../store/router';
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

interface MoreSection {
  title: string;
  entries: MoreEntry[];
}

const ROUTE_ICONS: Record<string, IconName> = {
  '/mission': 'mission',
  '/hypervisor': 'hypervisor',
  '/tasks': 'tasks',
  '/walkie': 'walkie',
  '/triggers': 'triggers',
  '/desktop': 'desktop',
  '/apps': 'apps',
  '/files': 'files',
  '/memory': 'memory',
  '/skills': 'skills',
  '/docs': 'docs',
};

const ROUTE_HINTS: Record<string, string> = {
  '/mission': 'Every agent — builds, chats, sub-agents — on one board',
  '/walkie': 'Chat with your workspace over the internal loopback preview',
  '/apps': 'Locally-listening services on this workspace',
  '/triggers': 'Webhooks + crons that fire builds',
  '/docs': 'Learn how every feature works',
};

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

function routeEntry(path: string, label?: string): MoreEntry {
  return {
    label: label ?? navLabel(path),
    icon: ROUTE_ICONS[path] ?? 'inbox',
    onSelect: () => { navigate(path); sheetOpen.value = null; },
    hint: ROUTE_HINTS[path],
  };
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
  const quickActions: MoreEntry[] = [
    ...mutatingEntries,
    {
      label: theme.value === 'light' ? 'Switch to dark' : 'Switch to light',
      icon: theme.value === 'light' ? 'moon' : 'sun',
      onSelect: () => {
        theme.value = theme.value === 'light' ? 'dark' : 'light';
        sheetOpen.value = null;
      },
    },
  ];
  // Same categories as the desktop rail (#267) — NAV_GROUPS is the single
  // source of truth. Full groups render here (including routes that also
  // have BottomNav slots) so the sheet doubles as a complete site map.
  const sections: MoreSection[] = [
    { title: 'Quick actions', entries: quickActions },
    ...visibleNavGroups({ ctoEnabled: serverMode.value.ctoEnabled }).map((g) => ({
      title: g.title,
      entries: [
        ...(g.landing ? [routeEntry(g.landing, 'Overview')] : []),
        ...g.items.map((i) => routeEntry(i.path)),
      ],
    })),
  ];
  const settingsEntry = routeEntry('/settings');
  return (
    <div class="more">
      {sections.map((sec) => (
        <section key={sec.title} class="more-section" aria-label={sec.title}>
          <h3 class="more-section-title muted">{sec.title}</h3>
          <ul class="more-list">
            {sec.entries.map((e) => (
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
        </section>
      ))}
      <ul class="more-list">
        <li key="Settings">
          <button class="more-item" type="button" onClick={settingsEntry.onSelect}>
            <span class="more-item-icon"><Icon name="settings" size={18} /></span>
            <div class="more-item-text">
              <span class="more-item-label">Settings</span>
            </div>
            <span class="more-item-chev"><Icon name="chevron-right" size={14} /></span>
          </button>
        </li>
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
