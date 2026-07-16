---
name: kc-ship-pr
description: Commit local changes and open a pull request against kube-coder from inside a workspace pod. Use when the user wants to push a branch or open/update a PR with the workspace GitHub App token.
user-invocable: true
allowed-tools: Bash, Read
argument-hint: "[PR title] (optional; inferred from the commit if omitted)"
---

# Ship a PR from a kube-coder workspace

The only GitHub credential here is the App installation token
(`/home/dev/.credentials/.github-token`, `ghs_…`). The token-refresh sidecar
installs a **self-refreshing global `credential.helper`** that reads that file
fresh on every call, so ordinary **`git push` works** — no Git Data API dance
needed. `gh api` also works (it reads `GITHUB_TOKEN` from `~/.github-env`).

There is no user-level `gh auth login` and no SSH key here, so do **not** suggest
`gh auth login` or a fork — the App token pushes to `origin` directly.

> **If `git push` is wedged** — `remote: Invalid username or token` or
> `could not read Username` — the cause is almost always a **stale baked
> `http.<host>.extraheader`** in `.git/config` (a point-in-time token from an
> old workaround; git sends it verbatim and it shadows the good helper).
> Fix it, then push normally:
> ```bash
> git config --unset-all http.https://github.com/.extraheader
> ```
> Only if push is *still* broken after that, fall back to the Git Data API
> (§3b). See your `github-auth` memory for the full background.

## Steps

Given the working tree already has the changes staged/committed on a feature
branch (create one first if the user is on `main`):

### 1. Commit locally

```bash
cd /home/dev/kube-coder   # or your worktree
git add -A            # or specific paths
git commit -m "<type>(<scope>): <summary>

<body>

Co-Authored-By: Claude <noreply@anthropic.com>"
```

### 2. Re-sync onto current origin/main (avoid a stale base)

```bash
git fetch origin main && git rebase origin/main
```

If the credential helper is somehow unavailable, the repo is public so an
unauthenticated fetch also works:
`git -c credential.helper= -c http.https://github.com/.extraheader= fetch https://github.com/imran31415/kube-coder.git main`

### 3. Push the branch

```bash
git push -u origin <branch>
```

### 3b. Fallback — push via the Git Data API (only if `git push` stays wedged)

Run this Python (token from env or the credential file). Set `BRANCH` and list
the changed files in `FILES`:

```bash
source /home/dev/.credentials/.github-env   # exports GITHUB_TOKEN
python3 - <<'PY'
import os, json, base64, urllib.request, subprocess
TOKEN = os.environ["GITHUB_TOKEN"]; REPO = "imran31415/kube-coder"
API = f"https://api.github.com/repos/{REPO}"
BRANCH = "REPLACE-branch-name"
FILES = subprocess.check_output(
    ["git", "diff", "--name-only", "main", "HEAD"]).decode().split()

def api(method, url, body=None):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=d, method=method)
    r.add_header("Authorization", f"token {TOKEN}")
    r.add_header("Accept", "application/vnd.github+json")
    if d: r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r) as x: return json.load(x)

base = api("GET", f"{API}/git/refs/heads/main")["object"]["sha"]
tree = api("GET", f"{API}/git/commits/{base}")["tree"]["sha"]
entries = []
for f in FILES:
    blob = api("POST", f"{API}/git/blobs",
               {"content": base64.b64encode(open(f, "rb").read()).decode(),
                "encoding": "base64"})
    entries.append({"path": f, "mode": "100644", "type": "blob", "sha": blob["sha"]})
t = api("POST", f"{API}/git/trees", {"base_tree": tree, "tree": entries})
msg = subprocess.check_output(["git", "log", "-1", "--format=%B"]).decode().strip()
commit = api("POST", f"{API}/git/commits",
             {"message": msg, "tree": t["sha"], "parents": [base]})
# New branch: POST a ref. To UPDATE an existing branch (add a commit), use
# PATCH {API}/git/refs/heads/{BRANCH} with {"sha": commit["sha"]} instead.
api("POST", f"{API}/git/refs",
    {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
print("pushed", commit["sha"][:8], "->", BRANCH)
PY
```

Notes:
- **New branch** → `POST /git/refs`. **Add a commit to an existing PR branch**
  → read the branch's current head first, base the tree on it, then
  `PATCH /git/refs/heads/{BRANCH}` with the new commit sha.
- The commit author is the App (bot), not the user — expected and fine for a PR.
- Only the files in `FILES` change; everything else is inherited from `base_tree`.

### 4. Open the PR

```bash
source /home/dev/.credentials/.github-env
BODY=$(cat <<'EOF'
## What & why
...

## Testing
...

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
gh api repos/imran31415/kube-coder/pulls \
  -f title="$ARGUMENTS" -f head="REPLACE-branch-name" -f base="main" -f body="$BODY" \
  --jq '.html_url'
```

If `gh` fails, the same works with `curl -X POST -H "Authorization: token $GITHUB_TOKEN"`.

### 5. Link the issue (if the PR resolves one)

The PR body/title referencing `(#N)` cross-links but does **not** auto-close.
To close on merge, add `Fixes #N` to the PR body, or close the issue after merge:

```bash
gh api repos/imran31415/kube-coder/issues/N/comments -f body="Resolved by #<pr>."
gh api -X PATCH repos/imran31415/kube-coder/issues/N -f state=closed -f state_reason=completed
```

## Before shipping

Run **kc-preflight** first so CI is green on the first push. Never push a branch
you haven't at least `bash -n`/typecheck/test-run locally — CI round-trips are slow.

## See also

- Your `github-auth` memory has the full background on the App-token auth setup
  and the stale-extraheader footgun.
- To add a commit to an already-open PR, just `git push` again; if you're using
  the §3b fallback, repeat it in PATCH mode onto the branch head.
