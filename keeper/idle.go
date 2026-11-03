package main

// The idle loop (#728 step 5): the keeper puts the workspace to sleep by itself.
//
// The split of work is the whole design. The workspace observes — only it can
// see its tasks, its Board runs and its own sockets, and keeper_idle.py turns
// those into a verdict with every ambiguity resolved to "busy". The keeper
// decides — it holds the policy (keeper.sleep.idleMinutes) and the one write
// that scales the Deployment. So the countdown is not a timer here: it is the
// workspace's quiet_since, which is why a keeper that restarts in the middle of
// one asks again and gets the same answer, with nothing to persist.
//
// Fail-safe is "stay awake". Any error, timeout, refusal, malformed answer or
// state other than awake means "not this round", and so does a single idle
// answer: three of the five signals are instantaneous socket counts that carry
// no timestamp, so one sample can miss a live-but-quiet tab. Two answers a poll
// apart have to agree before anything is scaled.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	// idlePollInterval is how often the keeper asks: a thirtieth of the default
	// threshold, so sleep lands within a minute of it, and one Deployment read
	// plus one small GET a minute costs next to nothing.
	idlePollInterval = 60 * time.Second
	// idleTimeout bounds one poll. A workspace too loaded to answer in this
	// long is not one to put to sleep.
	idleTimeout = 10 * time.Second
	// idleConfirmations is how many idle answers in a row it takes.
	idleConfirmations = 2
	// idleMaxBody caps how much of the answer is read; the real one is ~1 KiB.
	idleMaxBody = 64 << 10
	// idleRepeat is how often an unchanged reason for not sleeping is logged
	// again, so a refusal that lasts all week still shows in today's log.
	idleRepeat = time.Hour
	// keeperSecretHeader is how the poll proves it is the keeper. It must match
	// KEEPER_SECRET_HEADER in charts/workspace/handlers/system.py, and it is
	// deliberately not Authorization: the shared secret is not an API token
	// and must never be accepted as one.
	keeperSecretHeader = "X-Keeper-Secret"
)

// idleVerdict is the part of GET /api/keeper/idle's answer the keeper acts on.
// Idle is a pointer so a body without the field is a malformed answer, not a
// "false" that happens to be safe by accident.
type idleVerdict struct {
	Idle        *bool    `json:"idle"`
	IdleForS    float64  `json:"idle_for_s"`
	BusySignals []string `json:"busy_signals"`
	Reason      string   `json:"reason"`
}

// sleepable is the keeper's reading of the answer. It checks the observation
// against the keeper's own threshold rather than trusting the verdict alone,
// so a workspace that judged against a shorter threshold than the keeper was
// configured with cannot talk it into an early sleep.
func (v idleVerdict) sleepable(threshold time.Duration) bool {
	return v.Idle != nil && *v.Idle && len(v.BusySignals) == 0 &&
		v.IdleForS >= threshold.Seconds()
}

// idleLoopState belongs to the IdleLoop goroutine alone, so it needs no lock.
type idleLoopState struct {
	confirmed int       // idle answers in a row
	lastKey   string    // why the last round did not sleep, for the log
	lastAt    time.Time // when that reason was last logged
}

// IdleLoop asks the workspace whether it is idle every idlePollInterval, until
// ctx is cancelled. serve() starts it only with keeper.sleep.enabled, because
// without that the chart still declares replicas and the next upgrade would
// wake the workspace again.
func (s *Server) IdleLoop(ctx context.Context) {
	log.Printf("idle: %s/%s sleeps after %s with nobody using it; asking %s every %s",
		s.Kube.Namespace, s.Kube.Name, s.idleThreshold(), s.workspaceURL(), idlePollInterval)
	t := time.NewTicker(idlePollInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.sleepOnce(ctx)
		}
	}
}

// sleepOnce is one round: every condition for sleeping, cheapest first, so the
// usual "nothing to do" costs no I/O at all.
func (s *Server) sleepOnce(ctx context.Context) {
	// serve() only starts the loop with sleep on; checked here as well so the
	// rule lives where the decision does, and a test can pin it.
	if !s.SleepEnabled {
		return
	}
	// A request waiting to be replayed, or being delivered right now. Len
	// counts both (queue.go), and the one forward that has no entry happens
	// only when the queue is already full, so zero here also means no delivery
	// is in flight. Silent: the drain loop already logs its backlog.
	if s.Queue.Len() > 0 {
		s.notThisRound("queue", "")
		return
	}
	state, err := s.freshState(ctx)
	if err != nil {
		s.notThisRound("state-error", "idle: cannot read %s/%s, not sleeping this round: %v",
			s.Kube.Namespace, s.Kube.Name, err)
		return
	}
	if state != StateAwake {
		// Asleep or stopped: nothing to do. Starting: not settled yet, and its
		// quiet_since is about to move anyway.
		s.notThisRound("state-"+state, "")
		return
	}
	v, err := s.askIdle(ctx)
	if err != nil {
		key, _, _ := strings.Cut(err.Error(), ":")
		s.notThisRound("ask-"+key, "idle: %v", err)
		return
	}
	threshold := s.idleThreshold()
	if !v.sleepable(threshold) {
		key := "quiet"
		if len(v.BusySignals) > 0 {
			key = "busy-" + strings.Join(v.BusySignals, ",")
		}
		s.notThisRound(key, "idle: %s/%s is not idle (%s); not sleeping", s.Kube.Namespace, s.Kube.Name, v.Reason)
		return
	}
	s.idle.confirmed++
	if s.idle.confirmed < idleConfirmations {
		return
	}
	s.idle.confirmed = 0
	why := fmt.Sprintf("after %s quiet (threshold %s, signals all clear)", seconds(v.IdleForS), threshold)
	slept, err := s.sleepNow(ctx, why)
	switch {
	case errors.Is(err, ErrConflict):
		// Someone else wrote in between — an admin, a helm upgrade. Re-decide
		// from scratch next round; there is nothing wrong to report.
	case errors.Is(err, errNotOurs):
		log.Printf("idle: %s/%s is stopped by an admin; not sleeping it", s.Kube.Namespace, s.Kube.Name)
	case err != nil:
		log.Printf("idle: cannot scale %s/%s to 0: %v", s.Kube.Namespace, s.Kube.Name, err)
	case slept:
		s.idle.lastKey = ""
	}
}

// notThisRound resets the confirmations and logs why — but only when the
// reason changes, or once an hour while it does not, so a busy workspace does
// not write a line a minute. An empty format logs nothing.
func (s *Server) notThisRound(key, format string, args ...any) {
	s.idle.confirmed = 0
	if format == "" {
		s.idle.lastKey = key
		return
	}
	if key == s.idle.lastKey && time.Since(s.idle.lastAt) < idleRepeat {
		return
	}
	s.idle.lastKey, s.idle.lastAt = key, time.Now()
	log.Printf(format, args...)
}

// askIdle is one GET /api/keeper/idle, straight to the workspace's Service —
// never through the ingress, or the poll would look like a browser and blind
// the dashboard signal (docs/always-on-keeper.md §4.1, rule 3).
//
// Every way this can fail is an error, and each error's text starts with a
// short word and a colon, which sleepOnce uses to tell one reason from another
// when deciding whether to log it again.
func (s *Server) askIdle(ctx context.Context) (idleVerdict, error) {
	var v idleVerdict
	secret, err := s.controlSecret()
	if err != nil {
		return v, fmt.Errorf("secret: cannot ask whether the workspace is idle: %w", err)
	}
	if secret == "" {
		return v, errors.New("secret: the shared secret is empty, so the keeper cannot prove itself to the workspace; it will not sleep until keeper.controlSecret is fixed")
	}
	url := s.workspaceURL() + "/api/keeper/idle?idle_minutes=" + strconv.Itoa(s.IdleMinutes)
	ctx, cancel := context.WithTimeout(ctx, idleTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return v, fmt.Errorf("request: %w", err)
	}
	req.Header.Set(keeperSecretHeader, secret)
	req.Header.Set("Accept", "application/json")
	resp, err := s.client().Do(req)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return v, fmt.Errorf("timeout: %s did not answer within %s, not sleeping this round", s.workspaceURL(), idleTimeout)
		}
		return v, fmt.Errorf("unreachable: cannot reach %s, not sleeping this round: %w", s.workspaceURL(), err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, idleMaxBody))
	if err != nil {
		return v, fmt.Errorf("read: the answer from %s ended early, not sleeping this round: %w", s.workspaceURL(), err)
	}
	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return v, fmt.Errorf("refused: %s refused the keeper's credentials (%d); the workspace will never be put to sleep until this is fixed — is keeper.controlSecret the same Secret on both pods?", s.workspaceURL(), resp.StatusCode)
	case resp.StatusCode == http.StatusNotFound:
		return v, fmt.Errorf("missing: %s has no /api/keeper/idle (404); this workspace image is older than the keeper's sleep support", s.workspaceURL())
	case resp.StatusCode != http.StatusOK:
		return v, fmt.Errorf("status: %s answered %d, not sleeping this round", s.workspaceURL(), resp.StatusCode)
	}
	if err := json.Unmarshal(body, &v); err != nil || v.Idle == nil {
		if err == nil {
			err = errors.New(`no "idle" field`)
		}
		return idleVerdict{}, fmt.Errorf("malformed: %s answered something that is not an idle verdict, not sleeping this round: %v", s.workspaceURL(), err)
	}
	return v, nil
}

// idleThreshold is keeper.sleep.idleMinutes as a duration.
func (s *Server) idleThreshold() time.Duration {
	return time.Duration(s.IdleMinutes) * time.Minute
}

// seconds renders a float number of seconds for the audit line.
func seconds(s float64) time.Duration {
	return (time.Duration(s) * time.Second).Round(time.Second)
}
