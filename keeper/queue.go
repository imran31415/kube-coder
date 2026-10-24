// The queue of requests that arrived while the workspace was away (#728 step
// 4b).
//
// Why a queue exists at all: GitHub does not retry a failed webhook delivery,
// and a cron fire that lands on a dead nginx is simply lost. Before step 4b a
// sleeping workspace turned both into a raw 503 — the task never ran and
// nothing said so. So the keeper accepts the request, writes it down, wakes the
// workspace and replays it byte for byte once the workspace is back. server.py
// still checks the signature itself; the keeper only keeps the bytes.
//
// Two properties the rest of the design rests on, both of them structural
// rather than argued:
//
//   - Durable before acknowledged. Put fsyncs the entry before the caller is
//     told 202 and before the wake is attempted. Only that order closes the
//     window the PersistentVolume exists for: a keeper that dies a millisecond
//     after answering has already written the bytes down.
//
//   - One owner per entry. Claim moves the file into inflight/ by rename, which
//     is atomic, so of two goroutines racing for the same entry exactly one
//     gets it. A drain landing in the middle of a live forward therefore cannot
//     double-deliver.
//
// Entries hold whole requests, headers included, and a cron entry's headers
// carry its live fire token. That is unavoidable if the replay is to be
// byte-identical — the token IS the request. The directory is 0700 and the
// files 0600 on a volume no other pod mounts, and nothing here ever logs a
// header value.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Delivery semantics, per kind, as a named decision rather than an accident of
// control flow.
//
// The difference is forced by what the workspace does on the other end. A
// replayed webhook meets WebhookManager.REPLAY_CACHE and the sender's own
// delivery id, and a duplicated agent run is recoverable; so an ambiguous
// webhook is retried. handle_cron_fire has no dedup whatsoever
// (handlers/triggers.py:428) — two replays are two agent runs — while a missed
// tick simply fires again on schedule, and concurrencyPolicy: Forbid makes that
// cheap. So an ambiguous cron is dropped, loudly.
const (
	atLeastOnce = "at-least-once" // webhook
	atMostOnce  = "at-most-once"  // cron, page-watch
)

// semanticsFor reports how an entry of this kind is resolved when its delivery
// cannot be established.
func semanticsFor(kind string) string {
	if kind == "webhook" {
		return atLeastOnce
	}
	return atMostOnce
}

// Queue TTLs. A queued request is replayed after the workspace wakes, which
// takes the better part of a minute, and some signatures do not survive that.
//
// Slack and Stripe both sign a timestamp and both enforce a ±300 s window
// (TIMESTAMP_TOLERANCE, server.py:6934, enforced at :7180 and :7217). A replay
// that lands outside it fails as `404 Not found or unauthorized` with ledger
// reason='bad_signature' — which an operator reads as "my secret is wrong".
// Expiring the entry with a log line that says what actually happened is the
// honest answer, so those two get a TTL inside the window. GitHub and generic
// signatures cover the body alone and keep indefinitely, so they get the
// configured default.
const signedWindowTTL = 240 * time.Second

// ttlFor is how long an entry may wait before it is expired unreplayed.
func ttlFor(e *Entry, def time.Duration) time.Duration {
	switch e.Provider {
	case "slack", "stripe":
		if def > 0 && def < signedWindowTTL {
			return def
		}
		return signedWindowTTL
	default:
		return def
	}
}

// ErrQueueFull is Put's answer when the queue has reached maxEntries or
// maxBytes. The caller decides what that means: while the workspace is awake
// the entry is only crash insurance and the forward goes ahead without it;
// while it is away there is nothing else to do with the request, so the sender
// is told so rather than being handed a 202 for bytes nobody kept.
var ErrQueueFull = errors.New("queue is full")

// Entry is one buffered request, enough to replay it byte for byte.
//
// Path carries the query string, because a sender is free to put one there and
// a proxy that dropped it would change the request. Attempts counts deliveries
// that ended ambiguously, so a webhook cannot be retried for ever.
type Entry struct {
	Kind     string      `json:"kind"`
	ID       string      `json:"id"`
	Provider string      `json:"provider,omitempty"`
	Method   string      `json:"method"`
	Path     string      `json:"path"`
	Header   http.Header `json:"header,omitempty"`
	Body     []byte      `json:"body,omitempty"`
	QueuedAt time.Time   `json:"queued_at"`
	Attempts int         `json:"attempts,omitempty"`
}

// QueueLimits bounds what the volume can be made to hold. Both are needed: a
// thousand tiny crons and one hundred 1 MiB webhooks are different shapes of
// the same problem.
type QueueLimits struct {
	MaxEntries int
	MaxBytes   int64
	TTL        time.Duration
}

// Queue is the on-disk queue. Two directories under dir:
//
//	pending/   waiting to be delivered, named so a lexical sort is time order
//	inflight/  claimed by a goroutine that is delivering it now
//
// A third, tmp/, holds half-written files so that a directory scan of pending/
// never sees one.
type Queue struct {
	dir     string
	pending string
	infl    string
	tmp     string
	limits  QueueLimits

	mu    sync.Mutex
	count int   // entries in pending/ + inflight/
	bytes int64 // their total file size

	syncWarn sync.Once
}

// NewQueue opens or creates the queue in dir and recovers what a previous
// keeper left behind.
func NewQueue(dir string, limits QueueLimits) (*Queue, error) {
	if dir == "" {
		return nil, errors.New("queue: no directory configured")
	}
	if limits.MaxEntries <= 0 {
		limits.MaxEntries = 256
	}
	if limits.MaxBytes <= 0 {
		limits.MaxBytes = 64 << 20
	}
	if limits.TTL <= 0 {
		limits.TTL = time.Hour
	}
	q := &Queue{
		dir:     dir,
		pending: filepath.Join(dir, "pending"),
		infl:    filepath.Join(dir, "inflight"),
		tmp:     filepath.Join(dir, "tmp"),
		limits:  limits,
	}
	for _, d := range []string{q.pending, q.infl, q.tmp} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return nil, fmt.Errorf("create queue dir: %w", err)
		}
	}
	// A tmp/ file is a write that never completed, so it was never acknowledged
	// to anyone and belongs to nobody.
	if names, err := os.ReadDir(q.tmp); err == nil {
		for _, n := range names {
			_ = os.Remove(filepath.Join(q.tmp, n.Name()))
		}
	}
	if err := q.recover(); err != nil {
		return nil, err
	}
	return q, nil
}

// recover counts what is on disk and decides what to do with entries a previous
// keeper was delivering when it died. Those are the ambiguous ones: the request
// may have reached the workspace or may not, and nothing on either side can
// say which. The per-kind semantics above decide, and a dropped entry says so
// in the log, because a cron that silently vanished is the kind of thing an
// operator should be able to find afterwards.
func (q *Queue) recover() error {
	pend, err := q.scan(q.pending)
	if err != nil {
		return err
	}
	q.mu.Lock()
	q.count, q.bytes = len(pend), 0
	for _, f := range pend {
		q.bytes += f.size
	}
	q.mu.Unlock()

	infl, err := q.scan(q.infl)
	if err != nil {
		return err
	}
	var requeued, dropped int
	for _, f := range infl {
		e, rerr := q.read(filepath.Join(q.infl, f.name))
		if rerr != nil {
			// Unreadable: it cannot be replayed and it cannot be reasoned
			// about. Set it aside rather than deleting it, so whatever went
			// wrong is still there to look at, and carry on with the rest.
			log.Printf("queue: recover: %v; setting it aside", rerr)
			_ = os.Rename(filepath.Join(q.infl, f.name), filepath.Join(q.infl, f.name+".corrupt"))
			continue
		}
		if semanticsFor(e.Kind) == atMostOnce {
			log.Printf("queue: dropping %s/%s, queued %s: the keeper restarted while it was being delivered and %s may not be delivered twice (the schedule fires it again)",
				e.Kind, e.ID, e.QueuedAt.UTC().Format(time.RFC3339), e.Kind)
			_ = os.Remove(filepath.Join(q.infl, f.name))
			dropped++
			continue
		}
		if err := os.Rename(filepath.Join(q.infl, f.name), filepath.Join(q.pending, f.name)); err != nil {
			log.Printf("queue: recover: requeue %s/%s: %v", e.Kind, e.ID, err)
			continue
		}
		q.mu.Lock()
		q.count++
		q.bytes += f.size
		q.mu.Unlock()
		requeued++
	}
	if requeued > 0 || dropped > 0 {
		log.Printf("queue: recovered %d entr(ies) a previous keeper was delivering: %d requeued, %d dropped", requeued+dropped, requeued, dropped)
	}
	if n := q.Len(); n > 0 {
		log.Printf("queue: %d request(s) waiting for the workspace", n)
	}
	return nil
}

type queueFile struct {
	name string
	size int64
}

// scan lists the entry files in a directory, oldest first. Names that are not
// entries — a .corrupt set aside earlier, anything a human dropped in — are
// skipped rather than tripping over.
func (q *Queue) scan(dir string) ([]queueFile, error) {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read queue dir %s: %w", dir, err)
	}
	var out []queueFile
	for _, e := range ents {
		if e.IsDir() || filepath.Ext(e.Name()) != ".json" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, queueFile{name: e.Name(), size: info.Size()})
	}
	// The names begin with a zero-padded timestamp, so lexical order is arrival
	// order — which is the order a sender expects its deliveries to be made in.
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, nil
}

func (q *Queue) read(path string) (*Entry, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read queue entry: %w", err)
	}
	var e Entry
	if err := json.Unmarshal(b, &e); err != nil {
		return nil, fmt.Errorf("decode queue entry %s: %w", filepath.Base(path), err)
	}
	if e.Kind == "" || e.ID == "" || e.Method == "" {
		return nil, fmt.Errorf("queue entry %s is incomplete", filepath.Base(path))
	}
	return &e, nil
}

// Put writes an entry and makes it durable before returning.
//
// The whole point of this function is the order of its last three steps: write,
// fsync the file, then publish it by rename. A caller may answer 202 only after
// Put has returned, and must not wake the workspace before it either — a wake
// that raced an unwritten entry would hand the workspace back with nothing to
// replay and no record that anything was lost.
//
// Returns the claim token the entry would be claimed under, so a caller
// delivering it immediately (the awake fast path) can address it without a
// scan.
func (q *Queue) Put(e *Entry) (string, error) {
	if e.QueuedAt.IsZero() {
		e.QueuedAt = time.Now()
	}
	b, err := json.Marshal(e)
	if err != nil {
		return "", fmt.Errorf("encode queue entry: %w", err)
	}
	size := int64(len(b))

	q.mu.Lock()
	full := q.count >= q.limits.MaxEntries || q.bytes+size > q.limits.MaxBytes
	if !full {
		// Reserve the space before the write, so two concurrent Puts cannot
		// both pass a check that only one of them fits through.
		q.count++
		q.bytes += size
	}
	q.mu.Unlock()
	if full {
		return "", ErrQueueFull
	}

	name, err := entryName()
	if err != nil {
		q.release(size)
		return "", err
	}
	tmp := filepath.Join(q.tmp, name)
	if err := q.writeSynced(tmp, b); err != nil {
		q.release(size)
		return "", err
	}
	if err := os.Rename(tmp, filepath.Join(q.pending, name)); err != nil {
		_ = os.Remove(tmp)
		q.release(size)
		return "", fmt.Errorf("publish queue entry: %w", err)
	}
	q.syncDir(q.pending)
	return name, nil
}

// writeSynced writes the bytes and does not return until the kernel says they
// are on the device. 0600 because an entry's headers can carry a cron's fire
// token; the file is on a volume no other pod mounts, and this is the second
// lock on it.
func (q *Queue) writeSynced(path string, b []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return fmt.Errorf("create queue entry: %w", err)
	}
	if _, err := f.Write(b); err != nil {
		_ = f.Close()
		return fmt.Errorf("write queue entry: %w", err)
	}
	// The one fsync the durability claim rests on. An error here is fatal to
	// the Put: answering 202 for bytes the kernel has not committed is exactly
	// the lie this design exists to avoid.
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("sync queue entry: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close queue entry: %w", err)
	}
	return nil
}

// syncDir makes the rename itself survive a power cut. Best-effort on purpose:
// Windows refuses to flush a directory handle, and the dev box is Windows, so
// a hard error here would make the queue untestable where it is written. The
// file's own contents are synced above, which is the half that matters — a lost
// rename loses a request the same way a crash a microsecond earlier does.
func (q *Queue) syncDir(dir string) {
	d, err := os.Open(dir)
	if err == nil {
		err = d.Sync()
		_ = d.Close()
	}
	if err != nil {
		q.syncWarn.Do(func() {
			log.Printf("queue: this filesystem will not flush a directory (%v); entry contents are still flushed, but a power cut could lose the most recent entry", err)
		})
	}
}

// release gives back the space an entry was holding. It floors at zero rather
// than trusting its own arithmetic: the counters only exist to enforce a cap,
// and a count that drifted negative would quietly raise that cap instead of
// showing up as a failure.
func (q *Queue) release(size int64) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.count > 0 {
		q.count--
	}
	if q.bytes -= size; q.bytes < 0 {
		q.bytes = 0
	}
}

// Claim takes ownership of the oldest pending entry, or reports that there is
// nothing to do.
//
// Ownership is the rename into inflight/. Two goroutines calling Claim at the
// same moment both see the same file; the rename decides between them, and the
// loser moves on to the next one. Nothing else in the keeper needs a lock for
// this, because the filesystem already provides exactly the primitive wanted.
func (q *Queue) Claim() (*Entry, string, bool) {
	files, err := q.scan(q.pending)
	if err != nil {
		log.Printf("queue: claim: %v", err)
		return nil, "", false
	}
	for _, f := range files {
		if err := os.Rename(filepath.Join(q.pending, f.name), filepath.Join(q.infl, f.name)); err != nil {
			continue // somebody else got it, or it has just been expired
		}
		e, err := q.read(filepath.Join(q.infl, f.name))
		if err != nil {
			// A truncated or hand-edited file must not stop the entries behind
			// it from draining, and must not be retried for ever either.
			//
			// The accounting hangs off whether the set-aside rename worked, not
			// off the read: that way exactly one caller ever un-counts this
			// entry, however many of them tried to read it.
			log.Printf("queue: %v; setting it aside", err)
			if rerr := os.Rename(filepath.Join(q.infl, f.name), filepath.Join(q.infl, f.name+".corrupt")); rerr == nil {
				q.release(f.size)
			}
			continue
		}
		return e, f.name, true
	}
	return nil, "", false
}

// ClaimToken claims one entry by name, for the fast path where the entry to
// deliver is the one just written and a scan would be pointless. The error is
// what says somebody else got there first — a drain loop that happened to pick
// the same entry up — and the caller must then leave it alone.
func (q *Queue) ClaimToken(token string) error {
	if err := os.Rename(filepath.Join(q.pending, token), filepath.Join(q.infl, token)); err != nil {
		return fmt.Errorf("claim queue entry: %w", err)
	}
	return nil
}

// Done deletes a delivered entry. Called only once the workspace's own answer
// has been seen, so what it forgets has certainly been handled.
func (q *Queue) Done(token string) {
	path := filepath.Join(q.infl, token)
	var size int64
	if st, err := os.Stat(path); err == nil {
		size = st.Size()
	}
	// The remove is what un-counts the entry, so a second Done for the same
	// token — a caller being careful twice — adjusts nothing. An entry that is
	// already gone was already accounted for.
	if err := os.Remove(path); err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			log.Printf("queue: remove delivered entry: %v", err)
		}
		return
	}
	q.release(size)
}

// maxAttempts bounds how many times a delivery may end in doubt before the
// entry is given up on, so a request the workspace reliably half-answers cannot
// be replayed for ever.
//
// It counts ambiguous deliveries only — see Return, which does not spend one.
const maxAttempts = 5

// Return puts an entry back without counting an attempt against it.
//
// For a delivery that demonstrably did not happen: the connection was refused,
// so not a byte was written and nothing about the request is in doubt. Counting
// these would be the wrong bound entirely — a workspace unreachable for half a
// minute would exhaust maxAttempts and the entry would be dropped, losing an
// event that was never delivered once. The TTL is the backstop here, and it
// expires with a line that says what happened.
func (q *Queue) Return(token string, e *Entry) {
	q.requeue(token, e)
}

func (q *Queue) requeue(token string, e *Entry) {
	b, err := json.Marshal(e)
	if err != nil {
		log.Printf("queue: requeue %s/%s: %v", e.Kind, e.ID, err)
		return
	}
	// Rewrite it where it stands, then publish it back into pending/ by the
	// same rename Put uses: at no point is the entry in neither directory.
	tmp := filepath.Join(q.tmp, token)
	if err := q.writeSynced(tmp, b); err != nil {
		log.Printf("queue: requeue %s/%s: %v", e.Kind, e.ID, err)
		return
	}
	if err := os.Rename(tmp, filepath.Join(q.pending, token)); err != nil {
		log.Printf("queue: requeue %s/%s: %v", e.Kind, e.ID, err)
		_ = os.Remove(tmp)
		return
	}
	_ = os.Remove(filepath.Join(q.infl, token))
	q.syncDir(q.pending)
}

// Resolve ends a claim whose delivery could not be established, by the kind's
// own semantics. This is the single place the at-least-once / at-most-once
// decision is applied, so neither rule can be lost in a branch somewhere.
func (q *Queue) Resolve(token string, e *Entry) {
	if semanticsFor(e.Kind) == atMostOnce {
		log.Printf("queue: dropping %s/%s: the workspace may already have run it and %s may not run twice (the schedule fires it again)", e.Kind, e.ID, e.Kind)
		q.Done(token)
		return
	}
	e.Attempts++
	if e.Attempts >= maxAttempts {
		log.Printf("queue: giving up on %s/%s after %d attempts that could not be confirmed; dropping it", e.Kind, e.ID, e.Attempts)
		q.Done(token)
		return
	}
	q.requeue(token, e)
}

// PeekKind is the kind of the oldest waiting entry, or "" when nothing is
// waiting. Only so a wake the queue asks for can say truthfully why it happened
// — "a webhook arrived" or "a scheduled task is due" — rather than guessing and
// showing the wrong sentence to whoever is looking at the page.
func (q *Queue) PeekKind() string {
	files, err := q.scan(q.pending)
	if err != nil || len(files) == 0 {
		return ""
	}
	e, err := q.read(filepath.Join(q.pending, files[0].name))
	if err != nil {
		return ""
	}
	return e.Kind
}

// ExpireOlderThan drops entries whose signature can no longer verify, or which
// have simply waited too long, and says so for each one.
//
// The log line is the feature. Without it a Slack webhook queued across a wake
// comes back as `404 Not found or unauthorized` with ledger
// reason='bad_signature' and sends its owner looking for a wrong secret that
// does not exist. Expiring it here names what actually happened.
func (q *Queue) ExpireOlderThan(now time.Time) int {
	files, err := q.scan(q.pending)
	if err != nil {
		log.Printf("queue: expire: %v", err)
		return 0
	}
	n := 0
	for _, f := range files {
		path := filepath.Join(q.pending, f.name)
		e, err := q.read(path)
		if err != nil {
			continue // Claim deals with unreadable entries
		}
		ttl := ttlFor(e, q.limits.TTL)
		age := now.Sub(e.QueuedAt)
		if age < ttl {
			continue
		}
		if err := os.Remove(path); err != nil {
			continue
		}
		q.release(f.size)
		n++
		switch e.Provider {
		case "slack", "stripe":
			log.Printf("queue: expired %s/%s after %s: %s signs a timestamp and refuses anything older than 5 minutes, so replaying it now would be rejected as a bad signature rather than as the late delivery it is. The workspace was away too long.",
				e.Kind, e.ID, age.Round(time.Second), e.Provider)
		default:
			log.Printf("queue: expired %s/%s after %s, past the %s this queue keeps requests for", e.Kind, e.ID, age.Round(time.Second), ttl)
		}
	}
	return n
}

// Len is how many requests are waiting or in flight. Counted in memory, since
// this process is the only writer, so the drain loop can ask constantly without
// touching the disk. A nil queue — a keeper with no state volume — holds
// nothing, which is what the idle loop needs to hear.
func (q *Queue) Len() int {
	if q == nil {
		return 0
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.count
}

// entryName makes a filename that sorts by arrival time and cannot collide:
// nanoseconds for the order, randomness because two requests can land inside
// the same clock tick.
func entryName() (string, error) {
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("queue name: %w", err)
	}
	return fmt.Sprintf("%020d-%s.json", time.Now().UnixNano(), hex.EncodeToString(b[:])), nil
}
