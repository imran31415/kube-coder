import { navigate } from '../../store/router';
import { sheetOpen, theme } from '../../store/ui';
import { Icon, type IconName } from '../../components/Icon';
import './more.css';

interface MoreEntry {
  label: string;
  icon: IconName;
  onSelect: () => void;
  hint?: string;
}

export function MoreSheet() {
  const entries: MoreEntry[] = [
    { label: 'Files', icon: 'files', onSelect: () => { navigate('/files'); sheetOpen.value = null; } },
    { label: 'Settings', icon: 'settings', onSelect: () => { navigate('/settings'); sheetOpen.value = null; } },
    {
      label: theme.value === 'light' ? 'Switch to dark' : 'Switch to light',
      icon: theme.value === 'light' ? 'moon' : 'sun',
      onSelect: () => {
        theme.value = theme.value === 'light' ? 'dark' : 'light';
        sheetOpen.value = null;
      },
    },
    {
      label: 'Open legacy dashboard',
      icon: 'chevron-right',
      onSelect: () => {
        window.location.href = '/dashboard-legacy';
      },
    },
  ];
  return (
    <ul class="more-list">
      {entries.map((e) => (
        <li key={e.label}>
          <button class="more-item" onClick={e.onSelect}>
            <span class="more-item-icon"><Icon name={e.icon} size={18} /></span>
            <span class="more-item-label">{e.label}</span>
            <span class="more-item-chev"><Icon name="chevron-right" size={14} /></span>
          </button>
        </li>
      ))}
    </ul>
  );
}
