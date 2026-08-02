# devcontainer.json

If your repo already carries a `.devcontainer/devcontainer.json` — because it
came from Codespaces, Coder, Ona or DevPod — kube-coder reads it. Forwarded
ports show up on the Apps page, VS Code extensions and settings reach
code-server, `containerEnv`/`remoteEnv` reach the agents you dispatch, and the
lifecycle commands are shown to you so you can run them with one click.

Nothing is executed until you click Apply and read what will run.

## What kube-coder does and does not do

kube-coder **interprets** `devcontainer.json` inside the workspace pod you
already have. It does **not** build a container from it, and it never will
from here. Three constraints make that structural rather than a missing
feature:

| Constraint | Where it comes from | What it rules out |
|---|---|---|
| The pod runs as UID 1000 with `allowPrivilegeEscalation: false` | `templates/deployment.yaml` | `sudo` at runtime. The spec requires **features** to install as root at image-build time, so `features` can never work here. |
| `build.mode` defaults to `kaniko` | `values.yaml` | No Docker daemon, so `image` and `build` have nothing to build with. |
| One pod is one persistent home | architecture | A nested dev container would mean two filesystems, and code-server, tmux and your agents would each have to pick one. |

So the feature spends as much effort on **reporting what cannot be applied** as
on applying the rest. Every unsupported property comes back with a reason
grounded in the constraint above and a remedy you can act on. Silently ignoring
`features` would leave you with a workspace that *looks* configured and isn't —
which is the worst possible outcome for something whose whole purpose is
migration.

### Applied

| Property | How |
|---|---|
| `name` | Names a freshly-discovered project (never overwrites a name you chose) |
| `workspaceFolder` | Mapped onto the project directory; anything that escapes it is refused |
| `forwardPorts`, `portsAttributes` | Pinned on the **Apps** page with their labels |
| `customizations.vscode.extensions` | Installed into code-server |
| `customizations.vscode.settings` | Merged into code-server's `settings.json` |
| `containerEnv`, `remoteEnv` | Added to the environment of new builds in that directory |
| `onCreateCommand`, `updateContentCommand`, `postCreateCommand`, `postStartCommand` | Run on request, per hook, after you confirm |

### Reported, not applied

`image` · `build` · `features` · `dockerComposeFile` · `service` · `mounts` ·
`runArgs` · `privileged` · `capAdd` · `securityOpt` · `hostRequirements` ·
`initializeCommand` · `postAttachCommand` · `remoteUser` · `appPort` and a few
others. Each shows in the Dev container card with its reason and remedy.

The common case is `features`. The fix is usually mechanical: anything that
installs into `$HOME` — nvm, pyenv, rustup, `pip --user`, `npm -g` with a
user prefix — moves to `postCreateCommand` and works. Anything that needs to
write to `/usr` has to go into the workspace image instead.

## Using it

The **Dev container** card appears in the project brief on `/cto` for any
workdir that has a `devcontainer.json`. It shows what the file declares, what
has already run, and what cannot be applied.

Click **Apply…** and you get a dialog with:

- everything that will be applied immediately (ports, extensions, settings, env);
- the **exact text** of every lifecycle command, verbatim, with a checkbox each;
- a warning on any command that needs root — those will fail, and it is better
  to know before you wait fifteen minutes for it;
- the blocking unsupported properties repeated at the top, so you never approve
  a `postCreate` believing your `features` already installed.

Every hook starts **unticked**. Applying ports, settings, extensions and env
without running anything is one click.

### Re-running after a pod restart

`postStartCommand` is per-boot by definition. Tick **Re-run postStart after
every pod restart** in the dialog and kube-coder will run it once on each boot
— but only while `devcontainer.json` is byte-for-byte the file you consented
to. Change the file and the automatic run stops until you confirm the new
commands.

`postCreateCommand` is **never** run automatically at boot. A ten-minute
`npm ci` would delay every pod start, and restarting to escape a broken state
would just re-trigger whatever broke it.

## Safety

`devcontainer.json` comes out of a cloned repo, so it is treated as hostile
input throughout.

- **Nothing executes on discovery.** Browsing projects, opening the brief, or
  calling any GET only ever parses the file.
- **Consent is pinned to the file.** The apply request must echo back the hash
  of the exact file you were shown. If `devcontainer.json` changed in between —
  a `git pull`, or a `postStart` rewriting its own config — the server refuses
  with `409` instead of running text you never read.
- **Commands run in their own process group** with a wall-clock timeout
  (`devcontainer.commandTimeoutSeconds`, default 900s). On expiry the whole
  group is killed, so a runaway `npm install` cannot outlive it.
- **A non-zero exit stops the chain**, per the spec.
- **Three denylists** reject repo-supplied values that would take over the
  workspace regardless of whether any command runs:
  - *environment* — `ANTHROPIC_*`, `OPENAI_*`, `GH_*`, `GITHUB_*`, `AWS_*`,
    `PATH`, `LD_PRELOAD`, `BASH_ENV`, `GIT_SSH_COMMAND`, `NODE_OPTIONS` and
    friends. `ANTHROPIC_BASE_URL` is the sharpest: a repo setting it would
    silently proxy every agent prompt through an endpoint its author controls.
    Your own provider keys always win over anything the file declares.
  - *editor settings* — `terminal.integrated.profiles.*`,
    `security.workspace.trust.*`, `remote.*`, `http.proxy*`, and anything
    ending in `executablePath` / `interpreterPath` / `serverPath` /
    `shellArgs`. These run a command on every editor open, silently, forever.
  - *extension ids* — must be `publisher.name`. A `.vsix` path or a leading `-`
    is refused, because `--install-extension` also accepts a filesystem path
    and that would install arbitrary editor code straight out of the repo.
- **`${localEnv:X}` is never resolved.** It would copy a workspace secret into
  a value the repo chose the name of, which a later command could read back and
  send anywhere. It is reported as an unresolved caveat instead.
- **Nothing is clobbered.** A port you pinned by hand keeps your name; a
  setting you edited in code-server survives a repo that disagrees.
- **Read-only workspaces** (`READONLY_MODE=true`) reject both POST routes with
  `403`, and the boot pass does not run.
- **The CTO can look but not apply.** `list_devcontainers` and
  `get_devcontainer` are exposed as MCP tools so an agent can explain your
  environment; there is deliberately no apply tool. Approving repo-supplied
  commands is a human's job — an agent's judgement is exactly what a malicious
  `devcontainer.json` is trying to capture.

## Two homes, one gotcha

Lifecycle commands run with `HOME=/home/dev`, so `nvm`, `rustup` and
`pip --user` install onto the persistent volume and survive a restart.

Your **interactive** shells — the terminal, code-server, SSH — run with
`HOME=/home/ubuntu`, which is rebuilt from skel on every boot. So a tool
installed by `postCreateCommand` persists but may not be on your `PATH` in a
fresh terminal. If a command reports success and the binary seems missing,
that's why: look in `/home/dev/.nvm`, `/home/dev/.local/bin`, and add it to
your shell rc.

## JSONC

`devcontainer.json` is JSON **with comments and trailing commas** — both legal,
both present in almost every real file. kube-coder parses them, and reports
syntax errors with a line and column that point at the original file.

## Configuration

```yaml
devcontainer:
  enabled: true                # DEVCONTAINER_ENABLED
  autoApplyOnBoot: true        # DEVCONTAINER_AUTO_APPLY
  commandTimeoutSeconds: 900   # DEVCONTAINER_TIMEOUT
```

`enabled: false` hides the Dev container card and 404s `/api/devcontainer*`.
`autoApplyOnBoot: false` keeps the read-and-report surface but makes every
command execution require an explicit click, always.

## API

| Verb | Path | Notes |
|---|---|---|
| `GET` | `/api/devcontainer?workdir=<abs>` | Parsed config, what was applied, per-hook status. A directory with no file answers **200 with `found: false`**, not 404. |
| `GET` | `/api/devcontainer/scan` | Every workspace directory that has one, with counts. |
| `POST` | `/api/devcontainer/apply` | `{workdir, hooks[], config_hash, auto_apply}`. `hooks: []` applies ports/settings/extensions/env and runs nothing. `config_hash` is **required** whenever `hooks` is non-empty. Answers `202`. |
| `POST` | `/api/devcontainer/reset` | `{workdir, unpin_ports}`. Forgets that we applied here. Settings and extensions are not rolled back. |

Errors: `400 hash_required`, `409 hash_mismatch`, `409 busy`, `404 not_found`,
`422 invalid`.
