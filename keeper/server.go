package main

// Two listeners, split by who can reach them:
//
//   :8080, behind Service keeper-<user> — the page. The workspace ingresses
//     use that Service as their custom-error / default backend, exactly as
//     they used holding-<user> (#184), so every request that finds no Ready
//     workspace pod lands here — including, in oauth2 mode, requests to the
//     root ingress, which carries no auth annotation. So this listener only
//     ever serves the page, and never acts.
//
//   :8081, behind Service keeper-<user>-wake — GET /api/keeper/status and
//     POST /api/keeper/wake (under /oauth in oauth2 mode, like the rest of
//     the dashboard's API there). That Service is the backend of exactly one
//     ingress (keeper-ingress.yaml), which carries the same auth annotations
//     as the dashboard, so a wake always comes from someone who passed basic
//     auth or oauth2-proxy. The page Service does not expose this port at all,
//     so no default-backend request can reach it, whatever its URL.
//     (The NetworkPolicy keeps other pods off both ports.)

import (
	"context"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Server holds the keeper's HTTP handlers.
type Server struct {
	Kube     *Kube
	PageFile string // the holding page, a ConfigMap mount (not subPath, so updates arrive)
	// UserHeader names the request header that carries the signed-in user, for
	// the wake log line. Set only where the ingress overwrites it from its auth
	// response (oauth2 mode); empty in basic mode, where a client could send it.
	UserHeader string

	// Store holds what the workspace pushed: the token hash the app's bearer is
	// checked against, and the trigger ids that may wake it (#728 step 4). Nil
	// where no step-4 listener runs — the page and the browser's wake never
	// consult it, so they work without one.
	Store *Store
	// ControlSecretFile is the shared secret the workspace authenticates its
	// pushes with, a mounted Secret. Re-read per request, like Kube.TokenFile,
	// so rotating it needs no restart.
	ControlSecretFile string

	mu        sync.Mutex
	cached    string
	cachedAt  time.Time
	cacheFor  time.Duration
	lastWoken time.Time

	fetchMu sync.Mutex // one API read at a time for status, however many pages poll
	wakeMu  sync.Mutex // one wake at a time: concurrent tabs make one write, not a 409 storm
}

// statusResponse is the JSON both status and wake answer with.
type statusResponse struct {
	State string `json:"state"`
	// Woken is true while a wake the keeper started is still coming up, so the
	// page can say "waking" rather than "restarting" for the same pod start.
	Woken bool `json:"woken,omitempty"`
}

// wokenWindow is how long after a wake a starting workspace counts as woken.
const wokenWindow = 10 * time.Minute

// codeSleeping is the marker the phone app looks for on a non-2xx JSON body to
// tell "the workspace is away" from "the workspace answered with an error"
// (#778, mobile/src/util/workspaceReach.ts). Changing this string breaks a
// shipped app.
const codeSleeping = "workspace_sleeping"

// PageHandler serves :8080 — the away answer, for whoever the ingress sends
// here. It never touches the Kubernetes API: in oauth2 mode the root ingress
// has no auth annotation, so this listener takes unauthenticated requests and
// must not act, or even look.
func (s *Server) PageHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			_, _ = w.Write([]byte("ok\n"))
			return
		}
		if wantsJSON(r) {
			s.serveSleepingJSON(w)
			return
		}
		s.servePage(w, r)
	})
}

// wantsJSON reports whether the caller asked for JSON rather than a page.
//
// Two headers, because this listener is reached two ways: directly, where the
// client's own Accept arrives, and as the ingress's custom-error backend, where
// ingress-nginx passes the original Accept along as X-Format.
//
// Keyed on what the caller asked for, never on the path. A path rule looks
// tempting — "/api/… is an API" — but /health and /api/keeper/wake are both
// paths a browser lands on while the workspace is away, and they must still get
// the page. The app sends exactly "application/json"; a browser sends a long
// text/html,…,*/* list that never names JSON; curl sends */*. So naming JSON is
// the signal, and everything else, including no Accept at all, gets the page.
func wantsJSON(r *http.Request) bool {
	for _, h := range []string{r.Header.Get("Accept"), r.Header.Get("X-Format")} {
		if strings.Contains(strings.ToLower(h), "application/json") {
			return true
		}
	}
	return false
}

// serveSleepingJSON is the away answer for an API caller, in the shape the app
// already understands.
//
// It names no state, because this listener never reads the API. "Away" is all
// it can honestly say, and all the app needs: it shows its banner and offers
// Wake, and the wake listener — which does know — answers that.
func (s *Server) serveSleepingJSON(w http.ResponseWriter) {
	w.Header().Set("Retry-After", "5")
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"code": codeSleeping,
		// For a person running curl. The app ignores this and shows its own
		// wording, keyed off the code above.
		"error": "Your workspace is asleep.",
	})
}

// securityHeaders goes on every response. The page wakes an asleep workspace
// when it is shown, so it must not be shown inside another site's frame: that
// would let any page a signed-in user visits wake their workspace. Same-origin
// frames stay allowed, since the dashboard frames its own tools and the
// ingress serves this page in their place while the workspace is away.
func securityHeaders(h http.Header) {
	h.Set("X-Frame-Options", "SAMEORIGIN")
	h.Set("Content-Security-Policy", "frame-ancestors 'self'")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Referrer-Policy", "same-origin")
}

// servePage answers every path with the page and a 503, like the holding
// nginx did: the page's own /health poll relies on "not 2xx" meaning the
// workspace is still away, and a 2xx meaning it is back.
func (s *Server) servePage(w http.ResponseWriter, r *http.Request) {
	page, err := os.ReadFile(s.PageFile)
	h := w.Header()
	securityHeaders(h)
	h.Set("Cache-Control", "no-store")
	h.Set("Retry-After", "5")
	if err != nil {
		log.Printf("page: %v", err)
		h.Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("Your workspace is not available right now.\n"))
		return
	}
	h.Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusServiceUnavailable)
	if r.Method != http.MethodHead {
		_, _ = w.Write(page)
	}
}

// WakeHandler serves :8081. Exact paths only — anything else is a 404. This is
// where "exact" is actually enforced: on a host where any other Ingress uses a
// regex or rewrite-target (both auth modes do), ingress-nginx renders the
// keeper Ingress's Exact paths as case-insensitive prefix regexes, so
// /api/keeper/wake/ and /API/KEEPER/WAKE arrive here too (still through the
// Ingress's auth). In oauth2
// mode the routes sit under the dashboard's /oauth prefix, as every other
// browser API call does there; plain /api/keeper/* belongs to the phone's
// bearer ingress (step 4).
func (s *Server) WakeHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if r.URL.RawPath != "" {
			path = "" // percent-encoded: never one of ours
		}
		path = strings.TrimPrefix(path, "/oauth")
		switch {
		case path == "/api/keeper/status":
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				methodNotAllowed(w, "GET, HEAD")
				return
			}
			s.status(w, r)
		case path == "/api/keeper/wake":
			if r.Method != http.MethodPost {
				methodNotAllowed(w, "POST")
				return
			}
			s.wake(w, r)
		default:
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		}
	})
}

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	state, err := s.cachedState(r.Context())
	if err != nil {
		log.Printf("status: %v", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "cannot read the workspace's state"})
		return
	}
	writeJSON(w, http.StatusOK, s.response(state))
}

func (s *Server) wake(w http.ResponseWriter, r *http.Request) {
	// A custom header a cross-site form or image can't send, and a fetch
	// can't send cross-origin without a CORS preflight this server never
	// answers: a page on another site can't make a logged-in browser wake
	// the workspace.
	if r.Header.Get("X-Keeper-Wake") != "1" {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing X-Keeper-Wake header"})
		return
	}
	// Browsers say where a request came from; the page's own fetch is
	// same-origin. Anything else (a sibling workspace's subdomain, say) is
	// refused even if it found a way to send the header.
	if site := r.Header.Get("Sec-Fetch-Site"); site != "" && site != "same-origin" {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "cross-origin wake refused"})
		return
	}
	state, _, err := s.doWake(r)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "could not wake the workspace"})
		return
	}
	writeJSON(w, http.StatusOK, s.response(state))
}

// doWake is the wake itself, with no view attached. The guards in front of it
// and the reply after it differ between the browser page and the app
// (bearer.go) — the decision does not. It serialises wakes so concurrent
// callers make one write, logs who caused one, and refreshes the cache so the
// next status poll costs no API read.
func (s *Server) doWake(r *http.Request) (state string, woke bool, err error) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	s.wakeMu.Lock()
	state, woke, err = Wake(ctx, s.Kube)
	s.wakeMu.Unlock()
	if err != nil {
		log.Printf("wake: %s/%s for %s: %v", s.Kube.Namespace, s.Kube.Name, s.who(r), err)
		return "", false, err
	}
	switch {
	case woke:
		log.Printf("wake: %s/%s scaled 0 -> 1 for %s", s.Kube.Namespace, s.Kube.Name, s.who(r))
	case state == StateStopped:
		log.Printf("wake: %s/%s is stopped by an admin; not woken for %s", s.Kube.Namespace, s.Kube.Name, s.who(r))
	}
	s.mu.Lock()
	if woke {
		s.lastWoken = time.Now()
	}
	s.cached, s.cachedAt = state, time.Now()
	s.mu.Unlock()
	return state, woke, nil
}

// who names the requester for the log: the signed-in user where the ingress
// vouches for it, and the client address ingress-nginx saw.
func (s *Server) who(r *http.Request) string {
	client := r.Header.Get("X-Real-IP")
	if client == "" {
		client, _, _ = net.SplitHostPort(r.RemoteAddr)
	}
	if s.UserHeader != "" {
		if u := r.Header.Get(s.UserHeader); u != "" {
			return "user=" + u + " client=" + client
		}
	}
	return "client=" + client
}

// cachedState reads the Deployment at most once per cacheFor, so a few open
// pages polling every few seconds cost the API server one GET between them —
// also when their polls arrive together.
func (s *Server) cachedState(ctx context.Context) (string, error) {
	fresh := func() (string, bool) {
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.cached, s.cached != "" && time.Since(s.cachedAt) < s.cacheFor
	}
	if state, ok := fresh(); ok {
		return state, nil
	}
	s.fetchMu.Lock()
	defer s.fetchMu.Unlock()
	if state, ok := fresh(); ok { // another poll fetched it while this one waited
		return state, nil
	}
	d, err := s.Kube.Get(ctx)
	if err != nil {
		return "", err
	}
	state := Classify(d)
	s.mu.Lock()
	s.cached, s.cachedAt = state, time.Now()
	s.mu.Unlock()
	return state, nil
}

func (s *Server) response(state string) statusResponse {
	s.mu.Lock()
	defer s.mu.Unlock()
	woken := !s.lastWoken.IsZero() && time.Since(s.lastWoken) < wokenWindow
	return statusResponse{State: state, Woken: woken && (state == StateStarting || state == StateAwake)}
}

func methodNotAllowed(w http.ResponseWriter, allow string) {
	w.Header().Set("Allow", allow)
	writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	securityHeaders(w.Header())
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
