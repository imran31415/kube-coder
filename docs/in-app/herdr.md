# herdr in the terminal

> **What this is.** [herdr](https://herdr.dev/docs/) is a terminal
> multiplexer built for coding agents. It runs several agents side by side
> in one terminal, and its sidebar shows which ones are working, blocked or
> done. It is installed in every workspace (pinned to the version in the
> image) and runs in the workspace terminal. The web dashboard, the mobile app
> and the desktop app all open that same terminal, so herdr works the same in
> each.

## Quick path

1. Open a terminal. On the web dashboard use **New terminal ↗** in the top bar
   (on a phone browser, **More → New terminal**). In the mobile and desktop
   apps, open a terminal from the Builds list.
2. Run `herdr`. The first time, a short welcome screen appears. Press Enter,
   then Esc to skip the optional agent integrations.
3. Split a pane with **`Ctrl+B Ctrl+B v`** (or right-click a pane and choose
   a split), then start an agent in it, for example `claude`.
4. Detach with **`Ctrl+B Ctrl+B q`**. Everything keeps running.
5. Run `herdr` again, in this terminal or a new one, to reattach.

## Why `Ctrl+B` twice

The dashboard terminal is a tmux session, and tmux uses `Ctrl+B` as its own
prefix key. herdr uses `Ctrl+B` too. The first press goes to tmux, and the
second is passed through to herdr, so `Ctrl+B Ctrl+B` acts as one herdr
prefix. Every key in herdr's documentation works this way:

| herdr action | herdr docs | In the dashboard terminal |
| --- | --- | --- |
| Detach, leave everything running | `ctrl+b q` | `Ctrl+B Ctrl+B q` |
| Split right / down | `ctrl+b v` / `ctrl+b -` | `Ctrl+B Ctrl+B v` / `Ctrl+B Ctrl+B -` |
| New tab | `ctrl+b c` | `Ctrl+B Ctrl+B c` |
| Move between panes | `ctrl+b h/j/k/l` | `Ctrl+B Ctrl+B h/j/k/l` |
| All key bindings | `ctrl+b ?` | `Ctrl+B Ctrl+B ?` |

If you press `Ctrl+B` only once, the next key goes to tmux instead. Most keys
do nothing, but `Ctrl+B c` opens a new tmux window and `Ctrl+B d` disconnects
the terminal. If that happens, reload the terminal tab. herdr is still running.

In a shell that is not inside tmux, such as an SSH session, a single `Ctrl+B`
works as herdr's docs describe. Run `echo $TMUX` to check: it prints nothing
when you are not inside tmux.

**One key instead of two.** You can add a second herdr prefix that tmux does
not use. Add it to `/home/dev/.config/herdr/config.toml`:

```toml
[keys]
prefix = ["ctrl+b", "ctrl+s"]
```

Then run `herdr server reload-config`. `ctrl+s` and `ctrl+]` both reach
herdr through the dashboard terminal. Whatever key you choose stops reaching
the programs inside herdr's panes. `ctrl+s`, for example, is shell history
search.

## On a phone

herdr is mouse-first, so you can tap panes, tabs and the sidebar instead of
pressing keys. A phone keyboard has no `Ctrl` key, so use the menus:

- **Detach without keys.** Tap **menu → detach** in the sidebar. On a
  narrow terminal, herdr switches to its phone layout, and the same menu is
  under **switch** at the top right.
- **Split or add a tab.** Right-click (on a computer) or use **switch → new
  tab**. Splits are easiest with a keyboard, so set up a layout from a
  computer and it is waiting on the phone.
- **Just leave.** Closing the terminal screen also works. The herdr client
  stays attached inside the terminal's tmux session, so reopening that
  terminal shows herdr again.

In the mobile app, the terminal is laid out like a desktop page and scaled to
the screen, the same way Claude Code's interface appears there. In a terminal
that really is phone-width, herdr switches to its compact layout.

## What survives a restart

| What | Detach / close the tab | Pod restart |
| --- | --- | --- |
| Pane processes (shells, agents, dev servers) | Keep running | Stopped, like every process in the pod |
| Workspaces, tabs, pane layout and each pane's folder | Kept | Restored the next time you run `herdr` |
| `config.toml` (keys, theme, settings) | Kept | Kept |
| Agent conversations | Still running | Resumed if you installed that agent's herdr integration, otherwise a shell in the same folder |

herdr keeps its files in three places, all on the persistent volume:

- `/home/dev/.config/herdr`: `config.toml`, the saved session
  (`session.json`, `session-snapshots/`), logs, and the server's sockets.
- `/home/dev/.local/state/herdr`: agent-detection state.
- `/home/dev/.herdr`: worktrees created from herdr's worktree menu.

The terminal's home folder is `/home/ubuntu`, which is rebuilt on every
restart. At boot the workspace links `/home/ubuntu/.config/herdr`,
`/home/ubuntu/.local/state/herdr` and `/home/ubuntu/.herdr` to the folders
above, so herdr's default paths land on the persistent volume.

To end the session and every pane in it, run `herdr server stop`.

## Agent integrations are opt-in

On first run, herdr offers to install integrations for the agents it finds.
An integration makes the sidebar status more accurate and lets herdr resume
an agent's conversation after a restart. Installing one edits that agent's
own config. For Claude Code, it adds hooks to its settings. The workspace
does not install any by default. To add one yourself:

```bash
herdr integration status          # what is installed
herdr integration install claude  # or codex, opencode, …
```

## Limits

- **The version is set by the image.** `herdr update` cannot replace the
  root-owned binary in `/usr/local/bin`. Upgrades arrive with workspace
  image updates.
- **herdr is for you, not for Builds.** Builds, sub-agents and the
  Hypervisor still run in tmux. Nothing starts herdr at boot.
- **SSH is not covered.** The optional SSH sidecar is a separate, small
  container with its own home folder. A herdr started there does not share
  this session, and its state is not kept.
