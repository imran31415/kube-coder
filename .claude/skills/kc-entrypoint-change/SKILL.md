---
name: kc-entrypoint-change
description: Safely edit the workspace pod's boot scripts (start.sh, the entrypoint/ssh-server configmaps) in kube-coder. Use when changing anything that runs at pod start — credential wiring, service launch, tmux/ttyd config, symlinks — where the two-home layout and Helm templating have non-obvious footguns.
user-invocable: true
allowed-tools: Bash, Read, Edit, Grep
argument-hint: "[what you want the boot script to do]"
---

# Editing kube-coder boot scripts safely

Changes to the pod entrypoint affect **every workspace on next restart**, and a
mistake can break boot for everyone. This skill captures the gotchas and the
validation loop.

## Where boot logic lives

- **`charts/workspace/start.sh`** — the main IDE container entrypoint. Shipped
  into a ConfigMap via `{{ tpl (.Files.Get "start.sh") . | indent 4 }}` in
  `templates/workspace-entrypoint-configmap.yaml`, and run as
  `command: ["/bin/bash","-l","/workspace-entrypoint/start.sh"]`.
- **`charts/workspace/templates/ssh-server-configmap.yaml`** — the SSH sidecar
  entrypoint (`data.entrypoint.sh`), gated by `{{- if .Values.ssh.enabled }}`.
- **`charts/workspace/templates/terminal-entry-configmap.yaml`** — the per-ttyd
  connection script (tmux attach / DEC-mode resets).

Prefer editing `start.sh` as a real file; the configmaps are YAML block scalars.

## Footgun 1 — the two-home layout (MOST IMPORTANT)

There are **two** home directories:

- **`/home/dev`** — the persistent PVC. Survives restarts.
- **`/home/ubuntu`** — **ephemeral**. Wiped on every pod restart.

`start.sh` and the interactive terminals (ttyd, code-server, SSH) run with
**`HOME=/home/ubuntu`**. That's why `/home/ubuntu/.ssh` is a symlink onto the
PVC. **Anything a tool writes under `$HOME` is lost on restart unless it's
redirected onto `/home/dev`.**

**But `$HOME` is not `/home/ubuntu` everywhere (issue #793).** `server.py`
forces `HOME=/home/dev` on the CLIs it spawns — agent tasks, hooks, the
hypervisor — so they find their config on the PVC, and tmux copies the
environment of whoever started its server into every session created later.
So a `bash -l` can read **either** `/home/dev/.profile` or
`/home/ubuntu/.profile`. Consequences when you touch boot wiring:

- Seed env blocks into **both** homes — `bootstrap_rc` loops over
  `/home/ubuntu /home/dev` for exactly this reason. One home only means the
  block silently does nothing for half the shells in the pod.
- `/home/ubuntu` is rebuilt from skel each boot (so it always gets the current
  block text), while the PVC's `/home/dev` copy is written once and kept.
  **Changing a block's body needs a new marker string**, or the stale text
  lives on in `/home/dev`'s rc files.
- Since #795, `persist_cred` links `.ssh`, `.gitconfig` and the per-tool
  credential paths in **both homes** at the one store under
  `/home/dev/.credentials/`, so `~/.ssh` resolves to the same keys regardless
  of context (anything it displaces is kept at a `.pre795.<epoch>` backup, not
  deleted). Still never write `~` or `$HOME` in docs, examples or scripts
  here; spell the home out. A `rm -rf "$HOME/..."` meant to clear an ephemeral
  copy will delete PVC data when `$HOME` is `/home/dev`.

The established pattern to persist something is: keep the real data under
`/home/dev/...` (secrets go in `/home/dev/.credentials/`), and symlink the
ephemeral `/home/ubuntu` path to it. See the `persist_cred` helper and the
`for HOME_DIR in /home/ubuntu /home/dev` loop in `start.sh`, and the mirrored
block in `ssh-server-configmap.yaml` (the sidecar has its **own** ephemeral
`/home/ubuntu` and is always `HOME=/home/ubuntu` under sshd, so it needs the
symlinks independently).

When persisting a new credential/config: migrate any pre-existing login onto the
PVC once with `cp -an` (archive + **no-clobber**, so a real PVC login is never
overwritten by a stale ephemeral copy), then replace with a symlink. Make it
idempotent — guard with `[ -L "$link" ]` so re-runs don't nest symlinks.

## Footgun 2 — `tpl` runs Go templating over `start.sh`

Because the configmap uses `{{ tpl (.Files.Get "start.sh") . }}`, any literal
`{{ ... }}` in `start.sh` is interpreted by Helm. Plain shell (`$VAR`,
`$(cmd)`, backticks) is fine, but **never introduce `{{`/`}}`** unless you mean
a template action. After editing, always render (below) to catch this.

## Footgun 3 — configmap YAML indentation

The sidecar script is a `data.entrypoint.sh: |` block scalar at 4-space indent.
Keep added lines at the same indent; a stray dedent silently truncates the
script. `helm template` will surface most of these.

## Footgun 4 — two ttyd launch sites

`start.sh` launches ttyd once at boot **and** relaunches it in the watchdog
loop. If you change ttyd flags, change **both** occurrences (grep for `ttyd`).

## Footgun 5 — overwrite-on-boot files

`CLAUDE.md`/`claude-md.txt`, `~/.tmux.conf`, and the OpenCode config are
**rewritten every boot** so chart edits propagate. Don't add logic that
"preserves existing" for these — it defeats the design. User notes belong in the
persistent-memory subsystem, not these files.

## Validation loop (always run before pushing)

```bash
# 1. Shell syntax — a broken start.sh breaks boot for every workspace.
bash -n charts/workspace/start.sh

# 2. Render through Helm (catches tpl + YAML indent errors, proves it ships).
export PATH="/home/dev/.local/bin:$PATH"   # absolute: $HOME varies here
helm template test-ws charts/workspace/ -f charts/workspace/tests/test-values.yaml >/tmp/r.yaml
grep -n "<a string from your change>" /tmp/r.yaml    # confirm it rendered
# sidecar is gated — render it with ssh on:
helm template test-ws charts/workspace/ -f charts/workspace/tests/test-values.yaml \
  --set ssh.enabled=true >/tmp/r-ssh.yaml

# 3. If you can, unit-execute the logic in a sandbox against fake /home dirs
#    (extract the function, rebind paths to /tmp, stub chown) to prove
#    fresh / migrate / no-clobber / idempotent behavior.
```

## Add a regression test

Boot-script behavior is covered by **helm-unittest** files in
`charts/workspace/tests/*_test.yaml`. Add `matchRegex` assertions on
`data["start.sh"]` (and `data["entrypoint.sh"]` for the sidecar, with
`set: {ssh.enabled: true}`) so your change can't silently regress. Model it on
`credential_persist_test.yaml` or `browser_gate_test.yaml`. Then:

```bash
helm unittest charts/workspace/
```

Finish by running **kc-preflight**, then **kc-ship-pr**.
