import { useEffect, useState } from 'preact/hooks';
import { listFiles, makeDirectory, uploadFile, type FileEntry } from '../../api/files';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';
import { MutatorOnly } from '../../components/MutatorOnly';
import { PromptDialog } from '../../components/ConfirmDialog';
import './files.css';

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function FilesRoute() {
  const [path, setPath] = useState('');
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mkdirOpen, setMkdirOpen] = useState(false);

  async function refresh(p: string) {
    setBusy(true);
    setError(null);
    try {
      const r = await listFiles(p);
      setEntries(r.entries);
      setPath(r.path ?? p);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setLoadedOnce(true);
    }
  }

  useEffect(() => {
    void refresh('');
  }, []);

  async function go(name: string, kind: FileEntry['kind']) {
    if (kind !== 'dir') return;
    const next = path ? `${path}/${name}` : name;
    await refresh(next);
  }

  async function goUp() {
    if (!path) return;
    const parts = path.split('/').filter(Boolean);
    parts.pop();
    await refresh(parts.join('/'));
  }

  async function onUpload(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      await uploadFile(file, path);
      pushToast(`Uploaded ${file.name}`, { kind: 'success' });
      await refresh(path);
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Upload failed', { kind: 'danger' });
    } finally {
      setBusy(false);
      input.value = '';
    }
  }

  async function onMkdirConfirm(name: string) {
    setMkdirOpen(false);
    if (!name) return;
    setBusy(true);
    try {
      await makeDirectory(path ? `${path}/${name}` : name);
      pushToast(`Created ${name}/`, { kind: 'success' });
      await refresh(path);
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'mkdir failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  const dirs = entries.filter((e) => e.kind === 'dir').sort((a, b) => a.name.localeCompare(b.name));
  const files = entries.filter((e) => e.kind === 'file').sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div class="route route-files">
      <header class="route-header route-header-with-action">
        <div>
          <h1 class="route-title">Files</h1>
          <p class="route-subtitle muted">Browse /home/dev. Click a directory to enter, the breadcrumb to navigate back.</p>
        </div>
        <MutatorOnly>
          <div class="files-actions">
            <Button variant="ghost" onClick={() => setMkdirOpen(true)} disabled={busy}>
              <Icon name="plus" size={14} /> Folder
            </Button>
            <label class="files-upload">
              <input type="file" onChange={onUpload} hidden />
              <span class="btn btn-secondary btn-md">
                <Icon name="plus" size={14} /> Upload
              </span>
            </label>
          </div>
        </MutatorOnly>
      </header>

      <nav class="files-crumbs mono">
        <button class="files-crumb" onClick={() => refresh('')}>/home/dev</button>
        {path.split('/').filter(Boolean).map((p, i, arr) => {
          const upTo = arr.slice(0, i + 1).join('/');
          return (
            <span key={upTo}>
              <span class="files-crumb-sep">/</span>
              <button class="files-crumb" onClick={() => refresh(upTo)}>{p}</button>
            </span>
          );
        })}
      </nav>

      {error && <div class="trig-error" role="alert">{error}</div>}

      <table class="files-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Size</th>
            <th>Modified</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {path && (
            <tr class="files-row files-row-up">
              <td colSpan={4}>
                <button class="files-row-btn" onClick={goUp}>
                  <Icon name="chevron-right" size={14} class="files-up-icon" />
                  <span>..</span>
                </button>
              </td>
            </tr>
          )}
          {dirs.map((e) => (
            <tr class="files-row" key={e.name}>
              <td>
                <button class="files-row-btn" onClick={() => go(e.name, e.kind)}>
                  <Icon name="files" size={14} />
                  <span>{e.name}/</span>
                </button>
              </td>
              <td class="muted mono">—</td>
              <td class="muted mono">{new Date(e.mtime * 1000).toLocaleDateString()}</td>
              <td><OpenInVscode path={path ? `${path}/${e.name}` : e.name} kind="dir" /></td>
            </tr>
          ))}
          {files.map((e) => (
            <tr class="files-row" key={e.name}>
              <td>
                <span class="files-row-btn files-row-btn-static">
                  <Icon name="inbox" size={14} />
                  <span>{e.name}</span>
                </span>
              </td>
              <td class="muted mono">{fmtSize(e.size)}</td>
              <td class="muted mono">{new Date(e.mtime * 1000).toLocaleDateString()}</td>
              <td><OpenInVscode path={path ? `${path}/${e.name}` : e.name} kind="file" /></td>
            </tr>
          ))}
          {busy && (!loadedOnce || entries.length === 0) && (
            <>
              {Array.from({ length: 5 }, (_, i) => (
                <tr class="files-row files-row-skeleton" key={`skeleton-${i}`} aria-hidden="true">
                  <td><span class="files-skeleton-cell files-skeleton-name" /></td>
                  <td><span class="files-skeleton-cell files-skeleton-size" /></td>
                  <td><span class="files-skeleton-cell files-skeleton-date" /></td>
                  <td />
                </tr>
              ))}
            </>
          )}
        </tbody>
      </table>

      <PromptDialog
        open={mkdirOpen}
        title="New folder"
        body={path ? `Create under ${path}` : 'Create under /home/dev'}
        placeholder="folder-name"
        confirmLabel="Create"
        onConfirm={onMkdirConfirm}
        onCancel={() => setMkdirOpen(false)}
      />
    </div>
  );
}

/** Per-row "Open in VS Code" affordance. Files open at their parent
 *  folder (code-server's URL-payload `openFile` syntax varies between
 *  versions, but `?folder=<parent>` is universal and the file tree
 *  highlights the requested file via the path navigation). Directories
 *  open as the workspace folder. Hidden in read-only public demo via
 *  the MutatorOnly wrapper at the call sites — code-server is unauth'd
 *  there too so we don't surface a link that opens an editor session. */
function OpenInVscode({ path, kind }: { path: string; kind: 'dir' | 'file' }) {
  const absHome = '/home/dev';
  const abs = `${absHome}/${path}`;
  const folder = kind === 'dir'
    ? abs
    : abs.slice(0, abs.lastIndexOf('/')) || absHome;
  const href = `/oauth/vscode/?folder=${encodeURIComponent(folder)}`;
  return (
    <a
      class="files-row-vscode"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={kind === 'dir' ? 'Open folder in VS Code' : 'Open parent folder in VS Code'}
      aria-label={kind === 'dir' ? `Open ${path} in VS Code` : `Open parent of ${path} in VS Code`}
      onClick={(e) => e.stopPropagation()}
    >
      <Icon name="link" size={12} />
    </a>
  );
}
