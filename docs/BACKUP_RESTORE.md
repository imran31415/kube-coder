# Backing up and restoring a workspace's home volume

Every kube-coder workspace has exactly one piece of state that cannot be
rebuilt from source: its home volume, the `ws-<user>-home` PVC mounted at
`/home/dev`. The chart already refuses to let *our* tooling destroy it —
`charts/workspace/templates/pvc.yaml` carries `helm.sh/resource-policy: keep`,
so a `helm uninstall` (a rollback of a failed install, a decommission by
mistake) leaves the volume behind.

That is protection against one failure mode out of five. It does nothing for:

- storage-backend failure or a corrupted volume,
- node loss where the volume can't be re-attached,
- a cluster rebuild or a migration to different infrastructure,
- a stray `kubectl delete pvc` (or `make delete-user`, which is explicit about
  destroying the data),
- the user deleting their own files.

For those you need a copy somewhere else. That is what these two scripts are.

```bash
make backup-user  USER=<name>                       # -> backups/<name>/…tar.gz
make restore-user USER=<name> ARCHIVE=<path|s3://…>
```

---

## What is in the archive

Everything under `/home/dev`, which per `CLAUDE.md` means:

| Path | What it is |
|------|-----------|
| `/home/dev/<project>/…` | every project checkout and git clone |
| `/home/dev/.credentials/.ssh` | **SSH private keys** |
| `/home/dev/.credentials/.config/{git,gh}` | git identity, GitHub CLI login |
| `/home/dev/.credentials/…` | **every persisted tool login** — `npm`, `docker`, `aws`, `gcloud`, `eas`, `wrangler`, `~/.git-credentials` |
| `/home/dev/.claude-memory/memory.db` | the persistent memory store |
| `/home/dev/.claude-tasks/` | Build/task state, transcripts, **and the workspace's API token** |
| `/home/dev/kube-coder` | the auto-provisioned clone (regenerable, but archived anyway) |

### What is NOT in the archive

The archive is the *volume*, not the workspace. It does not contain:

- the Helm release or the workspace's `values.yaml` (that lives in
  `deployments/<user>/`, `users-private/<user>/`, or the GitOps repo — back
  those up with git, not with this),
- Kubernetes Secrets: TLS certs, the oauth2 cookie secret, assistant API keys,
  the GitHub App private key,
- the namespace, ingress, or DNS records,
- `/home/ubuntu` (`$HOME`), `/tmp`, or installed apt packages — all ephemeral
  and rebuilt from the image on every pod start.

So a restore brings back the user's *work and logins*. Recreating the
workspace around it is `make deploy USER=<name>` as usual.

---

## The sharp edge: the archive is a credential

An archive of a home volume contains live SSH private keys and every OAuth
token the user has ever persisted. Whoever holds the file can act as that user
on GitHub, npm, AWS and your own cluster. Treat the `.tar.gz` exactly like the
private key inside it.

What the scripts do for you:

- the archive is written `0600`, inside a `0700` directory,
- `backups/` is in `.gitignore`, so a backup cannot be committed by accident,
- a `.sha256` sidecar is written next to it, also `0600`,
- `--encrypt-to <gpg-id>` pipes the archive through `gpg --encrypt` and
  removes the plaintext — `restore-user.sh` decrypts a `.gpg` archive
  transparently, so this costs you nothing at restore time.

What they cannot do for you: decide where the file is allowed to live. **The
default sink is deliberately local and boring** — `backups/<user>/` inside this
checkout — because a local file is the one destination that can't quietly
replicate your users' SSH keys somewhere you didn't intend. It is also, by
itself, a poor backup: it is on the operator's machine, unencrypted, and it
dies with that machine.

For anything durable, pick one and make it a habit:

```bash
# 1. Encrypted local archive, then move it wherever you like.
make backup-user USER=alice ENCRYPT_TO=ops@example.com

# 2. Straight to object storage, bytes never touching your laptop.
#    Use a bucket with encryption-at-rest and versioning enabled, and a
#    credential scoped to that one prefix.
kubectl -n ws-alice create secret generic aws-backup \
  --from-literal=AWS_ACCESS_KEY_ID=… \
  --from-literal=AWS_SECRET_ACCESS_KEY=… \
  --from-literal=AWS_DEFAULT_REGION=us-west-2
  # for R2/MinIO/etc. add --from-literal=AWS_ENDPOINT_URL=https://…
make backup-user USER=alice S3=s3://my-kube-coder-backups/alice S3_SECRET=aws-backup
```

With `S3=` the copy runs entirely inside the cluster (a helper pod pipes
`tar` into `aws s3 cp -`), which is both faster and the only sane option for a
large volume — see *Consistency and size* below.

---

## Consistency and size

**Hot by default.** `make backup-user USER=alice` copies a volume that is being
written. Ordinary files are fine. A SQLite database mid-write — notably
`.claude-memory/memory.db` — can be captured torn, and a git repo can be
captured mid-`checkout`. Add `QUIESCE=1` to scale the workspace to 0 for the
duration and back up afterwards; that gives a genuine point-in-time archive and
costs the user a pod restart. Scheduled backups should use it, or accept the
caveat knowingly.

**Size.** With a local sink the bytes travel through your `kubectl exec`
connection. That is fine for a few GB and unreliable for tens of GB — the same
lesson `scripts/migrate-user-namespace.sh` learned the hard way (a 50Gi copy
reliably died with a websocket 1006 close, which is why the migrate path
streams pod-to-pod instead). A truncated transfer here is *detected*, never
silently kept, but detection is not completion: for large volumes use `S3=`,
which never routes the data through your machine.

**Integrity.** The local path writes to `<archive>.part`, verifies the finished
file end-to-end (`gzip -t` plus a full `tar -t`, which only pass on a complete
stream with its EOF blocks), and only then moves it into place and writes the
checksum. If anything fails, the partial file is deleted. There is never a
truncated file sitting in `backups/` that looks like a backup. The S3 path
gates on `set -o pipefail` plus an explicit `BACKUP_SENT_OK` marker for the
same reason.

**Read-only.** The backup helper pod mounts the home volume `readOnly: true`,
at both the volume mount and the PVC reference. A backup must not be able to
damage the data it exists to protect.

---

## Restoring

```bash
make restore-user USER=alice ARCHIVE=backups/alice/ws-alice-home-20260801T120000Z.tar.gz
```

In order, the script:

1. **Verifies the archive before touching anything** — the `.sha256` sidecar if
   present, then `gzip -t` and `tar -t`. A corrupt archive fails while the old
   data is still intact.
2. Scales the workspace to 0. This is not optional: the pod's entrypoint writes
   to `/home/dev` on boot (credential symlinks, rc files, the kube-coder
   clone), so extracting into a live volume races it.
3. **Refuses a non-empty `/home/dev`.** The default assumption is that you are
   filling a fresh volume. `FORCE=1` overrides it.
4. Extracts, gated on the remote `tar`'s own exit status — it never reports
   success the extraction didn't report.
5. Scales the workspace back to its original replica count, on the success path
   *and* on every failure path.

`FORCE=1` is a **merge, not a replace**: files in the archive overwrite their
counterparts, and files on the volume that the archive doesn't contain are left
alone. For a true replace, delete the PVC, let `make deploy USER=<name>`
recreate it, and restore into the empty volume — the path the default is built
for.

Restoring from object storage is symmetric, and also runs in-cluster:

```bash
make restore-user USER=alice ARCHIVE=s3://my-kube-coder-backups/alice/ws-alice-home-…tar.gz S3_SECRET=aws-backup
```

A restored workspace carries the credentials that were in the archive. If the
backup predates a credential rotation, expect to re-login — and note that the
managed GitHub App token in `.credentials` is refreshed hourly anyway, so a
stale one is harmless.

---

## The drill

A backup nobody has restored is not a backup — it is a file you feel good
about. Run this end to end once when you set backups up, and again whenever the
workspace image or the entrypoint changes materially.

```bash
# 1. Back up a real workspace, consistently.
make backup-user USER=alice QUIESCE=1
ARCHIVE=$(ls -t backups/alice/*.tar.gz | head -1)

# 2. Restore into a THROWAWAY workspace, never over the real one.
make new-user USER=alice-drill
make deploy   USER=alice-drill
# FORCE=1 is expected here: a freshly deployed workspace's /home/dev is already
# seeded by the entrypoint, so it is not empty. restore-user.sh stops the pod
# itself and brings it back up when it is done.
make restore-user USER=alice-drill ARCHIVE=$ARCHIVE FORCE=1

# 3. Prove the things that actually matter came back.
kubectl -n ws-alice-drill exec deploy/ws-alice-drill -c ide -- sh -c '
  ls -la /home/dev
  ls -l  /home/dev/.credentials/.ssh/
  sqlite3 /home/dev/.claude-memory/memory.db "select count(*) from memories;"
  git -C /home/dev/<some-project> log --oneline -1
  git -C /home/dev/<some-project> status --short
'

# 4. Tear the drill workspace down (this destroys its volume — that's the point).
make delete-user USER=alice-drill
```

What you are checking in step 3, specifically: the SSH key is present *and
still mode 600*, the memory DB opens rather than reporting `database disk image
is malformed` (the thing a hot backup can get wrong), and a git checkout is
clean rather than mid-operation.

---

## Scheduling it

Deliberately not built in. There is no backup CronJob in the chart, no
operator, and no Velero dependency — the first useful version of a backup is a
command an operator can run and a schedule they choose. Wire it up wherever
your other cron lives:

```cron
# 03:00 daily, consistent, straight to object storage. Alert on non-zero exit —
# these scripts fail loudly rather than producing a partial archive, so a
# non-zero status is the signal you want to page on.
0 3 * * * cd /path/to/kube-coder && make backup-user USER=alice QUIESCE=1 \
            S3=s3://my-kube-coder-backups/alice S3_SECRET=aws-backup \
            >> /var/log/kube-coder-backup.log 2>&1
```

Retention is the bucket's job (an S3 lifecycle rule), not the script's.

---

## Testing

`scripts/backup_restore_test.sh` covers both scripts offline — it shims
`kubectl` on `PATH`, so there is no cluster and no network. It asserts the
properties that decide whether a backup is trustworthy rather than merely
present: a truncated stream leaves nothing behind, the archive is `0600` with a
verifying checksum, the helper mounts read-only and pins itself to the live
pod's node (a `ReadWriteOnce` volume can only attach to one *node*), `QUIESCE`
scales back up even when the backup fails, restore verifies before it touches
the volume and refuses a non-empty one, and a full backup→restore round trip
reproduces the tree byte-identically. It runs in CI alongside the other shell
suites.

```bash
bash scripts/backup_restore_test.sh
```
