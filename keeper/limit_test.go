package main

import (
	"path/filepath"
	"testing"
	"time"
)

// clock is an injectable now, so an hour boundary costs no hour.
type clock struct{ t time.Time }

func (c *clock) now() time.Time      { return c.t }
func (c *clock) add(d time.Duration) { c.t = c.t.Add(d) }

func newLimiter(t *testing.T, max int) (*WakeLimiter, *Store, *clock) {
	t.Helper()
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	c := &clock{t: time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)}
	l := NewWakeLimiter(max, store)
	l.Now = c.now
	return l, store, c
}

// Zero means unlimited, and it has to: it is what a chart without the setting
// renders, and every step-3 test expects a wake to happen whenever it is asked
// for.
func TestWakeLimiterZeroIsUnlimited(t *testing.T) {
	l, _, _ := newLimiter(t, 0)
	for i := 0; i < 100; i++ {
		if !l.Allow() {
			t.Fatalf("refused wake %d with no cap set", i)
		}
	}
	if l.Remaining() != -1 {
		t.Fatalf("remaining = %d; want -1 for unlimited", l.Remaining())
	}
}

// A nil limiter is the same thing, because step 3's Server has no limiter at
// all and its handlers must keep working.
func TestWakeLimiterNilAllows(t *testing.T) {
	var l *WakeLimiter
	if !l.Allow() {
		t.Fatal("a keeper with no limiter refused a wake")
	}
}

func TestWakeLimiterCapsTheTenthAndEleventh(t *testing.T) {
	l, _, _ := newLimiter(t, 10)
	for i := 1; i <= 10; i++ {
		if !l.Allow() {
			t.Fatalf("wake %d refused inside a cap of 10", i)
		}
	}
	if l.Allow() {
		t.Fatal("the 11th wake in the hour was allowed")
	}
	if l.Remaining() != 0 {
		t.Fatalf("remaining = %d; want 0", l.Remaining())
	}
}

// A rolling window, not a fixed hour: with fixed buckets a sender spends a
// whole budget at 10:59 and another at 11:00, so the cap is really twice what
// it says.
func TestWakeLimiterWindowRolls(t *testing.T) {
	l, _, c := newLimiter(t, 2)
	if !l.Allow() || !l.Allow() {
		t.Fatal("the first two were refused")
	}
	c.add(59 * time.Minute)
	if l.Allow() {
		t.Fatal("still inside the hour, so this must be refused")
	}
	c.add(2 * time.Minute) // the first two are now 61 minutes old
	if !l.Allow() {
		t.Fatal("the window did not roll")
	}
}

// The reason the budget is on disk. A keeper that crashlooped would otherwise
// come back with a full budget every few seconds, which turns a crash into an
// unlimited-wake primitive.
func TestWakeLimiterBudgetSurvivesARestart(t *testing.T) {
	dir := t.TempDir()
	store, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	l := NewWakeLimiter(3, store)
	for i := 0; i < 3; i++ {
		if !l.Allow() {
			t.Fatalf("wake %d refused", i)
		}
	}
	if l.Allow() {
		t.Fatal("over the cap")
	}
	// The pod dies and comes back: a new Store over the same volume, which is
	// exactly what main.go does.
	again, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	l2 := NewWakeLimiter(3, again)
	if l2.Remaining() != 0 {
		t.Fatalf("remaining after a restart = %d; want 0 — a crashloop must not refill the budget", l2.Remaining())
	}
	if l2.Allow() {
		t.Fatal("a restart refilled the wake budget")
	}
}

// An hour after the crash, the restarted keeper is free again — the budget is
// persisted, not frozen.
func TestWakeLimiterPersistedBudgetStillExpires(t *testing.T) {
	dir := t.TempDir()
	store, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	l := NewWakeLimiter(1, store)
	if !l.Allow() {
		t.Fatal("first wake refused")
	}
	again, _ := NewStore(dir)
	l2 := NewWakeLimiter(1, again)
	c := &clock{t: time.Now().Add(61 * time.Minute)}
	l2.Now = c.now
	if !l2.Allow() {
		t.Fatal("an hour later the budget should have freed up")
	}
}

// A clock that steps backwards must not reset the budget: keeping a wake
// stamped in the future is the conservative reading, and it expires on its own.
func TestWakeLimiterIgnoresAClockGoingBackwards(t *testing.T) {
	l, _, c := newLimiter(t, 1)
	if !l.Allow() {
		t.Fatal("first wake refused")
	}
	c.add(-24 * time.Hour)
	if l.Allow() {
		t.Fatal("a backwards clock step refilled the budget")
	}
}

// Asking and booking are one call, so two callers racing for the last token do
// not both get it.
func TestWakeLimiterGrantsEachTokenOnce(t *testing.T) {
	l, _, _ := newLimiter(t, 5)
	granted := 0
	done := make(chan bool, 20)
	for i := 0; i < 20; i++ {
		go func() { done <- l.Allow() }()
	}
	for i := 0; i < 20; i++ {
		if <-done {
			granted++
		}
	}
	if granted != 5 {
		t.Fatalf("granted %d tokens from a budget of 5", granted)
	}
}

// An unwritable volume must not turn into "refuse every wake": the in-memory
// list still bounds this keeper's lifetime, and breaking the feature because a
// disk filled up is the wrong direction to fail in.
func TestWakeLimiterAllowsWhenItCannotRecord(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	// Point the store at a path that cannot be written, the way a full or
	// read-only volume would.
	store.path = filepath.Join(store.path, "not-a-directory", "state.json")
	l := NewWakeLimiter(2, store)
	buf := captureLog(t)
	if !l.Allow() {
		t.Fatal("a wake was refused because the budget could not be recorded")
	}
	if buf.String() == "" {
		t.Fatal("a budget that cannot be recorded must say so")
	}
	// Still bounded in memory.
	if !l.Allow() || l.Allow() {
		t.Fatal("the in-memory cap stopped applying")
	}
}
