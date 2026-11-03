// The wake budget (#728 step 4b).
//
// Step 4b is the first time a request to a public URL from someone who is not
// signed in can start a 4 GiB pod. The trigger-id check in store.go is what
// stops a stranger doing it at all; this is what bounds the cost when the
// request is genuine — a repository whose CI fires a webhook every few seconds,
// a cron set to * * * * *, a misconfigured sender retrying in a loop.
//
// Exhausting the budget does not discard anything. The request is queued and
// answered 202, and it drains at the next wake from any cause: a person
// visiting, the app's Wake button, or the budget refilling. GitHub does not
// retry a failed delivery, so dropping would lose the event for good, while
// holding it only delays it.
//
// The budget is on disk for one specific reason: a keeper that crashlooped
// would otherwise come back with a full budget every few seconds, which turns
// a crash into an unlimited-wake primitive.
package main

import (
	"log"
	"sync"
	"time"
)

// WakeLimiter caps wakes caused by unattended callers inside a rolling window.
//
// It deliberately does not govern the two human paths. A person who is signed
// in, or who holds this workspace's own API token and presses Wake up, is not
// who this protects against, and a Wake button that silently did nothing would
// be a worse failure than an extra pod start.
type WakeLimiter struct {
	// Max wakes per window. Zero means unlimited, which is both the value a
	// chart without the setting renders and the behaviour every step-3 test
	// expects.
	Max    int
	Window time.Duration
	// Now is injectable so the tests can cross an hour boundary without
	// sleeping through one.
	Now func() time.Time

	store *Store

	mu    sync.Mutex
	wakes []int64 // Unix seconds, oldest first
}

// NewWakeLimiter reads back whatever budget a previous keeper had spent.
func NewWakeLimiter(max int, store *Store) *WakeLimiter {
	l := &WakeLimiter{Max: max, Window: time.Hour, Now: time.Now, store: store}
	if store != nil {
		l.wakes = store.Wakes()
	}
	if max > 0 {
		log.Printf("wake budget: %d per hour for webhooks and crons; %d already spent in the last hour", max, l.spent())
	}
	return l
}

// Allow reports whether an unattended wake may happen now, and books it if so.
//
// Booking and asking are one call on purpose. Two callers that each asked and
// then booked could both be told yes for the last token in the budget.
func (l *WakeLimiter) Allow() bool {
	if l == nil || l.Max <= 0 {
		return true
	}
	now := l.now()
	l.mu.Lock()
	l.prune(now)
	if len(l.wakes) >= l.Max {
		spent := len(l.wakes)
		oldest := time.Unix(l.wakes[0], 0)
		l.mu.Unlock()
		log.Printf("wake budget: %d/%d spent in the last hour; not waking for this request. It is queued and will be delivered at the next wake, or when the budget frees up in %s.",
			spent, l.Max, time.Until(oldest.Add(l.Window)).Round(time.Second))
		return false
	}
	l.wakes = append(l.wakes, now.Unix())
	snapshot := append([]int64(nil), l.wakes...)
	l.mu.Unlock()
	// Persisted outside the lock, and a failure allows the wake rather than
	// refusing it: an unwritable volume is already reported loudly elsewhere,
	// and the in-memory list still bounds this keeper's own lifetime. Refusing
	// every wake because a disk filled up would break the feature in the
	// direction nobody asked for.
	if l.store != nil {
		if err := l.store.SetWakes(snapshot); err != nil {
			log.Printf("wake budget: could not record this wake, so a restart would forget it: %v", err)
		}
	}
	return true
}

// Remaining is how many unattended wakes are left in the window; -1 when there
// is no cap. For the log and for tests, not for a decision — a decision must go
// through Allow, which books what it grants.
func (l *WakeLimiter) Remaining() int {
	if l == nil || l.Max <= 0 {
		return -1
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.prune(l.now())
	if n := l.Max - len(l.wakes); n > 0 {
		return n
	}
	return 0
}

func (l *WakeLimiter) spent() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.prune(l.now())
	return len(l.wakes)
}

// prune drops wakes that have fallen out of the window. Callers hold l.mu.
//
// A rolling window rather than a fixed hour: with fixed buckets a sender can
// spend a whole budget at 10:59 and another at 11:00, so the cap is really
// twice what it says.
func (l *WakeLimiter) prune(now time.Time) {
	cut := now.Add(-l.Window).Unix()
	keep := l.wakes[:0:0] // a fresh slice, so the trimmed entries are released
	for _, w := range l.wakes {
		// A wake stamped in the future is a clock that went backwards. Keeping
		// it is the conservative reading: it expires on its own, and the
		// alternative is a budget that resets whenever NTP steps the clock.
		if w > cut {
			keep = append(keep, w)
		}
	}
	l.wakes = keep
}

func (l *WakeLimiter) now() time.Time {
	if l.Now != nil {
		return l.Now()
	}
	return time.Now()
}
