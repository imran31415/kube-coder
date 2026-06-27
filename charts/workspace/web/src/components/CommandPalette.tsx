import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { paletteOpen, theme, drawerOpen, type DrawerKey } from '../store/ui';
import { navigate, ROUTES } from '../store/router';
import { tasks, selectTask } from '../store/tasks';
import { sheetOpen } from '../store/ui';
import { memories, selectMemory, loadSelected } from '../store/memory';
import { triggers, fire } from '../store/triggers';
import { flatPages, loadManifest, manifest } from '../store/docs';
import { useEscape } from '../hooks/useEscape';
import { useScrollLock } from '../hooks/useScrollLock';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { Icon, type IconName } from './Icon';
import { useIsMobile } from '../hooks/useMediaQuery';
import './CommandPalette.css';

export interface PaletteEntry {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon?: IconName;
  match: string;
  onSelect: () => void;
}

function staticActions(): PaletteEntry[] {
  const routes: PaletteEntry[] = ROUTES.map((r) => ({
    id: `route:${r.path}`,
    group: 'Navigation',
    label: `Go to ${r.title}`,
    hint: r.path,
    icon: (r.path.slice(1) as IconName),
    match: `go ${r.title} ${r.path}`.toLowerCase(),
    onSelect: () => navigate(r.path),
  }));
  const actions: PaletteEntry[] = [
    {
      id: 'action:new-task',
      group: 'Actions',
      label: 'New task',
      icon: 'plus',
      match: 'new task create',
      onSelect: () => {
        navigate('/tasks');
        drawerOpen.value = 'new-task' as DrawerKey;
      },
    },
    {
      id: 'action:new-memory',
      group: 'Actions',
      label: 'New memory',
      icon: 'plus',
      match: 'new memory create remember',
      onSelect: () => {
        navigate('/memory');
        drawerOpen.value = 'memory-edit' as DrawerKey;
      },
    },
    {
      id: 'action:new-trigger',
      group: 'Actions',
      label: 'New trigger',
      icon: 'plus',
      match: 'new trigger create cron webhook',
      onSelect: () => {
        navigate('/triggers');
        drawerOpen.value = 'trigger-edit' as DrawerKey;
      },
    },
    {
      id: 'action:toggle-theme',
      group: 'Actions',
      label: 'Toggle theme',
      icon: theme.value === 'light' ? 'moon' : 'sun',
      match: 'toggle theme dark light',
      onSelect: () => {
        theme.value = theme.value === 'light' ? 'dark' : 'light';
      },
    },
  ];
  return [...routes, ...actions];
}

function dataEntries(isMobile: boolean): PaletteEntry[] {
  const out: PaletteEntry[] = [];
  for (const t of tasks.value.slice(0, 50)) {
    out.push({
      id: `task:${t.task_id}`,
      group: 'Builds',
      label: t.name || t.prompt || '(unnamed)',
      hint: t.task_id.slice(0, 18),
      icon: 'tasks',
      match: `task ${t.name ?? ''} ${t.prompt} ${t.status} ${t.task_id}`.toLowerCase(),
      onSelect: () => {
        navigate('/tasks');
        selectTask(t.task_id);
        if (isMobile) sheetOpen.value = 'task-detail';
      },
    });
  }
  for (const m of memories.value.slice(0, 50)) {
    out.push({
      id: `memory:${m.id}`,
      group: 'Memories',
      label: `${m.namespace}.${m.key}`,
      hint: (m.value ?? '').slice(0, 60),
      icon: 'memory',
      match: `memory ${m.namespace} ${m.key} ${m.value ?? ''}`.toLowerCase(),
      onSelect: () => {
        navigate('/memory');
        selectMemory(m);
        void loadSelected(m.namespace, m.key);
        if (isMobile) sheetOpen.value = 'memory-detail';
      },
    });
  }
  for (const t of triggers.value.slice(0, 50)) {
    out.push({
      id: `trigger:${t.kind}:${t.id}`,
      group: 'Triggers',
      label: `${t.kind === 'cron' ? '⏱' : '🪝'} ${t.id}`,
      hint: t.schedule ?? t.prompt.slice(0, 40),
      icon: 'triggers',
      match: `trigger ${t.kind} ${t.id} ${t.prompt}`.toLowerCase(),
      onSelect: () => {
        navigate('/triggers');
        void fire(t);
      },
    });
  }
  for (const p of flatPages.value) {
    out.push({
      id: `docs:${p.id}`,
      group: 'Docs',
      label: p.title,
      hint: p.section,
      icon: 'docs',
      match: `docs ${p.section} ${p.title}`.toLowerCase(),
      onSelect: () => navigate(`/docs/${p.id}`),
    });
  }
  return out;
}

export function CommandPalette() {
  const open = paletteOpen.value;
  const isMobile = useIsMobile();
  const [q, setQ] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const paletteRef = useRef<HTMLDivElement | null>(null);
  useEscape(open, () => (paletteOpen.value = false));
  useScrollLock(open);
  useFocusTrap(open, paletteRef);

  const entries = useMemo(
    () => [...staticActions(), ...dataEntries(isMobile)],
    // Recompute when our live data signals change.
    [tasks.value.length, memories.value.length, triggers.value.length, flatPages.value.length, theme.value, isMobile],
  );
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return entries.slice(0, 12); // show top hits by default
    return entries.filter((e) => e.match.includes(needle) || e.label.toLowerCase().includes(needle));
  }, [entries, q]);

  useEffect(() => {
    if (!open) {
      setQ('');
      setActive(0);
      return;
    }
    // Lazy-load docs manifest the first time the palette opens so doc
    // entries appear without forcing the user to visit /docs first.
    if (!manifest.value) void loadManifest();
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    if (active >= filtered.length) setActive(Math.max(0, filtered.length - 1));
  }, [filtered.length, active]);

  function onKey(e: KeyboardEvent) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((i) => Math.min(filtered.length - 1, i + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const choice = filtered[active];
      if (choice) { choice.onSelect(); paletteOpen.value = false; }
    }
  }

  const groups = useMemo(() => {
    const out = new Map<string, PaletteEntry[]>();
    for (const e of filtered) {
      const arr = out.get(e.group) ?? [];
      arr.push(e);
      out.set(e.group, arr);
    }
    return [...out.entries()];
  }, [filtered]);

  if (!open) return null;

  const activeId = filtered.length > 0 ? `palette-opt-${active}` : undefined;

  return (
    <div class="palette-scrim" onClick={() => (paletteOpen.value = false)}>
      <div ref={paletteRef} class="palette" role="dialog" aria-modal="true" aria-label="Command palette" onClick={(e) => e.stopPropagation()}>
        <div class="palette-input-row">
          <Icon name="search" size={16} />
          <input
            ref={inputRef}
            class="palette-input"
            type="text"
            value={q}
            placeholder="Search tasks, memories, triggers, actions…"
            onInput={(e) => setQ((e.target as HTMLInputElement).value)}
            onKeyDown={onKey}
            role="combobox"
            aria-expanded={true}
            aria-controls="palette-listbox"
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            autoFocus
          />
          <kbd class="palette-kbd">esc</kbd>
        </div>
        <div id="palette-listbox" class="palette-list" role="listbox">
          {groups.length === 0 && <div class="palette-empty muted">No matches.</div>}
          {groups.map(([group, items]) => (
            <div key={group}>
              <div class="palette-group">{group}</div>
              {items.map((e, idx) => {
                const flatIdx = filtered.indexOf(e);
                return (
                  <button
                    key={e.id}
                    id={`palette-opt-${flatIdx}`}
                    role="option"
                    aria-selected={flatIdx === active}
                    class={`palette-row ${flatIdx === active ? 'palette-row-active' : ''}`}
                    onMouseEnter={() => setActive(flatIdx)}
                    onClick={() => { e.onSelect(); paletteOpen.value = false; }}
                    data-group={group}
                    data-idx={idx}
                  >
                    {e.icon && <Icon name={e.icon} size={14} />}
                    <span class="palette-row-label">{e.label}</span>
                    {e.hint && <span class="palette-row-hint">{e.hint}</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
