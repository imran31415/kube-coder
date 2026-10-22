package main

import (
	"net/http"
	"strings"
	"testing"
)

const testAPIToken = "tok-the-workspaces-own-api-token"

// newAppServer is a step-4 server that has already been told the token hash,
// which is the state the chart reaches a moment after the workspace boots.
func newAppServer(t *testing.T, f *fakeAPI, k *Kube) *Server {
	t.Helper()
	s := newStep4Server(t, f, k)
	if err := s.Store.SetTokenHash(hashOf(testAPIToken)); err != nil {
		t.Fatal(err)
	}
	return s
}

// appAuth is what the app sends: a bearer and nothing else.
func appAuth() map[string]string {
	return map[string]string{"Authorization": "Bearer " + testAPIToken}
}

func TestAppWakesAnAsleepWorkspace(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newAppServer(t, f, k)
	rec := do(s.AppHandler(), http.MethodPost, "/api/keeper/wake", appAuth())
	if rec.Code != http.StatusOK {
		t.Fatalf("wake = %d %q; want 200", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec); got.State != StateStarting || !got.Woken {
		t.Fatalf("body = %+v; want starting and woken", got)
	}
	if len(f.scales) != 1 || f.scales[0].Replicas != 1 {
		t.Fatalf("scales = %+v; want one scale to 1", f.scales)
	}
}

func TestAppStatus(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s := newAppServer(t, f, k)
	rec := do(s.AppHandler(), http.MethodGet, "/api/keeper/status", appAuth())
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d %q; want 200", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec); got.State != StateAwake {
		t.Fatalf("state = %q; want awake", got.State)
	}
	if len(f.scales) != 0 {
		t.Fatal("status must not write")
	}
}

// TestAppWakeNeedsNoBrowserGuards is the one that keeps the shipped app
// working. #778 sends a bearer and nothing else: no X-Keeper-Wake header, and
// whatever Sec-Fetch-Site a React Native fetch happens to carry. Those two
// guards defend a cookie-authenticated browser from another site acting on the
// user's behalf; a bearer is never attached automatically, so there is nothing
// to forge, and requiring them here would break the app in the field.
func TestAppWakeNeedsNoBrowserGuards(t *testing.T) {
	for _, extra := range []map[string]string{
		nil,
		{"Sec-Fetch-Site": "cross-site"},
		{"Sec-Fetch-Site": "none"},
	} {
		f, k := newFake(t, 0, 0, FieldManager)
		s := newAppServer(t, f, k)
		h := appAuth()
		for key, v := range extra {
			h[key] = v
		}
		rec := do(s.AppHandler(), http.MethodPost, "/api/keeper/wake", h)
		if rec.Code != http.StatusOK {
			t.Fatalf("wake with %v = %d %q; want 200", extra, rec.Code, rec.Body.String())
		}
		if len(f.scales) != 1 {
			t.Fatalf("wake with %v did not scale: %+v", extra, f.scales)
		}
	}
}

// TestAppWakeOnAStoppedWorkspaceSaysSo is why this listener does not simply
// delegate to the browser's handler. That one answers 200 with the state in the
// body and lets the holding page read it; the app reads no state field and
// treats any 2xx as "it is coming up", so a 200 here would leave the banner
// saying "Starting your workspace…" for ever about a pod that will never start.
// It must be a 4xx, and not 404 or 405, which the app reads as "no keeper here"
// and answers by going to the admin controller instead of showing the reason.
func TestAppWakeOnAStoppedWorkspaceSaysSo(t *testing.T) {
	f, k := newFake(t, 0, 0) // replicas 0 owned by nobody: an admin stopped it
	s := newAppServer(t, f, k)
	rec := do(s.AppHandler(), http.MethodPost, "/api/keeper/wake", appAuth())
	if rec.Code != http.StatusConflict {
		t.Fatalf("wake = %d %q; want 409", rec.Code, rec.Body.String())
	}
	if got := errorOf(t, rec); got != msgAppStopped {
		t.Fatalf("message = %q; want the stopped copy", got)
	}
	if len(f.scales) != 0 {
		t.Fatalf("a stopped workspace was written to: %+v", f.scales)
	}
}

func TestAppRefusesBadBearers(t *testing.T) {
	cases := []struct {
		name   string
		header map[string]string
	}{
		{"no Authorization header", nil},
		{"the wrong token", map[string]string{"Authorization": "Bearer nope"}},
		{"an empty bearer", map[string]string{"Authorization": "Bearer "}},
		{"the scheme alone", map[string]string{"Authorization": "Bearer"}},
		{"no scheme", map[string]string{"Authorization": testAPIToken}},
		{"basic auth", map[string]string{"Authorization": "Basic " + testAPIToken}},
		// Not a prefix match: the comparison is over hashes.
		{"a prefix of the token", map[string]string{"Authorization": "Bearer " + testAPIToken[:len(testAPIToken)-1]}},
		{"the token plus a byte", map[string]string{"Authorization": "Bearer " + testAPIToken + "x"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s := newAppServer(t, f, k)
			rec := do(s.AppHandler(), http.MethodPost, "/api/keeper/wake", c.header)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("= %d %q; want 403", rec.Code, rec.Body.String())
			}
			if got := errorOf(t, rec); got != msgAppForbidden {
				t.Fatalf("message = %q; want the forbidden copy", got)
			}
			if len(f.scales) != 0 {
				t.Fatalf("a refused wake scaled the workspace: %+v", f.scales)
			}
		})
	}
}

// TestAppAcceptsAnyBearerCase: RFC 7235 makes the scheme case-insensitive and
// clients vary, so a lower-case scheme must still work.
func TestAppAcceptsAnyBearerCase(t *testing.T) {
	for _, scheme := range []string{"Bearer", "bearer", "BEARER", "BeArEr"} {
		f, k := newFake(t, 0, 0, FieldManager)
		s := newAppServer(t, f, k)
		rec := do(s.AppHandler(), http.MethodPost, "/api/keeper/wake",
			map[string]string{"Authorization": scheme + " " + testAPIToken})
		if rec.Code != http.StatusOK {
			t.Fatalf("scheme %q = %d; want 200", scheme, rec.Code)
		}
	}
}

// TestAppRefusesBeforeTheWorkspaceHasSpoken: until the token hash arrives the
// keeper cannot tell the real token from a guess, so it must refuse even the
// real one rather than guess.
func TestAppRefusesBeforeTheWorkspaceHasSpoken(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k) // no SetTokenHash
	rec := do(s.AppHandler(), http.MethodPost, "/api/keeper/wake", appAuth())
	if rec.Code != http.StatusForbidden {
		t.Fatalf("= %d; want 403", rec.Code)
	}
	if len(f.scales) != 0 {
		t.Fatalf("scaled without knowing the token: %+v", f.scales)
	}
}

func TestAppRoutes(t *testing.T) {
	cases := []struct {
		name   string
		method string
		path   string
		want   int
	}{
		{"GET wake", http.MethodGet, "/api/keeper/wake", http.StatusMethodNotAllowed},
		{"POST status", http.MethodPost, "/api/keeper/status", http.StatusMethodNotAllowed},
		{"an unknown path", http.MethodPost, "/api/keeper/nope", http.StatusNotFound},
		{"a trailing slash", http.MethodPost, "/api/keeper/wake/", http.StatusNotFound},
		{"percent-encoded", http.MethodPost, "/api/keeper/%77ake", http.StatusNotFound},
		{"the root", http.MethodGet, "/", http.StatusNotFound},
		// The browser's copies live under /oauth and belong to :8081. This
		// listener does not trim the prefix, so they are not found here.
		{"the oauth prefix", http.MethodPost, "/oauth/api/keeper/wake", http.StatusNotFound},
		// The workspace's own pushes must not be reachable from the ingress.
		{"triggers on the app port", http.MethodPost, "/api/keeper/triggers", http.StatusNotFound},
		{"token-hash on the app port", http.MethodPost, "/api/keeper/token-hash", http.StatusNotFound},
		// Step 1's endpoint is the workspace's, not the keeper's.
		{"idle on the app port", http.MethodGet, "/api/keeper/idle", http.StatusNotFound},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s := newAppServer(t, f, k)
			rec := do(s.AppHandler(), c.method, c.path, appAuth())
			if rec.Code != c.want {
				t.Fatalf("%s %s = %d %q; want %d", c.method, c.path, rec.Code, rec.Body.String(), c.want)
			}
			if len(f.scales) != 0 {
				t.Fatalf("%s %s scaled the workspace: %+v", c.method, c.path, f.scales)
			}
		})
	}
}

// TestAppMessagesAreForPeople: the app renders the "error" field verbatim in
// the Wake banner, so these strings are copy. Assert they read like it — no
// status codes, no Go identifiers, and a hint at what to do.
func TestAppMessagesAreForPeople(t *testing.T) {
	for _, m := range []string{msgAppForbidden, msgAppStopped} {
		if len(m) < 20 || !strings.HasSuffix(m, ".") {
			t.Fatalf("%q does not read like a sentence shown to a person", m)
		}
		for _, jargon := range []string{"403", "409", "nil", "err", "HTTP", "keeper"} {
			if strings.Contains(m, jargon) {
				t.Fatalf("%q leaks %q into user-facing copy", m, jargon)
			}
		}
	}
}
