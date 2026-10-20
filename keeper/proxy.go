// The front door for webhooks, crons and page-watches (#728 step 4b).
//
// Before this, a GitHub delivery that arrived while the workspace slept hit an
// ingress with no pod behind it and got a raw nginx 503. GitHub does not retry,
// so the task never ran and nothing recorded that anything was asked for. A
// cron fire was worse: the CronJob reported success because `curl -f` against
// nginx's 503 is a failure the Job retried twice and then gave up on, silently.
//
// Now those requests arrive here instead. While the workspace is awake they are
// forwarded and the answer is mirrored back, so nothing changes for anyone.
// While it is away they are written to the queue, answered 202, and replayed
// byte for byte once the workspace is back — the signature check still happens
// inside server.py, which is the only place that holds the secret.
//
// Three rules shape everything below:
//
//  1. The keeper never decides whether a request is authentic. It knows which
//     ids exist, which is enough to answer 404 and wake nothing for a stranger
//     walking URLs, and it knows nothing else. A request for a known id with a
//     wrong signature wakes the workspace and is then rejected by the
//     workspace — the cost of a wake, not of a task.
//
//  2. Decide from the outcome of the forward, never from cached state alone.
//     The state is a one-second-old guess used to pick the first action; what
//     the reply says is decided by what actually happened.
//
//  3. Byte fidelity is a security property, not a nicety. The HMAC covers the
//     exact body, so any mutation the keeper makes turns into a 404 with ledger
//     reason='bad_signature', which reads to its owner as a wrong secret. That
//     is why the body is buffered and re-sent with an explicit Content-Length
//     rather than streamed through httputil.ReverseProxy: handle_webhook_receive
//     reads exactly int(headers['Content-Length']) (handlers/triggers.py:156)
//     and nothing in server.py de-chunks a request, so a chunked forward would
//     run the HMAC over an empty body.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

// proxyMaxBody is the largest request the keeper will buffer: 1 MiB, the same
// number in all three places that have an opinion. ingress-webhooks.yaml sets
// proxy-body-size: 1m and handle_webhook_receive rejects anything over
// 1*1024*1024 with a 413, so a request of exactly 1 MiB must pass here and one
// byte more must not — if the keeper's cap disagreed with either, one of them
// would be lying about what it accepts.
const proxyMaxBody = 1 << 20

// Why the wake is triggered, for the holding page's copy. A person who arrives
// to find their workspace starting on its own deserves to be told that a
// scheduled task did it, rather than left to guess.
const (
	reasonVisit   = "visit"
	reasonApp     = "app"
	reasonWebhook = "webhook"
	reasonCron    = "cron"
)

// forwardOutcome says what became of an attempted delivery. The three cases are
// genuinely different and the entry's fate differs for each: a request that was
// never written can be retried freely, and one that was written cannot.
type forwardOutcome int

const (
	// forwardResponded: a status line came back. The workspace has the request
	// and has answered it, whatever the answer was.
	forwardResponded forwardOutcome = iota
	// forwardDialFailed: no connection was established, so not a byte of the
	// request reached the workspace. Safe to re-decide from fresh state.
	forwardDialFailed
	// forwardAmbiguous: the request went out and no answer came back. It may
	// have run. This is the case the per-kind semantics exist for.
	forwardAmbiguous
)

// proxyRoute recognises the three paths the keeper stands in front of, and
// nothing else.
//
// The id is held to validTriggerID, which admits no dot, slash or percent. That
// is the path-traversal guard: /api/webhooks/../../api/keeper/wake does not
// parse as a route here, so it falls through to the app handler and is a 404.
// Percent-encoded forms never get this far — RawPath is non-empty for those and
// the caller rejects them before asking.
func proxyRoute(path string) (kind, id string, ok bool) {
	for _, r := range []struct{ prefix, kind string }{
		{"/api/webhooks/", "webhook"},
		{"/api/triggers/cron-fire/", "cron"},
		{"/api/triggers/page-watch-check/", "page-watch"},
	} {
		if !strings.HasPrefix(path, r.prefix) {
			continue
		}
		rest := path[len(r.prefix):]
		if strings.Contains(rest, "/") {
			return "", "", false
		}
		return r.kind, rest, true
	}
	return "", "", false
}

// ProxyHandler is what :8082 serves: the three proxied routes, and the app's
// own status and wake behind them.
//
// One listener for both because they share a peer — everything here arrives
// from the ingress controller or from a CronJob pod in this namespace — and
// because the keeper's own paths must keep answering while a wake is under way.
// The app's two routes are checked first by AppHandler's exact match, and the
// three proxied prefixes cannot collide with them.
func (s *Server) ProxyHandler() http.Handler {
	app := s.AppHandler()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawPath != "" {
			// Percent-encoded: never one of ours, and never worth decoding to
			// find out. The app handler answers 404.
			app.ServeHTTP(w, r)
			return
		}
		kind, id, ok := proxyRoute(r.URL.Path)
		if !ok {
			app.ServeHTTP(w, r)
			return
		}
		if r.Method != http.MethodPost {
			// All three receivers are POST-only in server.py. Answering here
			// keeps a browser that wandered onto a webhook URL from waking the
			// workspace.
			methodNotAllowed(w, "POST")
			return
		}
		s.serveProxy(w, r, kind, id)
	})
}

// notFound is the answer for an id the keeper does not know, in the exact shape
// and wording server.py uses for the same thing (handlers/triggers.py:139).
// Matching it matters: a sender must not be able to tell "no such webhook" from
// "wrong signature" from "the keeper has not been told about this one yet", and
// a probe must not be able to map which ids exist.
func notFound(w http.ResponseWriter) {
	writeJSON(w, http.StatusNotFound, map[string]string{"error": "Not found or unauthorized"})
}

// stopped is the answer for an admin-stopped workspace: never woken, never
// queued. One function because the keeper can find this out in two places — at
// the door, and on the re-read after a failed dial — and the two must not
// drift.
func stopped(w http.ResponseWriter) {
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"code":  "workspace_stopped",
		"error": "This workspace is stopped, so it cannot run anything. Start it from the dashboard or the workspace controller.",
	})
}

// dropForStopped forgets an entry the keeper had already accepted, because the
// workspace it was for has been stopped.
func (s *Server) dropForStopped(w http.ResponseWriter, e *Entry, token string) {
	if token != "" {
		s.Queue.Done(token)
	}
	stopped(w)
}

// reasonFor turns a queued entry's kind into the wake reason the holding page
// shows. A page-watch is a schedule as far as the person reading it is
// concerned, so it shares the cron wording.
func reasonFor(kind string) string {
	if kind == "webhook" {
		return reasonWebhook
	}
	return reasonCron
}

// serveProxy is the decision table.
func (s *Server) serveProxy(w http.ResponseWriter, r *http.Request, kind, id string) {
	if s.Store == nil || s.Queue == nil {
		// Unreachable in the chart, which gives this listener both. A keeper
		// without them cannot tell a real id from a guess, and the fail-safe
		// direction — the same one keeper_idle resolves every ambiguity in — is
		// to act on nothing.
		notFound(w)
		return
	}
	trig, known := s.Store.Lookup(kind, id)
	if !known {
		// No wake, no entry, and the log line is deliberately coarse: one line
		// per unknown id would let a stranger with a URL list fill the log.
		log.Printf("proxy: %s/%s is not a trigger this workspace has; refused without waking it", kind, id)
		notFound(w)
		return
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, proxyMaxBody))
	if err != nil {
		log.Printf("proxy: %s/%s: %v", kind, id, err)
		// server.py's own answer to the same condition, so a sender sees one
		// behaviour whether the workspace is up or not.
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "payload too large"})
		return
	}

	state, stateErr := s.cachedState(r.Context())
	if stateErr != nil {
		// The API server is unreachable or the Role is wrong. The request is
		// real and the keeper cannot tell what to do with it, so keep it and
		// wake nothing: a wake decided on no information is the one thing worse
		// than a late delivery.
		log.Printf("proxy: %s/%s: cannot read the workspace's state, queueing without waking: %v", kind, id, stateErr)
		state = ""
	}
	if state == StateStopped {
		// Nothing is queued. An admin stop lasts as long as the admin wants,
		// and a queue that accepted requests for days would be a way to fill a
		// volume from the internet.
		log.Printf("proxy: %s/%s refused: the workspace is stopped by an admin", kind, id)
		stopped(w)
		return
	}

	e := &Entry{
		Kind: kind, ID: id, Provider: trig.Provider,
		Method: r.Method, Path: r.URL.RequestURI(),
		Header: forwardableHeader(r.Header), Body: body,
		QueuedAt: time.Now(),
	}

	// Durable first, always. Everything after this point may crash without
	// losing the request.
	token, err := s.Queue.Put(e)
	switch {
	case errors.Is(err, ErrQueueFull) && state == StateAwake:
		// The entry is only crash insurance on this path: the forward is about
		// to happen synchronously and its answer is mirrored back, so a sender
		// that gets a 2xx has been served. Refusing a request the workspace can
		// handle right now, because of a backlog it is draining right now,
		// would be the wrong trade.
		log.Printf("proxy: %s/%s: the queue is full, forwarding without a crash-recovery entry", kind, id)
		token = ""
	case errors.Is(err, ErrQueueFull):
		// Away, and nowhere to keep the bytes. Say so instead of answering 202
		// for a request nobody wrote down — a visible failure the sender can
		// retry beats a silent loss.
		log.Printf("proxy: %s/%s refused: the queue is full (%d waiting)", kind, id, s.Queue.Len())
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"code":  "queue_full",
			"error": "This workspace is away and its queue of waiting requests is full. Try again once it is back.",
		})
		return
	case err != nil:
		log.Printf("proxy: %s/%s: %v", kind, id, err)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"code":  "queue_unavailable",
			"error": "This workspace is away and the keeper could not store the request. Try again shortly.",
		})
		return
	}

	if state == StateAwake {
		s.forwardLive(w, r, e, token)
		return
	}
	s.queued(w, e, token, state)
}

// forwardLive forwards a request the workspace should be able to answer now,
// and mirrors the answer back.
func (s *Server) forwardLive(w http.ResponseWriter, r *http.Request, e *Entry, token string) {
	claimed := token != ""
	if claimed {
		// Take ownership before delivering, so a drain that wakes up in the
		// middle of this cannot pick the same entry up and deliver it twice.
		if err := s.Queue.ClaimToken(token); err != nil {
			// Somebody else owns it, which means somebody else is delivering
			// it. Nothing to do but tell the sender it is in hand.
			log.Printf("proxy: %s/%s is already being delivered; answering queued", e.Kind, e.ID)
			s.accepted(w, e, StateAwake)
			return
		}
	}
	outcome, resp := s.forward(r.Context(), e)
	switch outcome {
	case forwardResponded:
		defer func() { _ = resp.Body.Close() }()
		if claimed {
			// The workspace answered, so this request has been handled whatever
			// the answer was. Forget it before the body is copied: a client that
			// hangs up mid-copy must not leave the entry to be replayed.
			s.Queue.Done(token)
		}
		mirror(w, resp)
	case forwardDialFailed:
		// Not a byte was written, so the request can be re-decided from scratch.
		// The state was awake a moment ago and the connection was refused, which
		// usually means the pod went away between the two — so look again,
		// bypassing the cache that told us the stale answer.
		state, err := s.freshState(r.Context())
		if err != nil {
			log.Printf("proxy: %s/%s: the workspace did not answer and its state cannot be read: %v", e.Kind, e.ID, err)
			state = ""
		}
		if claimed {
			// Return, not Resolve: the connection was refused, so the request
			// demonstrably did not run and this costs it no attempt.
			s.Queue.Return(token, e)
		}
		if state == StateStopped {
			// An admin stopped it between the read and the dial. Answer as the
			// front door would, and keep nothing: one rule for a stopped
			// workspace, applied wherever the keeper finds out about it.
			log.Printf("proxy: %s/%s: the workspace was stopped by an admin mid-forward; dropping the request", e.Kind, e.ID)
			if claimed {
				s.Queue.Claim() // take it back out of pending to delete it
			}
			s.dropForStopped(w, e, token)
			return
		}
		if state == StateAwake {
			// Still awake and still refusing connections. Keep the entry and let
			// the drain loop retry; answering 202 is honest — it is accepted and
			// not yet delivered.
			log.Printf("proxy: %s/%s: the workspace is awake but not answering; keeping the request for the drain loop", e.Kind, e.ID)
			s.accepted(w, e, state)
			return
		}
		s.queued(w, e, token, state)
	case forwardAmbiguous:
		// Written, and no answer. The workspace may have run it. Resolve applies
		// the kind's semantics: a webhook is kept for another attempt, a cron is
		// dropped rather than risk running twice.
		log.Printf("proxy: %s/%s: the connection ended before the workspace answered; resolving as %s", e.Kind, e.ID, semanticsFor(e.Kind))
		if claimed {
			s.Queue.Resolve(token, e)
		}
		s.accepted(w, e, StateAwake)
	}
}

// queued answers a request the workspace cannot take yet, and wakes it if that
// is both needed and allowed.
//
// The 202 comes before the wake, in that order and deliberately: the entry is
// already durable, so the sender can be released immediately and the wake —
// which takes an API round trip — happens on time the sender is not paying for.
//
// And it is flushed, which is the part that makes that true. net/http holds a
// small response in its buffer until the handler returns, so without the flush
// the "answer first" ordering would be invisible from outside: a wake that took
// longer than GitHub's own 10-second timeout would have it record a failed
// delivery for a request that is queued and will run.
func (s *Server) queued(w http.ResponseWriter, e *Entry, token string, state string) {
	s.accepted(w, e, state)
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	if state != StateAsleep {
		// starting: it is already coming up, and spending a wake token on a pod
		// that is already starting buys nothing. Unknown state: see serveProxy.
		if state == StateStarting {
			log.Printf("proxy: %s/%s queued; the workspace is already starting", e.Kind, e.ID)
		}
		return
	}
	if !s.Limiter.Allow() {
		// Decision 4: queued, answered, not woken. It drains at the next wake
		// from any cause, including the budget freeing up, which the drain loop
		// notices on its own.
		log.Printf("proxy: %s/%s queued without waking: the wake budget is spent", e.Kind, e.ID)
		return
	}
	// A context of its own, not the request's: the sender has already been
	// answered and may well have hung up, and a wake must not be cancelled
	// because of that.
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if _, _, err := s.wakeNow(ctx, fmt.Sprintf("%s=%s", e.Kind, e.ID), reasonFor(e.Kind)); err != nil {
		// Logged by wakeNow. The entry stays; the drain loop tries again.
		return
	}
}

// accepted is the 202. It names the state so an operator running curl can see
// why the request was not handled immediately, and carries no detail about the
// trigger beyond what the caller already knew.
func (s *Server) accepted(w http.ResponseWriter, e *Entry, state string) {
	msg := "This workspace is waking up. Your request is queued and will run as soon as it is back."
	if state == StateAwake {
		msg = "Your request is queued and will run shortly."
	}
	writeJSON(w, http.StatusAccepted, map[string]string{
		"status": "queued",
		"code":   "workspace_waking",
		"error":  msg,
	})
}

// forward is the one place a request leaves the keeper.
//
// Two things here are load-bearing. The explicit ContentLength with
// TransferEncoding cleared, for the reason in this file's header. And
// DisableKeepAlives on the transport, which is what makes the dialled flag
// below trustworthy: on a pooled connection no dial happens, so a reset would
// be reported as "never written" when it might have been written — and for a
// cron, the difference between those two is whether an agent runs twice.
func (s *Server) forward(ctx context.Context, e *Entry) (forwardOutcome, *http.Response) {
	// Bounded in flight, and bounded here rather than at the door: a signed
	// webhook must be made durable before it can be shed, or load-shedding
	// becomes a way to lose events.
	if s.forwards != nil {
		select {
		case s.forwards <- struct{}{}:
			defer func() { <-s.forwards }()
		case <-ctx.Done():
			return forwardAmbiguous, nil
		}
	}

	ctx, cancel := context.WithTimeout(ctx, forwardTimeout)
	defer cancel()
	dialed := new(bool)
	ctx = context.WithValue(ctx, dialedKey{}, dialed)

	req, err := http.NewRequestWithContext(ctx, e.Method, s.workspaceURL()+e.Path, bytes.NewReader(e.Body))
	if err != nil {
		log.Printf("proxy: %s/%s: build request: %v", e.Kind, e.ID, err)
		return forwardDialFailed, nil
	}
	req.Header = e.Header.Clone()
	// The length the signature was computed over, stated as a number. Clearing
	// TransferEncoding as well, so nothing downstream can decide to chunk this.
	req.ContentLength = int64(len(e.Body))
	req.TransferEncoding = nil

	resp, err := s.client().Do(req)
	if err == nil {
		return forwardResponded, resp
	}
	if !*dialed {
		log.Printf("proxy: %s/%s: could not reach the workspace: %v", e.Kind, e.ID, err)
		return forwardDialFailed, nil
	}
	log.Printf("proxy: %s/%s: the workspace did not answer: %v", e.Kind, e.ID, err)
	return forwardAmbiguous, nil
}

// forwardTimeout bounds one delivery. A webhook task is started and answered
// 202 by server.py before the agent runs, so the receiver answers in
// milliseconds; a minute is generous for a pod that has just booted and is
// still loading its config.
const forwardTimeout = 60 * time.Second

type dialedKey struct{}

// client is the keeper's one outbound client, built once.
func (s *Server) client() *http.Client {
	s.clientOnce.Do(func() {
		d := &net.Dialer{Timeout: 5 * time.Second}
		s.httpClient = &http.Client{
			// No redirects: a webhook receiver that answered 302 would be a bug,
			// and following one would send the body somewhere it was not signed
			// for.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
			Transport: &http.Transport{
				DisableKeepAlives: true,
				DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
					c, err := d.DialContext(ctx, network, addr)
					if err == nil {
						if flag, ok := ctx.Value(dialedKey{}).(*bool); ok {
							*flag = true
						}
					}
					return c, err
				},
			},
		}
	})
	return s.httpClient
}

// workspaceURL is where the forward goes. The default is the in-cluster Service
// of the Deployment this keeper looks after, which is the same address
// CronManager._apply_k8s computes for itself; KEEPER_WORKSPACE_URL overrides it
// for a cluster whose DNS suffix is not cluster.local.
func (s *Server) workspaceURL() string {
	if s.WorkspaceURL != "" {
		return strings.TrimRight(s.WorkspaceURL, "/")
	}
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:6080", s.Kube.Name, s.Kube.Namespace)
}

// hopByHop are the headers that describe this connection rather than the
// request, and so must not be copied onto the next one. Content-Length is in
// the list because the forward states its own, from the bytes it actually has.
var hopByHop = []string{
	"Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization",
	"Proxy-Connection", "Te", "Trailer", "Transfer-Encoding", "Upgrade",
	"Content-Length",
}

// forwardableHeader copies the headers that belong to the request.
//
// Every value of every header is kept, including repeats: a sender that sends
// X-Hub-Signature-256 twice must be forwarded both, because only the workspace
// gets to decide what that means. X-Forwarded-For is passed through untouched
// and the keeper does not add itself to it — the ingress controller already
// recorded who called, and the ledger's forwarded_for should keep saying that
// rather than naming an internal hop.
func forwardableHeader(h http.Header) http.Header {
	out := make(http.Header, len(h))
	for k, vs := range h {
		if isHopByHop(k) {
			continue
		}
		out[k] = append([]string(nil), vs...)
	}
	return out
}

func isHopByHop(key string) bool {
	for _, h := range hopByHop {
		if strings.EqualFold(key, h) {
			return true
		}
	}
	return false
}

// mirror copies the workspace's answer back to the sender verbatim.
//
// Verbatim includes the status code and the body bytes, because a sender reads
// both: GitHub shows the response body in its delivery log, and a 409 from the
// replay cache means something different to its owner than a 202. The keeper's
// security headers are deliberately not added — this is the workspace's answer
// to its own API caller, not a page in a browser.
func mirror(w http.ResponseWriter, resp *http.Response) {
	dst := w.Header()
	for k, vs := range resp.Header {
		if isHopByHop(k) {
			continue
		}
		for _, v := range vs {
			dst.Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		// The status line is already on the wire, so there is nothing to
		// correct; the sender sees a short body and the entry is already gone,
		// which is right — the workspace has the request.
		log.Printf("proxy: copying the workspace's answer ended early: %v", err)
	}
}

// drainInterval is how often the queue is looked at. Long enough to cost
// nothing (the length is an in-memory counter, and the Kubernetes API is only
// asked when something is waiting), short enough that a request queued just
// after a wake began is delivered as soon as the workspace is ready.
const drainInterval = 5 * time.Second

// drainBatch bounds one pass, so a long backlog cannot keep the loop from
// noticing a shutdown, and a woken workspace is not hit by a thousand replays
// in one breath.
const drainBatch = 20

// DrainLoop delivers what the queue holds, once the workspace can take it.
//
// It is also what makes two of the decision table's rows eventually come true
// rather than merely be recorded: a request queued while the wake budget was
// spent is woken for here once the budget frees up, and a request kept after an
// ambiguous forward is retried here. Nothing else in the keeper would ever look
// at the queue again.
func (s *Server) DrainLoop(ctx context.Context) {
	t := time.NewTicker(drainInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.drainOnce(ctx)
		}
	}
}

func (s *Server) drainOnce(ctx context.Context) {
	if s.Queue == nil || s.Queue.Len() == 0 {
		return
	}
	s.Queue.ExpireOlderThan(time.Now())
	if s.Queue.Len() == 0 {
		return
	}
	// Fresh, not cached. The wake that put these entries here wrote "starting"
	// into the cache on its way past, and a drain that believed it would wait a
	// tick for no reason. One read per tick, and only while something is
	// waiting, is the whole cost.
	state, err := s.freshState(ctx)
	if err != nil {
		log.Printf("proxy: %d request(s) waiting and the workspace's state cannot be read: %v", s.Queue.Len(), err)
		return
	}
	switch state {
	case StateAwake:
		s.deliverWaiting(ctx)
	case StateAsleep:
		// Something is waiting and the workspace is asleep, which means the
		// wake that should have happened at arrival did not — the budget was
		// spent, or the API call failed. Try again, under the same cap.
		if !s.Limiter.Allow() {
			return
		}
		log.Printf("proxy: %d request(s) waiting; waking the workspace to deliver them", s.Queue.Len())
		_, _, _ = s.wakeNow(ctx, "queue", reasonFor(s.Queue.PeekKind()))
	}
	// starting: it is on its way. stopped: leave the entries to expire rather
	// than undo an admin's stop.
}

// deliverWaiting replays entries one at a time, in arrival order. Sequential on
// purpose: a sender that delivered three events expects them to be handled in
// the order it sent them, and a workspace that has just booted should not meet
// the whole backlog at once.
func (s *Server) deliverWaiting(ctx context.Context) {
	for i := 0; i < drainBatch; i++ {
		if ctx.Err() != nil {
			return
		}
		e, token, ok := s.Queue.Claim()
		if !ok {
			return
		}
		outcome, resp := s.forward(ctx, e)
		switch outcome {
		case forwardResponded:
			if resp.StatusCode < 200 || resp.StatusCode >= 300 {
				// The workspace made a decision, so the entry is finished — but
				// a replay rejected after a wake is the thing an operator will
				// want to find, so it is named rather than counted.
				log.Printf("proxy: replayed %s/%s after %s and the workspace answered %d; not retrying",
					e.Kind, e.ID, time.Since(e.QueuedAt).Round(time.Second), resp.StatusCode)
			} else {
				log.Printf("proxy: replayed %s/%s after %s", e.Kind, e.ID, time.Since(e.QueuedAt).Round(time.Second))
			}
			_ = resp.Body.Close()
			s.Queue.Done(token)
		case forwardDialFailed:
			// The workspace was awake a moment ago. Put it back — Return, so a
			// refused connection costs the entry no attempt — and stop the
			// pass: whatever is wrong applies to every other entry too.
			s.Queue.Return(token, e)
			return
		case forwardAmbiguous:
			s.Queue.Resolve(token, e)
		}
	}
}

// queueLimitsFromEnv reads the chart's queue settings. Every one of them has a
// working default, so an older chart that does not set them still runs.
func queueLimitsFromEnv() QueueLimits {
	return QueueLimits{
		MaxEntries: envInt("KEEPER_QUEUE_MAX_ENTRIES", 256),
		MaxBytes:   int64(envInt("KEEPER_QUEUE_MAX_BYTES", 64<<20)),
		TTL:        time.Duration(envInt("KEEPER_QUEUE_TTL_SECONDS", 3600)) * time.Second,
	}
}

// envInt reads a whole number from the environment, falling back to def for
// anything missing or unreadable. A typo in a value must not stop the keeper
// from serving the page, so it is reported and ignored.
func envInt(name string, def int) int {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return def
	}
	n, err := parseInt(v)
	if err != nil || n < 0 {
		log.Printf("%s=%q is not a whole number; using %d", name, v, def)
		return def
	}
	return n
}

func parseInt(s string) (int, error) {
	n := 0
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return 0, fmt.Errorf("not a number")
		}
		n = n*10 + int(s[i]-'0')
		if n > 1<<40 {
			return 0, fmt.Errorf("too large")
		}
	}
	if len(s) == 0 {
		return 0, fmt.Errorf("empty")
	}
	return n, nil
}
