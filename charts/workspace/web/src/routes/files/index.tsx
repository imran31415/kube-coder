import { useEffect, useState } from 'preact/hooks';
import { listFiles, makeDirectory, uploadFile, type FileEntry } from '../../api/files';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';
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
  const [error, setError] = useState<string | null>(null);

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

  async function onMkdir() {
    const name = prompt('New folder name:');
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
        <div class="files-actions">
          <Button variant="ghost" onClick={onMkdir} disabled={busy}>
            <Icon name="plus" size={14} /> Folder
          </Button>
          <label class="files-upload">
            <input type="file" onChange={onUpload} hidden />
            <span class="btn btn-secondary btn-md">
              <Icon name="plus" size={14} /> Upload
            </span>
          </label>
        </div>
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
          </tr>
        </thead>
        <tbody>
          {path && (
            <tr class="files-row files-row-up">
              <td colSpan={3}>
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
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
