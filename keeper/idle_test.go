package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeIdle is the workspace's GET /api/keeper/idle. It answers idle by default
// and records what the keeper sent.
type fakeIdle struct {
	mu    sync.Mutex
	code  int    // 0 = 200
	body  string // "" = idle for an hour, nothing busy
	delay time.Duration
	// during, if set, runs while the poll is being answered: something that
	// happens in the window between the keeper asking and the keeper acting.
	during func()
	got    []http.Header
	query  []url.Values
	paths  []string
}

func (fi *fakeIdle) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	fi.mu.Lock()
	fi.got = append(fi.got, r.Header.Clone())
	fi.query = append(fi.query, r.URL.Query())
	fi.paths = append(fi.paths, r.URL.Path)
	code, body, delay, during := fi.code, fi.body, fi.delay, fi.during
	fi.during = nil
	fi.mu.Unlock()
	if during != nil {
		during()
	}
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-r.Context().Done():
			return
		}
	}
	if code == 0 {
		code = http.StatusOK
	}
	if body == "" {
		body = answer(true, 3600)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write([]byte(body))
}

func (fi *fakeIdle) set(fn func(*fakeIdle)) {
	fi.mu.Lock()
	defer fi.mu.Unlock()
	fn(fi)
}

func (fi *fakeIdle) polls() int {
	fi.mu.Lock()
	defer fi.mu.Unlock()
	return len(fi.got)
}

// answer is a keeper_idle.py report, cut down to the fields the keeper reads.
func answer(idle bool, idleFor float64, busy ...string) string {
	if busy == nil {
		busy = []string{}
	}
	b, _ := json.Marshal(map[string]any{
		"idle": idle, "idle_for_s": idleFor, "busy_signals": busy,
		"reason": fmt.Sprintf("quiet for %ds", int(idleFor)),
	})
	return string(b)
}

// newIdleServer is newProxyServer with sleep on and the workspace replaced by
// an idle endpoint. Built on the proxy constructor because the idle loop has to
// see the same queue the proxy fills.
func newIdleServer(t *testing.T, f *fakeAPI, k *Kube) (*Server, *fakeIdle) {
	t.Helper()
	s, _ := newProxyServer(t, f, k)
	fi := &fakeIdle{}
	srv := httptest.NewServer(fi)
	t.Cleanup(srv.Close)
	s.WorkspaceURL = srv.URL
	s.SleepEnabled, s.IdleMinutes = true, 30
	return s, fi
}

func rounds(s *Server, n int) {
	for i := 0; i < n; i++ {
		s.sleepOnce(context.Background())
	}
}

// sleeps counts the keeper's scale-to-0 writes.
func sleeps(f *fakeAPI) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.scales {
		if c.Replicas == 0 {
			n++
		}
	}
	return n
}

func stateOf(t *testing.T, k *Kube) string {
	t.Helper()
	d, err := k.Get(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return Classify(d)
}

func queueOne(t *testing.T, s *Server) string {
	t.Helper()
	token, err := s.Queue.Put(&Entry{Kind: "webhook", ID: "gh", Method: http.MethodPost,
		Path: "/api/webhooks/gh", Body: []byte(`{}`), QueuedAt: time.Now()})
	if err != nil {
		t.Fatal(err)
	}
	return token
}

// ── the decision ────────────────────────────────────────────────────────────

func TestIdleVerdictSleepable(t *testing.T) {
	yes, no := true, false
	thirty := 30 * time.Minute
	cases := []struct {
		name string
		v    idleVerdict
		want bool
	}{
		{"idle for an hour, nothing busy", idleVerdict{Idle: &yes, IdleForS: 3600}, true},
		{"idle for exactly the threshold", idleVerdict{Idle: &yes, IdleForS: 1800}, true},
		{"no idle field at all", idleVerdict{IdleForS: 3600}, false},
		{"idle false", idleVerdict{Idle: &no, IdleForS: 3600}, false},
		{"idle true but a signal is busy", idleVerdict{Idle: &yes, IdleForS: 3600, BusySignals: []string{"tasks"}}, false},
		{"idle against a shorter threshold than the keeper's", idleVerdict{Idle: &yes, IdleForS: 600}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.v.sleepable(thirty); got != c.want {
				t.Fatalf("sleepable = %v, want %v", got, c.want)
			}
		})
	}
}

// The whole condition list. Every row but the first must leave the workspace
// awake, and the cheap refusals must not even ask the workspace.
func TestSleepOnceDecisionTable(t *testing.T) {
	cases := []struct {
		name           string
		replicas       int32
		ready          int32
		owners         []string
		sleepOff       bool
		queued         bool
		failGet        int
		emptySecret    bool
		code           int
		body           string
		wantSleep      bool
		wantNoPoll     bool // the workspace must not have been asked at all
		wantStateAfter string
	}{
		{name: "awake and idle twice: sleeps", replicas: 1, ready: 1, wantSleep: true, wantStateAfter: StateAsleep},
		{name: "keeper.sleep.enabled is off", replicas: 1, ready: 1, sleepOff: true, wantNoPoll: true, wantStateAfter: StateAwake},
		{name: "busy with a task", replicas: 1, ready: 1, body: answer(false, 0, "tasks")},
		{name: "busy with a terminal and an editor", replicas: 1, ready: 1, body: answer(false, 0, "terminals", "code_server")},
		{name: "quiet, but not for long enough yet", replicas: 1, ready: 1, body: answer(false, 120)},
		{name: "contradictory: idle but a signal busy", replicas: 1, ready: 1, body: answer(true, 3600, "dashboard")},
		{name: "idle against a shorter threshold", replicas: 1, ready: 1, body: answer(true, 600)},
		{name: "a request is queued", replicas: 1, ready: 1, queued: true, wantNoPoll: true},
		{name: "already asleep", owners: []string{FieldManager}, wantNoPoll: true, wantStateAfter: StateAsleep},
		{name: "stopped by an admin", wantNoPoll: true, wantStateAfter: StateStopped},
		{name: "starting", replicas: 1, ready: 0, wantNoPoll: true},
		{name: "the API cannot be read", replicas: 1, ready: 1, failGet: http.StatusInternalServerError, wantNoPoll: true},
		{name: "no shared secret", replicas: 1, ready: 1, emptySecret: true, wantNoPoll: true},
		{name: "the poll is refused (401)", replicas: 1, ready: 1, code: http.StatusUnauthorized},
		{name: "the poll is forbidden (403)", replicas: 1, ready: 1, code: http.StatusForbidden},
		{name: "no idle endpoint (404)", replicas: 1, ready: 1, code: http.StatusNotFound},
		{name: "the workspace errors (500)", replicas: 1, ready: 1, code: http.StatusInternalServerError},
		{name: "a 200 that is not JSON", replicas: 1, ready: 1, body: "<html>restarting</html>"},
		{name: "JSON without the idle field", replicas: 1, ready: 1, body: `{"busy_signals":[],"idle_for_s":3600}`},
		{name: "idle that is not a boolean", replicas: 1, ready: 1, body: `{"idle":"yes","idle_for_s":3600}`},
		{name: "a 302 to somewhere else", replicas: 1, ready: 1, code: http.StatusFound},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, c.replicas, c.ready, c.owners...)
			s, fi := newIdleServer(t, f, k)
			fi.set(func(fi *fakeIdle) { fi.code, fi.body = c.code, c.body })
			f.set(func(f *fakeAPI) { f.failGet = c.failGet })
			s.SleepEnabled = !c.sleepOff
			if c.queued {
				queueOne(t, s)
			}
			if c.emptySecret {
				if err := os.WriteFile(s.ControlSecretFile, []byte("\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			_ = captureLog(t)
			rounds(s, 3)

			if got := sleeps(f); (got > 0) != c.wantSleep {
				t.Fatalf("scale-to-0 writes = %d, want sleep = %v", got, c.wantSleep)
			}
			if c.wantNoPoll && fi.polls() != 0 {
				t.Fatalf("asked the workspace %d times; this refusal must not need to", fi.polls())
			}
			if c.wantStateAfter != "" && c.failGet == 0 {
				if got := stateOf(t, k); got != c.wantStateAfter {
					t.Fatalf("state after = %s, want %s", got, c.wantStateAfter)
				}
			}
		})
	}
}

func TestOneIdleAnswerIsNotEnough(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	rounds(s, 1)
	if sleeps(f) != 0 {
		t.Fatal("slept on a single idle answer")
	}
	rounds(s, 1)
	if sleeps(f) != 1 {
		t.Fatalf("scale-to-0 writes = %d after two idle answers, want 1", sleeps(f))
	}
}

// Any answer that is not "idle" resets the count, errors included — otherwise
// two idle answers an hour apart, with a busy afternoon between them, would add
// up to a sleep.
func TestAnyOtherOutcomeResetsTheCount(t *testing.T) {
	interruptions := []struct {
		name string
		do   func(t *testing.T, s *Server, f *fakeAPI, fi *fakeIdle) (undo func())
	}{
		{"a busy answer", func(_ *testing.T, _ *Server, _ *fakeAPI, fi *fakeIdle) func() {
			fi.set(func(fi *fakeIdle) { fi.body = answer(false, 0, "tasks") })
			return func() { fi.set(func(fi *fakeIdle) { fi.body = "" }) }
		}},
		{"a failed poll", func(_ *testing.T, _ *Server, _ *fakeAPI, fi *fakeIdle) func() {
			fi.set(func(fi *fakeIdle) { fi.code = http.StatusInternalServerError })
			return func() { fi.set(func(fi *fakeIdle) { fi.code = 0 }) }
		}},
		{"an API error", func(_ *testing.T, _ *Server, f *fakeAPI, _ *fakeIdle) func() {
			f.set(func(f *fakeAPI) { f.failGet = http.StatusServiceUnavailable })
			return func() { f.set(func(f *fakeAPI) { f.failGet = 0 }) }
		}},
		{"a queued request", func(t *testing.T, s *Server, _ *fakeAPI, _ *fakeIdle) func() {
			token := queueOne(t, s)
			// Delivered the way the drain loop does it: claimed, then done.
			return func() {
				if err := s.Queue.ClaimToken(token); err != nil {
					t.Fatal(err)
				}
				s.Queue.Done(token)
			}
		}},
	}
	for _, c := range interruptions {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 1, 1)
			s, fi := newIdleServer(t, f, k)
			_ = captureLog(t)
			rounds(s, 1) // idle
			undo := c.do(t, s, f, fi)
			rounds(s, 1) // interrupted
			undo()
			rounds(s, 1) // idle again: the first of a new pair
			if sleeps(f) != 0 {
				t.Fatal("slept although the two idle answers were not consecutive")
			}
			rounds(s, 1)
			if sleeps(f) != 1 {
				t.Fatalf("scale-to-0 writes = %d, want 1 once two idle answers follow each other", sleeps(f))
			}
		})
	}
}

// The write is step 3's: the keeper's apply on /scale, forced, carrying the
// resourceVersion of the read it decided on.
func TestTheSleepIsTheKeepersApply(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	rounds(s, 2)
	f.mu.Lock()
	scales := append([]scaleCall(nil), f.scales...)
	f.mu.Unlock()
	if len(scales) != 1 {
		t.Fatalf("scale writes = %+v, want exactly one", scales)
	}
	c := scales[0]
	if c.Replicas != 0 || c.RV != "100" || c.ContentType != "application/apply-patch+yaml" ||
		c.Query != "fieldManager=kube-coder-keeper&force=true" {
		t.Fatalf("scale = %+v; want an apply of 0 under the keeper at resourceVersion 100", c)
	}
	if got := stateOf(t, k); got != StateAsleep {
		t.Fatalf("after the sleep the workspace is %s, want asleep — not stopped", got)
	}
}

// ── what the poll sends ─────────────────────────────────────────────────────

func TestThePollProvesItIsTheKeeper(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, fi := newIdleServer(t, f, k)
	s.IdleMinutes = 7
	rounds(s, 1)
	fi.mu.Lock()
	defer fi.mu.Unlock()
	if len(fi.got) != 1 {
		t.Fatalf("polls = %d, want 1", len(fi.got))
	}
	if got := fi.got[0].Get("X-Keeper-Secret"); got != testControlSecret {
		t.Fatalf("X-Keeper-Secret = %q, want the shared secret with its newline trimmed", got)
	}
	if got := fi.got[0].Get("Authorization"); got != "" {
		t.Fatalf("Authorization = %q; the shared secret must never be sent as a bearer", got)
	}
	if fi.paths[0] != "/api/keeper/idle" || fi.query[0].Get("idle_minutes") != "7" {
		t.Fatalf("poll = %s?%v; want /api/keeper/idle with the keeper's own threshold", fi.paths[0], fi.query[0])
	}
}

func TestTheSecretIsNeverLogged(t *testing.T) {
	answers := []struct {
		code int
		body string
	}{
		{http.StatusUnauthorized, ""}, {http.StatusNotFound, ""}, {http.StatusInternalServerError, ""},
		{0, "not json"}, {0, answer(false, 0, "tasks")}, {0, ""}, {0, ""},
	}
	f, k := newFake(t, 1, 1)
	s, fi := newIdleServer(t, f, k)
	buf := captureLog(t)
	for _, a := range answers {
		fi.set(func(fi *fakeIdle) { fi.code, fi.body = a.code, a.body })
		rounds(s, 1)
	}
	if strings.Contains(buf.String(), testControlSecret) {
		t.Fatalf("the shared secret appears in the log:\n%s", buf.String())
	}
}

// ── what it says ────────────────────────────────────────────────────────────

func TestARefusalSaysWhatToFix(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, fi := newIdleServer(t, f, k)
	fi.set(func(fi *fakeIdle) { fi.code = http.StatusUnauthorized })
	buf := captureLog(t)
	rounds(s, 1)
	got := buf.String()
	if !strings.Contains(got, "refused the keeper's credentials (401)") || !strings.Contains(got, "keeper.controlSecret") {
		t.Fatalf("log = %q; a refusal must say that it will never sleep and what to check", got)
	}
}

func TestAMissingEndpointSaysTheImageIsOld(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, fi := newIdleServer(t, f, k)
	fi.set(func(fi *fakeIdle) { fi.code = http.StatusNotFound })
	buf := captureLog(t)
	rounds(s, 1)
	if !strings.Contains(buf.String(), "older than the keeper's sleep support") {
		t.Fatalf("log = %q", buf.String())
	}
}

// A busy workspace answers busy every minute for hours. One line per change of
// reason, not one per round.
func TestAReasonIsLoggedOnceNotEveryRound(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, fi := newIdleServer(t, f, k)
	fi.set(func(fi *fakeIdle) { fi.body = answer(false, 0, "tasks") })
	buf := captureLog(t)
	rounds(s, 5)
	if n := strings.Count(buf.String(), "is not idle"); n != 1 {
		t.Fatalf("%d 'is not idle' lines for five identical rounds, want 1:\n%s", n, buf.String())
	}
	fi.set(func(fi *fakeIdle) { fi.body = answer(false, 0, "terminals") })
	rounds(s, 3)
	if n := strings.Count(buf.String(), "is not idle"); n != 2 {
		t.Fatalf("%d lines after the reason changed, want 2", n)
	}
	// And an unchanged reason is repeated once it is an hour old.
	s.idle.lastAt = time.Now().Add(-idleRepeat - time.Second)
	rounds(s, 1)
	if n := strings.Count(buf.String(), "is not idle"); n != 3 {
		t.Fatalf("%d lines after an hour, want 3", n)
	}
}

func TestTheAuditLineSaysWhyAndAgainstWhat(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	buf := captureLog(t)
	rounds(s, 2)
	want := "idle: ws-ns/ws-alice scaled 1 -> 0 after 1h0m0s quiet (threshold 30m0s, signals all clear)"
	if !strings.Contains(buf.String(), want) {
		t.Fatalf("log = %q; want %q", buf.String(), want)
	}
}

// ── the window between asking and acting ───────────────────────────────────

// Each of these happens while the second poll is being answered, so the
// pre-checks all passed and only the re-check under the wake lock can see it.
func TestTheReCheckSeesWhatHappenedDuringThePoll(t *testing.T) {
	cases := []struct {
		name      string
		during    func(t *testing.T, s *Server, f *fakeAPI)
		wantState string
	}{
		{"an admin stopped it", func(_ *testing.T, _ *Server, f *fakeAPI) {
			f.set(func(f *fakeAPI) { f.adminScale(0) })
		}, StateStopped},
		{"the controller stopped it", func(_ *testing.T, _ *Server, f *fakeAPI) {
			f.set(func(f *fakeAPI) { f.controllerApply(0) })
		}, StateStopped},
		{"the pod restarted", func(_ *testing.T, _ *Server, f *fakeAPI) {
			f.set(func(f *fakeAPI) { f.ready = 0 })
		}, StateStarting},
		{"a webhook was queued", func(t *testing.T, s *Server, _ *fakeAPI) {
			queueOne(t, s)
		}, StateAwake},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 1, 1)
			s, fi := newIdleServer(t, f, k)
			_ = captureLog(t)
			rounds(s, 1)
			fi.set(func(fi *fakeIdle) { fi.during = func() { c.during(t, s, f) } })
			rounds(s, 1)
			if n := sleeps(f); n != 0 {
				t.Fatalf("the keeper wrote replicas 0 %d time(s); the re-check should have stopped it", n)
			}
			if got := stateOf(t, k); got != c.wantState {
				t.Fatalf("state = %s, want %s", got, c.wantState)
			}
		})
	}
}

// A write that loses to someone else's is not an error worth a log line: the
// next round decides again from what is there now.
func TestAConflictSkipsTheRoundQuietly(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	var bump func(f *fakeAPI)
	bump = func(f *fakeAPI) { f.rv++; f.beforeScale = bump } // a writer that always lands first
	f.set(func(f *fakeAPI) { f.beforeScale = bump })
	buf := captureLog(t)
	rounds(s, 2)
	if sleeps(f) == 0 || stateOf(t, k) != StateAwake {
		t.Fatalf("expected refused writes and a workspace still awake; state %s", stateOf(t, k))
	}
	if strings.Contains(buf.String(), "cannot scale") {
		t.Fatalf("a conflict was logged as a failure: %q", buf.String())
	}
	f.set(func(f *fakeAPI) { f.beforeScale = nil })
	rounds(s, 2)
	if stateOf(t, k) != StateAsleep {
		t.Fatal("once nobody else was writing, two idle answers should have put it to sleep")
	}
}

// Wakes and sleeps take the same lock, so however they are interleaved each
// one's read-then-write is whole and none of them loses a race to another.
func TestSleepsAndWakesNeverInterleave(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	_ = captureLog(t)
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx := context.Background()
			if i%2 == 0 {
				_, _ = s.sleepNow(ctx, "for the test")
				return
			}
			_, _, _ = s.wakeNow(ctx, "client=test", reasonVisit)
			// Woken is "starting"; let the fake's pod come up so the next
			// sleep has an awake workspace to consider.
			f.set(func(f *fakeAPI) { f.ready = 1 })
		}()
	}
	wg.Wait()
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.conflicts != 0 {
		t.Fatalf("%d scale writes lost a race; wakes and sleeps must be serialised", f.conflicts)
	}
	if len(f.scales) == 0 {
		t.Fatal("nothing was written at all; the test proved nothing")
	}
}

func TestIdleLoopStopsWhenCancelled(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	_ = captureLog(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { s.IdleLoop(ctx); close(done) }()
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("IdleLoop did not return after its context was cancelled")
	}
}

func TestAPollThatTimesOutDoesNotSleep(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, fi := newIdleServer(t, f, k)
	fi.set(func(fi *fakeIdle) { fi.delay = 5 * time.Second })
	buf := captureLog(t)
	for i := 0; i < 2; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
		s.sleepOnce(ctx)
		cancel()
	}
	if sleeps(f) != 0 {
		t.Fatal("slept although the workspace never answered")
	}
	if !strings.Contains(buf.String(), "did not answer within") {
		t.Fatalf("log = %q; want the timeout named", buf.String())
	}
}

// ── what the status JSON says ───────────────────────────────────────────────

// After a sleep the cache already says asleep, so the next page poll costs no
// API read — the same courtesy wakeNow extends.
func TestASleepRefreshesTheCache(t *testing.T) {
	f, k := newFake(t, 1, 1)
	s, _ := newIdleServer(t, f, k)
	_ = captureLog(t)
	rounds(s, 2)
	f.mu.Lock()
	before := f.gets
	f.mu.Unlock()
	if got := decode(t, do(s.WakeHandler(), http.MethodGet, "/api/keeper/status", nil)); got.State != StateAsleep {
		t.Fatalf("status = %+v right after a sleep", got)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.gets != before {
		t.Fatalf("the status poll read the API %d time(s); the sleep should have left the answer cached", f.gets-before)
	}
}

// The keeper's half of closing the 0 -> 0 gap (#728 step 5): the controller's
// Stop on a sleeping workspace is an apply under its own manager, which records
// it as an owner of spec.replicas even though the value does not change. It
// shares the field rather than taking it, and a shared field is not the
// keeper's alone, so the workspace is stopped and nothing the keeper does
// undoes it — until a Start, which is a `kubectl scale` again.
func TestAControllerStopOnASleepingWorkspaceSticks(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	ctx := context.Background()
	f.set(func(f *fakeAPI) { f.controllerApply(0) })
	f.mu.Lock()
	owners := slices.Clone(f.owners)
	f.mu.Unlock()
	if !slices.Equal(owners, []string{FieldManager, "kube-coder-controller"}) {
		t.Fatalf("owners after the Stop = %v; want the keeper and the controller sharing the field", owners)
	}
	if got := stateOf(t, k); got != StateStopped {
		t.Fatalf("after the controller's Stop: %s, want stopped", got)
	}
	if state, woke, err := Wake(ctx, k); err != nil || woke || state != StateStopped {
		t.Fatalf("Wake = %s, %v, %v; a visit must not undo a controller Stop", state, woke, err)
	}
	if _, err := Sleep(ctx, k); !errors.Is(err, errNotOurs) {
		t.Fatalf("Sleep = %v; want errNotOurs for a workspace the controller stopped", err)
	}
	f.set(func(f *fakeAPI) { f.adminScale(1); f.ready = 1 })
	if got := stateOf(t, k); got != StateAwake {
		t.Fatalf("after the controller's Start: %s, want awake", got)
	}
	if _, err := Sleep(ctx, k); err != nil {
		t.Fatal(err)
	}
	if got := stateOf(t, k); got != StateAsleep {
		t.Fatalf("after the keeper slept the started workspace: %s, want asleep — its forced apply takes the field back", got)
	}
}

func TestIdleMinutesFromEnv(t *testing.T) {
	_ = captureLog(t)
	for in, want := range map[string]int{"": 30, "5": 5, "0": 30, "-3": 30, "soon": 30, "90": 90} {
		t.Setenv("KEEPER_IDLE_MINUTES", in)
		if got := idleMinutesFromEnv(); got != want {
			t.Errorf("KEEPER_IDLE_MINUTES=%q -> %d, want %d", in, got, want)
		}
	}
}
