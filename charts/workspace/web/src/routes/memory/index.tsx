import { useEffect, useMemo, useState } from 'preact/hooks';
import {
  filteredMemories,
  memoryFilter,
  memoryNamespaceFacet,
  memories,
  namespaces,
  selectedMemory,
  selectMemory,
  loadSelected,
  startMemoryPolling,
  stopMemoryPolling,
  removeMemory,
  saveMemory,
} from '../../store/memory';
import { sheetOpen, drawerOpen, type DrawerKey } from '../../store/ui';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { Drawer } from '../../components/Drawer';
import { BottomSheet } from '../../components/BottomSheet';
import {
  getMemoryHistory,
  getMemoryNeighbors,
  type MemoryRecord,
  type MemoryUpsertInput,
} from '../../api/memory';
import './memory.css';

export function MemoryRoute() {
  const isMobile = useIsMobile();
  const [editing, setEditing] = useState<MemoryUpsertInput | null>(null);

  useEffect(() => {
    startMemoryPolling(30000);
    return () => stopMemoryPolling();
  }, []);

  function onNew() {
    setEditing({ namespace: 'user.', key: '', value: '', kind: 'semantic', importance: 0.6 });
    drawerOpen.value = 'memory-edit' as DrawerKey;
  }
  function onEdit(m: MemoryRecord) {
    setEditing({
      namespace: m.namespace,
      key: m.key,
      value: m.value,
      kind: m.kind,
      importance: m.importance,
      tags: m.tags_list,
    });
    drawerOpen.value = 'memory-edit' as DrawerKey;
  }
  function onRowClick(m: MemoryRecord) {
    selectMemory(m);
    void loadSelected(m.namespace, m.key);
    if (isMobile) sheetOpen.value = 'memory-detail';
  }

  const list = filteredMemories.value;

  return (
    <div class="route route-memory">
      <header class="route-header route-header-with-action">
        <div>
          <h1 class="route-title">Memory</h1>
          <p class="route-subtitle muted">
            Facts that persist across sessions — {memories.value.length} entries, filterable by namespace and content.
          </p>
        </div>
        <Button variant="primary" size="md" onClick={onNew}>
          <Icon name="plus" size={14} /> New memory
        </Button>
      </header>

      <div class="mem-layout">
        <div class="mem-master">
          <MemoryToolbar />
          {list.length === 0 ? (
            <EmptyState
              icon={<Icon name="memory" size={24} />}
              title={memoryFilter.value || memoryNamespaceFacet.value ? 'No matches' : 'No memories yet'}
              description={
                memoryFilter.value || memoryNamespaceFacet.value
                  ? 'Try clearing the filter.'
                  : 'Memories persist facts about you and your projects across Claude sessions.'
              }
              action={
                !memoryFilter.value && !memoryNamespaceFacet.value && (
                  <Button variant="primary" onClick={onNew}>
                    <Icon name="plus" size={14} /> Create one
                  </Button>
                )
              }
            />
          ) : (
            <MemoryList list={list} onRowClick={onRowClick} onEdit={onEdit} />
          )}
        </div>
        {!isMobile && (
          <div class="mem-detail-pane">
            <MemoryDetail onEdit={onEdit} />
          </div>
        )}
      </div>

      <BottomSheet
        open={isMobile && sheetOpen.value === 'memory-detail'}
        onClose={() => {
          sheetOpen.value = null;
          selectMemory(null);
        }}
        initialSnap="full"
      >
        <MemoryDetail onEdit={onEdit} />
      </BottomSheet>

      {!isMobile ? (
        <Drawer
          open={drawerOpen.value === ('memory-edit' as DrawerKey)}
          onClose={() => (drawerOpen.value = null)}
          title={editing?.namespace && editing.key ? 'Edit memory' : 'New memory'}
          width={560}
        >
          {editing && (
            <MemoryForm
              initial={editing}
              onCancel={() => (drawerOpen.value = null)}
              onSubmit={async (input) => {
                const m = await saveMemory(input);
                if (m) {
                  drawerOpen.value = null;
                  setEditing(null);
                }
              }}
            />
          )}
        </Drawer>
      ) : (
        <BottomSheet
          open={drawerOpen.value === ('memory-edit' as DrawerKey)}
          onClose={() => (drawerOpen.value = null)}
          initialSnap="full"
          title={editing?.namespace && editing.key ? 'Edit memory' : 'New memory'}
        >
          {editing && (
            <MemoryForm
              initial={editing}
              onCancel={() => (drawerOpen.value = null)}
              onSubmit={async (input) => {
                const m = await saveMemory(input);
                if (m) {
                  drawerOpen.value = null;
                  setEditing(null);
                  // Mobile-only: the master/detail layout doesn't render on
                  // phones, so we open the detail sheet so the user sees
                  // what they just saved instead of landing on the list.
                  sheetOpen.value = 'memory-detail';
                }
              }}
            />
          )}
        </BottomSheet>
      )}
    </div>
  );
}

function MemoryToolbar() {
  const ns = namespaces.value;
  return (
    <div class="mem-toolbar">
      <Input
        fullWidth
        placeholder="Search namespace, key, or content…"
        value={memoryFilter.value}
        onInput={(e) => (memoryFilter.value = (e.target as HTMLInputElement).value)}
        aria-label="Filter memories"
      />
      <div class="mem-namespace-row" role="tablist" aria-label="Namespace facets">
        <button
          class={`mem-ns ${memoryNamespaceFacet.value == null ? 'mem-ns-active' : ''}`}
          onClick={() => (memoryNamespaceFacet.value = null)}
        >
          All
        </button>
        {ns.map((n) => (
          <button
            key={n}
            class={`mem-ns ${memoryNamespaceFacet.value === n ? 'mem-ns-active' : ''}`}
            onClick={() => (memoryNamespaceFacet.value = memoryNamespaceFacet.value === n ? null : n)}
          >
            {n}
          </button>
        ))}
      </div>
    </div>
  );
}

function MemoryList({ list, onRowClick }: { list: MemoryRecord[]; onRowClick: (m: MemoryRecord) => void; onEdit: (m: MemoryRecord) => void }) {
  return (
    <ul class="mem-list" role="list">
      {list.map((m) => {
        const active = selectedMemory.value?.id === m.id;
        const importance = Math.round(m.importance * 100);
        return (
          <li key={m.id}>
            <button class={`mem-row ${active ? 'mem-row-active' : ''}`} onClick={() => onRowClick(m)}>
              <div class="mem-row-head">
                <span class="mem-row-ns mono">{m.namespace}.{m.key}</span>
                <Pill tone="neutral" mono>{m.kind}</Pill>
                <span class="mem-row-imp muted mono">{importance}%</span>
              </div>
              <div class="mem-row-value muted">{m.value.slice(0, 180)}{m.value.length > 180 ? '…' : ''}</div>
              {m.tags_list.length > 0 && (
                <div class="mem-row-tags">
                  {m.tags_list.slice(0, 4).map((t) => (
                    <span class="mem-tag" key={t}>#{t}</span>
                  ))}
                  {m.tags_list.length > 4 && <span class="muted">+{m.tags_list.length - 4}</span>}
                </div>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

type MemTab = 'value' | 'history' | 'relations';

function MemoryDetail({ onEdit }: { onEdit: (m: MemoryRecord) => void }) {
  const m = selectedMemory.value;
  const [tab, setTab] = useState<MemTab>('value');
  const [history, setHistory] = useState<MemoryRecord[] | null>(null);
  const [neighbors, setNeighbors] = useState<{ nodes: MemoryRecord[]; edges: { from_id: number; to_id: number; kind: string; weight: number }[] } | null>(null);

  const cacheKey = useMemo(() => (m ? `${m.namespace}/${m.key}` : ''), [m]);

  useEffect(() => {
    setHistory(null);
    setNeighbors(null);
    setTab('value');
  }, [cacheKey]);

  useEffect(() => {
    if (!m) return;
    if (tab === 'history' && history == null) {
      getMemoryHistory(m.namespace, m.key)
        .then((r) => setHistory(r.versions))
        .catch(() => setHistory([]));
    }
    if (tab === 'relations' && neighbors == null) {
      getMemoryNeighbors(m.namespace, m.key, 1)
        .then(setNeighbors)
        .catch(() => setNeighbors({ nodes: [], edges: [] }));
    }
  }, [tab, m, history, neighbors]);

  if (!m) {
    return (
      <EmptyState
        icon={<Icon name="memory" size={24} />}
        title="Select a memory"
        description="Pick a memory from the list to view its full value, version history, and relations."
      />
    );
  }
  return (
    <article class="md">
      <header class="md-header">
        <div class="md-headline">
          <span class="md-ns mono">{m.namespace}.{m.key}</span>
          <Pill tone="neutral" mono>{m.kind}</Pill>
        </div>
        <div class="md-actions">
          <Button size="sm" variant="ghost" onClick={() => onEdit(m)}>Edit</Button>
          <Button
            size="sm"
            variant="danger"
            onClick={() => {
              if (confirm(`Delete ${m.namespace}.${m.key}? It will be soft-deleted and remain in history.`)) {
                void removeMemory(m.namespace, m.key);
              }
            }}
          >
            Delete
          </Button>
        </div>
      </header>
      <div class="md-meta muted">
        importance {Math.round(m.importance * 100)}% · v{m.version} · {m.access_count} reads ·
        updated {new Date(m.updated_at * 1000).toLocaleString()}
      </div>
      <nav class="md-tabs" role="tablist">
        {(['value', 'history', 'relations'] as MemTab[]).map((id) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            class={`td-tab ${tab === id ? 'td-tab-active' : ''}`}
            onClick={() => setTab(id)}
          >
            {id === 'value' ? 'Value' : id === 'history' ? `History${history ? ` (${history.length})` : ''}` : 'Relations'}
          </button>
        ))}
      </nav>
      {tab === 'value' && (
        <>
          <pre class="md-value">{m.value}</pre>
          {m.tags_list.length > 0 && (
            <div class="md-tags">
              {m.tags_list.map((t) => (
                <span key={t} class="mem-tag">#{t}</span>
              ))}
            </div>
          )}
          {m.source && <div class="md-source muted mono">source: {m.source}</div>}
        </>
      )}
      {tab === 'history' && (
        <div class="md-tab-body">
          {history == null ? (
            <p class="muted">Loading…</p>
          ) : history.length === 0 ? (
            <p class="muted">No previous versions.</p>
          ) : (
            <ol class="md-history">
              {history.map((v) => (
                <li key={v.version} class="md-history-item">
                  <div class="md-history-head">
                    <Pill tone="neutral" mono>v{v.version}</Pill>
                    <span class="muted mono">{new Date(v.updated_at * 1000).toLocaleString()}</span>
                  </div>
                  <pre class="md-history-value">{v.value.slice(0, 600)}{v.value.length > 600 ? '…' : ''}</pre>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      {tab === 'relations' && (
        <div class="md-tab-body">
          {neighbors == null ? (
            <p class="muted">Loading…</p>
          ) : neighbors.edges.length === 0 ? (
            <p class="muted">No relations recorded for this entry yet.</p>
          ) : (
            <ul class="md-relations">
              {neighbors.edges.map((e) => {
                const other = neighbors.nodes.find((n) => n.id === (e.from_id === m.id ? e.to_id : e.from_id));
                return (
                  <li key={`${e.from_id}-${e.to_id}-${e.kind}`}>
                    <Pill tone="info" mono>{e.kind}</Pill>
                    {' → '}
                    <span class="mono">{other ? `${other.namespace}.${other.key}` : '?'}</span>
                    <span class="muted mono"> · weight {e.weight.toFixed(2)}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </article>
  );
}

function MemoryForm({
  initial,
  onSubmit,
  onCancel,
}: {
  initial: MemoryUpsertInput;
  onSubmit: (input: MemoryUpsertInput) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [namespace, setNamespace] = useState(initial.namespace);
  const [key, setKey] = useState(initial.key);
  const [value, setValue] = useState(initial.value);
  const [kind, setKind] = useState(initial.kind ?? 'semantic');
  const [importance, setImportance] = useState(initial.importance ?? 0.6);
  const [tags, setTags] = useState((initial.tags ?? []).join(', '));
  const [expires, setExpires] = useState<number | ''>('');
  const [busy, setBusy] = useState(false);

  const valid = /^[a-z0-9._-]+$/i.test(namespace) && /^[a-z0-9._-]+$/i.test(key) && value.trim().length > 0;

  async function onFormSubmit(e: Event) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    await onSubmit({
      namespace,
      key,
      value,
      kind,
      importance,
      tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
      ...(expires === '' ? {} : { expires_in_days: Number(expires) }),
    });
    setBusy(false);
  }

  return (
    <form class="mf" onSubmit={onFormSubmit}>
      <div class="mf-row">
        <label class="mf-field">
          <span class="mf-label">Namespace</span>
          <Input
            fullWidth
            value={namespace}
            placeholder="user.preferences"
            onInput={(e) => setNamespace((e.target as HTMLInputElement).value)}
            required
          />
        </label>
        <label class="mf-field">
          <span class="mf-label">Key</span>
          <Input
            fullWidth
            value={key}
            placeholder="editor"
            onInput={(e) => setKey((e.target as HTMLInputElement).value)}
            required
          />
        </label>
      </div>

      <label class="mf-field">
        <span class="mf-label">Value</span>
        <textarea
          class="mf-textarea"
          value={value}
          rows={6}
          required
          placeholder="A concise fact, one sentence ideally."
          onInput={(e) => setValue((e.target as HTMLTextAreaElement).value)}
        />
      </label>

      <div class="mf-row">
        <label class="mf-field">
          <span class="mf-label">Kind</span>
          <select class="mf-select" value={kind} onChange={(e) => setKind((e.target as HTMLSelectElement).value)}>
            <option value="semantic">semantic — facts</option>
            <option value="episodic">episodic — events</option>
            <option value="procedural">procedural — how-tos</option>
          </select>
        </label>
        <label class="mf-field">
          <span class="mf-label">Importance ({Math.round(importance * 100)}%)</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={importance}
            onInput={(e) => setImportance(Number((e.target as HTMLInputElement).value))}
          />
        </label>
      </div>

      <div class="mf-row">
        <label class="mf-field">
          <span class="mf-label">Tags (comma-separated)</span>
          <Input
            fullWidth
            value={tags}
            placeholder="work, deadline"
            onInput={(e) => setTags((e.target as HTMLInputElement).value)}
          />
        </label>
        <label class="mf-field">
          <span class="mf-label">Expires in (days)</span>
          <Input
            fullWidth
            type="number"
            placeholder="never"
            value={expires === '' ? '' : String(expires)}
            onInput={(e) => {
              const v = (e.target as HTMLInputElement).value;
              setExpires(v === '' ? '' : Number(v));
            }}
          />
        </label>
      </div>

      <div class="mf-actions">
        <Button variant="ghost" type="button" onClick={onCancel}>Cancel</Button>
        <Button variant="primary" type="submit" disabled={!valid || busy}>
          <Icon name="check" size={14} /> Save memory
        </Button>
      </div>
    </form>
  );
}
