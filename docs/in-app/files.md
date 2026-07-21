# Files

> **What the Files tab does.** Browses and uploads to `/home/dev`,
> your pod's persistent volume. Everything you save here survives pod
> restarts; everything outside it (notably `/tmp`) is gone on restart.

## Where data lives

| Path | Survives restart? | Notes |
| --- | --- | --- |
| `/home/dev/**` | ✅ | PVC, default 50Gi. All real work belongs here. |
| `/home/dev/.credentials/**` | ✅ | SSH keys, git config, gh tokens (symlinked to `~/.ssh`, `~/.gitconfig`, `~/.config/gh`). |
| `/home/dev/.claude-tasks/**` | ✅ | Task state — meta.json, output.log, transcripts. |
| `/home/dev/.claude-memory/memory.db` | ✅ | Persistent memory SQLite. |
| `/home/dev/.local/bin/*` | ✅ | Tools you install yourself. Already on `PATH` in every shell. |
| `~/.local/bin`, `~/.bashrc`, … | ❌ | `$HOME` is `/home/ubuntu` — ephemeral. Use `/home/dev/...` absolute paths instead. |
| `/tmp/**` | ❌ | Ephemeral, lost on restart. |
| `apt-get install ...` packages | ❌ | Re-installed on next restart unless persisted. |

## Browsing

Click into a directory to drill down; click a breadcrumb to jump back.
Hidden dotfiles (`.foo`) are hidden by default; toggle from the
header. Files are listed with size and mtime; click a file for a quick
preview (text only — binaries show a download link).

## Uploading

Drop files onto the upload zone or click **Upload**. Limits:

- **200 MiB per request.**
- Destination defaults to the current directory.
- File names sanitised: leading dots stripped, slashes rejected.

Uploads go straight to disk — no intermediate copy in `/tmp`.

## Making directories

**+ New folder** prompts for a name; it's created under the current
breadcrumb. Same sanitisation rules as upload.

## What's NOT here (yet)

- Inline editing — use VS Code (port 8080) or the terminal.
- Rename / move / delete — coming in a later phase; for now use the
  terminal.
- Symlink display — links are followed transparently. If you need to
  inspect, use `ls -la` from the terminal.

> :::scenario
> **Pattern: drop a credential, use it from Claude.**
> Drop a `.env` into your project directory. From a Claude task, ask
> *"read .env and set up the API client using those values"*. Claude
> reads it like any other file — no special handling needed, but
> remember it lives on the PVC and is readable by anything in the
> pod.
> :::

## Path traversal

The backend validates every path under `/home/dev` using `realpath`,
so a crafted `..` in the upload header can't write outside your home
directory. If you get a `403`, you tried to write somewhere unsafe —
not a bug.
