package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func newQueue(t *testing.T) *Queue {
	t.Helper()
	q, err := NewQueue(filepath.Join(t.TempDir(), "queue"), QueueLimits{})
	if err != nil {
		t.Fatal(err)
	}
	return q
}

// entry is a plausible signed GitHub delivery: the headers server.py verifies
// over, and a body whose exact bytes the signature covers.
func entry(kind, id string) *Entry {
	return &Entry{
		Kind: kind, ID: id, Method: http.MethodPost,
		Path: "/api/webhooks/" + id,
		Header: http.Header{
			"Content-Type":        {"application/json"},
			"X-Hub-Signature-256": {"sha256=0b1c"},
			"X-Github-Delivery":   {"d-" + id},
		},
		Body: []byte(`{"action":"opened"}`),
	}
}

func TestQueueRoundTripsAnEntryByteForByte(t *testing.T) {
	q := newQueue(t)
	in := entry("webhook", "gh")
	in.Body = []byte("\x00\r\n\xff\xfe lone\rCR \xf0\x9f\x92\x80 not utf-8 above")
	in.Header.Add("X-Hub-Signature-256", "sha256=second") // a repeated header
	if _, err := q.Put(in); err != nil {
		t.Fatal(err)
	}
	got, _, ok := q.Claim()
	if !ok {
		t.Fatal("nothing claimed")
	}
	if string(got.Body) != string(in.Body) {
		t.Fatalf("body = %q; want %q — the HMAC covers these exact bytes", got.Body, in.Body)
	}
	if n := len(got.Header["X-Hub-Signature-256"]); n != 2 {
		t.Fatalf("kept %d signature headers; want both, because only the workspace decides what two means", n)
	}
	if got.Method != in.Method || got.Path != in.Path || got.Kind != in.Kind || got.ID != in.ID {
		t.Fatalf("round trip lost something: %+v", got)
	}
}

// The durability claim, as close to a power cut as a test can get: the entry is
// a file on disk the moment Put returns, so a keeper that dies immediately
// after answering 202 has already written the request down. A second Queue over
// the same directory is what a restarted keeper does.
func TestQueueSurvivesAKeeperThatDiesAfterTheAnswer(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "queue")
	q, err := NewQueue(dir, QueueLimits{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := q.Put(entry("webhook", "gh")); err != nil {
		t.Fatal(err)
	}
	// No Close, no flush, no graceful anything: the process simply stops here.
	again, err := NewQueue(dir, QueueLimits{})
	if err != nil {
		t.Fatal(err)
	}
	if again.Len() != 1 {
		t.Fatalf("after a restart the queue holds %d; want the one request it accepted", again.Len())
	}
	e, _, ok := again.Claim()
	if !ok || e.ID != "gh" {
		t.Fatalf("claim = %+v, %v; want the queued webhook", e, ok)
	}
}

func TestQueueEntryFilesAre0600(t *testing.T) {
	if isWindows() {
		t.Skip("file modes are not POSIX here; CI runs this on Linux")
	}
	q := newQueue(t)
	// A cron entry's headers carry its live fire token — that is what makes the
	// replay byte-identical, and what makes the mode matter.
	e := entry("cron", "nightly")
	e.Header.Set("Authorization", "Bearer fire-token")
	if _, err := q.Put(e); err != nil {
		t.Fatal(err)
	}
	found := 0
	err := filepath.Walk(q.dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		found++
		if m := info.Mode().Perm(); m != 0o600 {
			t.Errorf("%s is %o; want 0600 — it holds a live bearer token", path, m)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if found != 1 {
		t.Fatalf("walked %d files; want the one entry", found)
	}
}

// Claim is the one-owner primitive the whole no-double-delivery argument rests
// on. Run it from many goroutines against many entries and count: every entry
// must be handed out exactly once.
//
// POSIX only, and deliberately not papered over. The claim IS the rename, and
// rename is exclusive on Linux — measured here, 8 goroutines over 60 entries
// gave 60 distinct claims, 0 duplicates and 366 cleanly lost races. Windows
// does not provide that: MoveFileEx lets a second caller rename the same source
// and the delete is asynchronous, so two owners can hold one entry. The keeper
// ships on distroless Linux, so the property holds where it runs, and this test
// runs where the property exists — `make keeper-test-race` in WSL, the same
// reason that target exists at all.
func TestQueueClaimHandsEachEntryToExactlyOneOwner(t *testing.T) {
	if isWindows() {
		t.Skip("rename is not an exclusive claim on Windows; run `make keeper-test-race` (WSL), which is also where -race runs")
	}
	q := newQueue(t)
	const n = 60
	for i := 0; i < n; i++ {
		if _, err := q.Put(entry("webhook", fmt.Sprintf("w%02d", i))); err != nil {
			t.Fatal(err)
		}
	}
	var mu sync.Mutex
	seen := map[string]int{}
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				e, tok, ok := q.Claim()
				if !ok {
					return
				}
				mu.Lock()
				seen[e.ID]++
				mu.Unlock()
				q.Done(tok)
			}
		}()
	}
	wg.Wait()
	if len(seen) != n {
		t.Fatalf("claimed %d distinct entries; want %d", len(seen), n)
	}
	for id, c := range seen {
		if c != 1 {
			t.Fatalf("%s was claimed %d times; two owners means two agent runs", id, c)
		}
	}
	if q.Len() != 0 {
		t.Fatalf("queue still holds %d after every entry was done", q.Len())
	}
}

// Arrival order is what a sender expects its deliveries to be handled in.
func TestQueueDrainsInArrivalOrder(t *testing.T) {
	q := newQueue(t)
	for i := 0; i < 5; i++ {
		if _, err := q.Put(entry("webhook", fmt.Sprintf("w%d", i))); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 5; i++ {
		e, tok, ok := q.Claim()
		if !ok {
			t.Fatalf("ran out after %d", i)
		}
		if want := fmt.Sprintf("w%d", i); e.ID != want {
			t.Fatalf("claim %d = %s; want %s", i, e.ID, want)
		}
		q.Done(tok)
	}
}

func TestQueueRefusesWhenFull(t *testing.T) {
	q, err := NewQueue(filepath.Join(t.TempDir(), "q"), QueueLimits{MaxEntries: 3})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if _, err := q.Put(entry("webhook", fmt.Sprintf("w%d", i))); err != nil {
			t.Fatalf("put %d: %v", i, err)
		}
	}
	if _, err := q.Put(entry("webhook", "overflow")); err != ErrQueueFull {
		t.Fatalf("put past the cap = %v; want ErrQueueFull so the caller can say so out loud", err)
	}
	if q.Len() != 3 {
		t.Fatalf("len = %d; a refused Put must not count", q.Len())
	}
	// And the space comes back, so a full queue recovers by draining.
	_, tok, _ := q.Claim()
	q.Done(tok)
	if _, err := q.Put(entry("webhook", "after-drain")); err != nil {
		t.Fatalf("put after draining one: %v", err)
	}
}

func TestQueueRefusesPastMaxBytes(t *testing.T) {
	// A body of 3000 bytes is about 4100 on disk, because JSON renders it as
	// base64. Two of them do not fit in 8000, which is the point: the byte cap
	// has to bind before the entry cap does, or a handful of 1 MiB webhooks
	// would fill a volume while the count still looked healthy.
	q, err := NewQueue(filepath.Join(t.TempDir(), "q"), QueueLimits{MaxEntries: 1000, MaxBytes: 8000})
	if err != nil {
		t.Fatal(err)
	}
	big := entry("webhook", "big")
	big.Body = make([]byte, 3000)
	if _, err := q.Put(big); err != nil {
		t.Fatalf("first big put: %v", err)
	}
	if _, err := q.Put(big); err != ErrQueueFull {
		t.Fatalf("second big put = %v; want ErrQueueFull from the byte cap", err)
	}
	if q.Len() != 1 {
		t.Fatalf("len = %d; the refused Put must leave the counters alone", q.Len())
	}
}

// A truncated or hand-edited entry must not panic, must not be retried for
// ever, and above all must not block the entries behind it.
func TestQueueSetsACorruptEntryAsideAndKeepsDraining(t *testing.T) {
	q := newQueue(t)
	if _, err := q.Put(entry("webhook", "bad")); err != nil {
		t.Fatal(err)
	}
	if _, err := q.Put(entry("webhook", "good")); err != nil {
		t.Fatal(err)
	}
	files, err := q.scan(q.pending)
	if err != nil {
		t.Fatal(err)
	}
	// Truncate the first one, which is also the one Claim will reach first.
	if err := os.WriteFile(filepath.Join(q.pending, files[0].name), []byte(`{"kind":"web`), 0o600); err != nil {
		t.Fatal(err)
	}
	e, tok, ok := q.Claim()
	if !ok {
		t.Fatal("a corrupt entry at the head stopped the queue dead")
	}
	if e.ID != "good" {
		t.Fatalf("claimed %s; want the intact entry behind the corrupt one", e.ID)
	}
	q.Done(tok)
	if _, _, ok := q.Claim(); ok {
		t.Fatal("the corrupt entry came back; it must be set aside, not retried")
	}
	if _, err := os.Stat(filepath.Join(q.infl, files[0].name+".corrupt")); err != nil {
		t.Fatalf("the corrupt entry was deleted rather than kept for inspection: %v", err)
	}
}

// The at-most-once rule, at the point it actually bites: a keeper killed
// mid-forward leaves an entry in inflight/, and nothing on either side can say
// whether the workspace ran it. A webhook is retried, a cron is not — because
// handle_cron_fire has no dedup at all, so two replays are two agent runs,
// while a missed tick just fires again on schedule.
func TestQueueRecoveryAppliesPerKindSemantics(t *testing.T) {
	for _, c := range []struct {
		kind    string
		recover bool
	}{
		{"webhook", true},
		{"cron", false},
		{"page-watch", false},
	} {
		t.Run(c.kind, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "q")
			q, err := NewQueue(dir, QueueLimits{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := q.Put(entry(c.kind, "t1")); err != nil {
				t.Fatal(err)
			}
			if _, _, ok := q.Claim(); !ok { // now in flight; the keeper dies here
				t.Fatal("claim")
			}
			again, err := NewQueue(dir, QueueLimits{})
			if err != nil {
				t.Fatal(err)
			}
			_, _, ok := again.Claim()
			if ok != c.recover {
				t.Fatalf("%s recovered = %v; want %v (%s)", c.kind, ok, c.recover, semanticsFor(c.kind))
			}
			if !c.recover && again.Len() != 0 {
				t.Fatalf("%s left %d entries behind; a dropped entry must be gone", c.kind, again.Len())
			}
		})
	}
}

// Resolve is the live half of the same rule.
func TestQueueResolveAppliesPerKindSemantics(t *testing.T) {
	q := newQueue(t)
	for _, c := range []struct {
		kind string
		back bool
	}{
		{"webhook", true},
		{"cron", false},
	} {
		if _, err := q.Put(entry(c.kind, "t-"+c.kind)); err != nil {
			t.Fatal(err)
		}
		e, tok, ok := q.Claim()
		if !ok {
			t.Fatal("claim")
		}
		q.Resolve(tok, e)
		_, tok2, back := q.Claim()
		if back != c.back {
			t.Fatalf("%s came back = %v; want %v", c.kind, back, c.back)
		}
		if back {
			q.Done(tok2)
		}
	}
}

// A webhook whose delivery is never confirmed must not be replayed for ever.
func TestQueueGivesUpAfterMaxAttempts(t *testing.T) {
	q := newQueue(t)
	if _, err := q.Put(entry("webhook", "flaky")); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < maxAttempts+2; i++ {
		e, tok, ok := q.Claim()
		if !ok {
			if i < 2 {
				t.Fatalf("gave up after only %d attempts", i)
			}
			return
		}
		q.Resolve(tok, e)
	}
	t.Fatalf("still retrying after %d attempts", maxAttempts+2)
}

// The per-provider TTL, which exists so that a Slack webhook queued across a
// long wake is expired with a line that says what happened, instead of being
// replayed into a `404 bad_signature` that reads as "your secret is wrong".
func TestQueueExpiresSignedProvidersInsideTheirWindow(t *testing.T) {
	cases := []struct {
		provider string
		age      time.Duration
		expired  bool
	}{
		{"github", 10 * time.Minute, false},
		{"generic", 10 * time.Minute, false},
		{"slack", 2 * time.Minute, false},
		{"slack", 5 * time.Minute, true},
		{"stripe", 5 * time.Minute, true},
		{"github", 2 * time.Hour, true}, // the default TTL still applies
	}
	for _, c := range cases {
		t.Run(c.provider+"/"+c.age.String(), func(t *testing.T) {
			q, err := NewQueue(filepath.Join(t.TempDir(), "q"), QueueLimits{TTL: time.Hour})
			if err != nil {
				t.Fatal(err)
			}
			e := entry("webhook", "w")
			e.Provider = c.provider
			e.QueuedAt = time.Now().Add(-c.age)
			if _, err := q.Put(e); err != nil {
				t.Fatal(err)
			}
			n := q.ExpireOlderThan(time.Now())
			if (n == 1) != c.expired {
				t.Fatalf("%s at %s expired %d; want expired=%v", c.provider, c.age, n, c.expired)
			}
			if q.Len() != 1-n {
				t.Fatalf("len = %d after expiring %d", q.Len(), n)
			}
		})
	}
}

// The expiry log line is the whole point of expiring rather than replaying, so
// it has to name the real cause. An operator who reads "bad signature" goes
// looking for a wrong secret that does not exist.
func TestQueueExpiryLogNamesTheTimestampWindow(t *testing.T) {
	q, err := NewQueue(filepath.Join(t.TempDir(), "q"), QueueLimits{TTL: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	e := entry("webhook", "w")
	e.Provider = "slack"
	e.QueuedAt = time.Now().Add(-10 * time.Minute)
	if _, err := q.Put(e); err != nil {
		t.Fatal(err)
	}
	buf := captureLog(t)
	q.ExpireOlderThan(time.Now())
	logged := buf.String()
	for _, want := range []string{"slack", "timestamp", "5 minutes"} {
		if !strings.Contains(logged, want) {
			t.Fatalf("expiry log %q does not mention %q", logged, want)
		}
	}
	if strings.Contains(logged, "fire-token") || strings.Contains(logged, "sha256=") {
		t.Fatalf("expiry log leaked a credential: %q", logged)
	}
}

// Nothing in the queue may ever log a header value: a cron entry's headers hold
// its live fire token, and a webhook's hold its signature.
func TestQueueNeverLogsHeaderValues(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "q")
	buf := captureLog(t)
	q, err := NewQueue(dir, QueueLimits{})
	if err != nil {
		t.Fatal(err)
	}
	e := entry("cron", "nightly")
	e.Header.Set("Authorization", "Bearer super-secret-fire-token")
	tok, err := q.Put(e)
	if err != nil {
		t.Fatal(err)
	}
	if err := q.ClaimToken(tok); err != nil {
		t.Fatal(err)
	}
	// A restart with it in flight: the drop path logs about this entry.
	if _, err := NewQueue(dir, QueueLimits{}); err != nil {
		t.Fatal(err)
	}
	logged := buf.String()
	if strings.Contains(logged, "super-secret-fire-token") {
		t.Fatalf("the queue logged a fire token: %q", logged)
	}
	if !strings.Contains(logged, "nightly") {
		t.Fatalf("a dropped cron must be findable in the log afterwards; got %q", logged)
	}
}

func TestQueueClaimTokenIsExclusive(t *testing.T) {
	q := newQueue(t)
	tok, err := q.Put(entry("webhook", "w"))
	if err != nil {
		t.Fatal(err)
	}
	if err := q.ClaimToken(tok); err != nil {
		t.Fatalf("first claim: %v", err)
	}
	if err := q.ClaimToken(tok); err == nil {
		t.Fatal("the same entry was claimed twice; one owner per entry is the whole rule")
	}
}

// An entry that is in flight is not pending, so a concurrent drain cannot see
// it — and Put's reserved space is still counted, so the cap cannot be walked
// past by claiming.
func TestQueueLenCountsInflight(t *testing.T) {
	q := newQueue(t)
	if _, err := q.Put(entry("webhook", "w")); err != nil {
		t.Fatal(err)
	}
	_, tok, ok := q.Claim()
	if !ok {
		t.Fatal("claim")
	}
	if q.Len() != 1 {
		t.Fatalf("len = %d while an entry is in flight; want 1, it is still the keeper's", q.Len())
	}
	q.Done(tok)
	if q.Len() != 0 {
		t.Fatalf("len = %d after done", q.Len())
	}
}

// tmp/ holds half-written files. One left behind by a crash was never
// acknowledged to anybody, so it belongs to nobody and must not be drained.
func TestQueueDiscardsHalfWrittenFilesOnOpen(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "q")
	q, err := NewQueue(dir, QueueLimits{})
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(entry("webhook", "half"))
	if err := os.WriteFile(filepath.Join(q.tmp, "00-half.json"), b[:len(b)/2], 0o600); err != nil {
		t.Fatal(err)
	}
	again, err := NewQueue(dir, QueueLimits{})
	if err != nil {
		t.Fatal(err)
	}
	if again.Len() != 0 {
		t.Fatalf("len = %d; a file that never left tmp/ was never accepted", again.Len())
	}
	if _, _, ok := again.Claim(); ok {
		t.Fatal("a half-written file was drained")
	}
}

func isWindows() bool { return os.PathSeparator == '\\' }

// A refused connection must not spend one of the five attempts. Counting those
// would be the wrong bound entirely: a workspace unreachable for half a minute
// would exhaust the budget at one drain tick per five seconds, and the entry
// would be dropped — losing an event that was never delivered even once.
func TestQueueReturnDoesNotSpendAnAttempt(t *testing.T) {
	q := newQueue(t)
	if _, err := q.Put(entry("webhook", "unreachable")); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < maxAttempts*3; i++ {
		e, tok, ok := q.Claim()
		if !ok {
			t.Fatalf("the entry was dropped after %d returns; a refused connection is not an ambiguous delivery", i)
		}
		if e.Attempts != 0 {
			t.Fatalf("after %d returns Attempts = %d; want 0", i, e.Attempts)
		}
		q.Return(tok, e)
	}
}

// The wake the drain loop asks for has to say truthfully what is waiting, or
// the page tells someone a scheduled task is due when a webhook arrived.
func TestQueuePeekKindNamesTheOldestWaitingEntry(t *testing.T) {
	q := newQueue(t)
	if got := q.PeekKind(); got != "" {
		t.Fatalf("an empty queue peeked %q", got)
	}
	if _, err := q.Put(entry("cron", "nightly")); err != nil {
		t.Fatal(err)
	}
	if _, err := q.Put(entry("webhook", "gh")); err != nil {
		t.Fatal(err)
	}
	if got := q.PeekKind(); got != "cron" {
		t.Fatalf("peeked %q; want the oldest entry's kind", got)
	}
	_, tok, _ := q.Claim()
	q.Done(tok)
	if got := q.PeekKind(); got != "webhook" {
		t.Fatalf("peeked %q after the cron was delivered; want webhook", got)
	}
}
