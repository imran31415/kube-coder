// The phone's way in (#728 step 4).
//
// The app shipped in #778 already knows how to show "Your workspace is asleep"
// and a Wake up button; its keeper call was dead code until now, because
// /api/keeper/* on the bearer ingress reached a workspace that wasn't there.
// This listener answers it.
//
// It is a separate listener from the browser's (:8081) because the two
// authenticate differently and nothing else about them differs: the browser is
// vouched for by the ingress (basic auth or oauth2-proxy), the app presents the
// workspace's own API token as a bearer and the keeper checks it against the
// hash the workspace pushed. The keeper can therefore refuse a bad token
// without ever being able to mint a good one.
package main

import (
	"net/http"
	"strings"
)

// Messages this listener returns in the "error" field. The app renders that
// field verbatim as the Wake banner's detail line (mobile/src/api/client.ts
// turns `{"error": …}` into the ApiError message, and WakeBanner shows it in
// red), so these are user-facing copy, not diagnostics. They say what the
// person can do next.
const (
	msgAppForbidden = "This app isn't allowed to wake this workspace. Reconnect it in Settings to refresh its access."
	msgAppStopped   = "Your workspace is stopped, not asleep. Start it from the dashboard or your controller."
)

// AppHandler serves :8082 — GET /api/keeper/status and POST /api/keeper/wake
// for the app, on the bearer ingress.
//
// No /oauth prefix is trimmed here, unlike the browser's handler: the app calls
// the bare paths, and in oauth2 mode the browser's copies live under /oauth
// precisely so these stay free.
func (s *Server) AppHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if r.URL.RawPath != "" {
			path = "" // percent-encoded: never one of ours
		}
		switch path {
		case "/api/keeper/status":
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				methodNotAllowed(w, "GET, HEAD")
				return
			}
			if !s.authApp(w, r) {
				return
			}
			s.status(w, r)
		case "/api/keeper/wake":
			if r.Method != http.MethodPost {
				methodNotAllowed(w, "POST")
				return
			}
			if !s.authApp(w, r) {
				return
			}
			s.appWake(w, r)
		default:
			// 404 and 405 are what the app reads as "no keeper in front of this
			// workspace" (wakeViaKeeper returns false on either), so it falls
			// back to the admin controller. That is the right meaning for a
			// path this keeper does not serve.
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		}
	})
}

// authApp checks the bearer against the hash the workspace pushed.
//
// It deliberately does not apply the browser's guards. X-Keeper-Wake and
// Sec-Fetch-Site defend a cookie-authenticated browser against another site
// making the request on the user's behalf; a bearer token is never attached
// automatically by anything, so there is no cross-site request to forge, and
// requiring either would simply break the shipped app, which sends neither.
//
// 403 rather than 401: there is no challenge to offer, and the app treats a 401
// as "your session ended" when the actual fix is to reconnect.
func (s *Server) authApp(w http.ResponseWriter, r *http.Request) bool {
	// A nil Store means no step-4 state at all, so nothing can be verified.
	// Refusing is the only safe answer; it is also unreachable in the chart,
	// which always gives this listener a store.
	if s.Store == nil || !s.Store.BearerMatches(bearerToken(r)) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": msgAppForbidden})
		return false
	}
	return true
}

// appWake wakes the workspace for the app and translates the outcome into
// something the shipped app renders correctly.
//
// The translation is the point. The browser's handler answers 200 with the
// state in the body, and the holding page branches on it. The app parses no
// state field at all, and reads any 2xx as "it is coming up" — so a 200 for an
// admin-stopped workspace would leave the banner saying "Starting your
// workspace…" for ever, for a pod that will never start. A stopped workspace
// therefore gets a 4xx with copy that says what it is.
//
// Not rate-limited: the wake budget bounds unattended callers (webhooks and
// crons), not a person holding this workspace's own token. A Wake button that
// silently did nothing would be a worse failure than an extra pod start.
func (s *Server) appWake(w http.ResponseWriter, r *http.Request) {
	state, _, err := s.doWake(r)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Couldn't reach Kubernetes to wake your workspace. Try again in a moment."})
		return
	}
	if state == StateStopped {
		// 409, not 404 or 405: those two mean "no keeper here" to the app and
		// would send it off to the controller instead of showing the reason.
		writeJSON(w, http.StatusConflict, map[string]string{"error": msgAppStopped})
		return
	}
	writeJSON(w, http.StatusOK, s.response(state))
}

// bearerToken pulls the token out of an Authorization header. Scheme matching
// is case-insensitive because RFC 7235 says so and clients vary.
func bearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if len(h) < 7 || !strings.EqualFold(h[:7], "bearer ") {
		return ""
	}
	return strings.TrimSpace(h[7:])
}
