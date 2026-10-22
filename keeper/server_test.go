package main

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func newServer(t *testing.T, f *fakeAPI, k *Kube) *Server {
	t.Helper()
	page := filepath.Join(t.TempDir(), "holding.html")
	if err := os.WriteFile(page, []byte("<h1>Waking up…</h1>"), 0o644); err != nil {
		t.Fatal(err)
	}
	return &Server{Kube: k, PageFile: page, cacheFor: time.Second}
}

func do(h http.Handler, method, target string, header map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, target, nil)
	for k, v := range header {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) statusResponse {
	t.Helper()
	var s statusResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &s); err != nil {
		t.Fatalf("body %q: %v", rec.Body.String(), err)
	}
	return s
}

func TestPageAnswersEveryPathWithTheHoldingPage(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	h := newServer(t, f, k).PageHandler()
	for _, p := range []string{"/", "/oauth/", "/health", "/api/keeper/wake", "/api/claude/chat"} {
		for _, m := range []string{http.MethodGet, http.MethodPost} {
			rec := do(h, m, p, map[string]string{"X-Keeper-Wake": "1"})
			if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "Waking up") {
				t.Fatalf("%s %s = %d %q; want 503 + page", m, p, rec.Code, rec.Body.String())
			}
			if rec.Header().Get("Cache-Control") != "no-store" {
				t.Fatalf("%s %s: page must not be cached", m, p)
			}
		}
	}
	// The page listener is reachable without auth in oauth2 mode: it must
	// never act, whatever it is sent.
	if len(f.scales) != 0 || f.gets != 0 {
		t.Fatalf("page listener touched the API: %d gets, %+v", f.gets, f.scales)
	}
}

func TestPageHealthzAndHead(t *testing.T) {
	f, k := newFake(t, 0, 0)
	h := newServer(t, f, k).PageHandler()
	if rec := do(h, http.MethodGet, "/healthz", nil); rec.Code != http.StatusOK {
		t.Fatalf("/healthz = %d", rec.Code)
	}
	if rec := do(h, http.MethodHead, "/", nil); rec.Code != http.StatusServiceUnavailable || rec.Body.Len() != 0 {
		t.Fatalf("HEAD / = %d with %d bytes; want 503, empty", rec.Code, rec.Body.Len())
	}
}

func TestPageMissingFileStillAnswers(t *testing.T) {
	f, k := newFake(t, 0, 0)
	s := newServer(t, f, k)
	s.PageFile = filepath.Join(t.TempDir(), "gone.html")
	if rec := do(s.PageHandler(), http.MethodGet, "/", nil); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("missing page = %d; want 503", rec.Code)
	}
}

func TestStatus(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	h := newServer(t, f, k).WakeHandler()
	rec := do(h, http.MethodGet, "/api/keeper/status", nil)
	if rec.Code != http.StatusOK || decode(t, rec).State != StateAsleep {
		t.Fatalf("status = %d %q; want 200 asleep", rec.Code, rec.Body.String())
	}
	do(h, http.MethodGet, "/api/keeper/status", nil)
	if f.gets != 1 {
		t.Fatalf("two polls within a second cost %d API reads; want 1", f.gets)
	}
	if len(f.scales) != 0 {
		t.Fatal("status must never write")
	}
}

func TestWakeEndpoint(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	h := newServer(t, f, k).WakeHandler()
	rec := do(h, http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"})
	got := decode(t, rec)
	if rec.Code != http.StatusOK || got.State != StateStarting || !got.Woken {
		t.Fatalf("wake = %d %+v; want 200 starting woken", rec.Code, got)
	}
	if len(f.scales) != 1 || f.scales[0].Replicas != 1 {
		t.Fatalf("scales = %+v; want one apply of 1", f.scales)
	}
	// The status right after reports the wake, so the page can say "waking".
	if s := decode(t, do(h, http.MethodGet, "/api/keeper/status", nil)); s.State != StateStarting || !s.Woken {
		t.Fatalf("status after wake = %+v", s)
	}
	// A second wake while it starts is a no-op.
	do(h, http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"})
	if len(f.scales) != 1 {
		t.Fatalf("second wake wrote again: %+v", f.scales)
	}
}

// oauth2 mode routes the same two calls under the dashboard's /oauth prefix.
func TestWakeEndpointUnderTheOauthPrefix(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	h := newServer(t, f, k).WakeHandler()
	if s := decode(t, do(h, http.MethodGet, "/oauth/api/keeper/status", nil)); s.State != StateAsleep {
		t.Fatalf("status = %+v; want asleep", s)
	}
	rec := do(h, http.MethodPost, "/oauth/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"})
	if rec.Code != http.StatusOK || len(f.scales) != 1 {
		t.Fatalf("wake = %d, scales %+v; want 200 and one apply", rec.Code, f.scales)
	}
}

func TestWakeEndpointLeavesAStoppedWorkspaceStopped(t *testing.T) {
	f, k := newFake(t, 0, 0) // an admin's 0
	h := newServer(t, f, k).WakeHandler()
	rec := do(h, http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"})
	if s := decode(t, rec); rec.Code != http.StatusOK || s.State != StateStopped || s.Woken {
		t.Fatalf("wake on stopped = %d %+v; want 200 stopped", rec.Code, s)
	}
	if len(f.scales) != 0 {
		t.Fatalf("wake undid an admin Stop: %+v", f.scales)
	}
}

func TestWakeEndpointRefuses(t *testing.T) {
	cases := []struct {
		name, method, path string
		header             map[string]string
		code               int
	}{
		{"no wake header (a cross-site form post)", http.MethodPost, "/api/keeper/wake", nil, http.StatusForbidden},
		{"wrong header value", http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "yes"}, http.StatusForbidden},
		{"GET cannot wake", http.MethodGet, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"}, http.StatusMethodNotAllowed},
		{"POST to status", http.MethodPost, "/api/keeper/status", map[string]string{"X-Keeper-Wake": "1"}, http.StatusMethodNotAllowed},
		{"trailing slash", http.MethodPost, "/api/keeper/wake/", map[string]string{"X-Keeper-Wake": "1"}, http.StatusNotFound},
		{"encoded path", http.MethodPost, "/api/keeper/%77ake", map[string]string{"X-Keeper-Wake": "1"}, http.StatusNotFound},
		{"root", http.MethodPost, "/", map[string]string{"X-Keeper-Wake": "1"}, http.StatusNotFound},
		{"some other prefix", http.MethodPost, "/x/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"}, http.StatusNotFound},
		{"oauth prefix glued on", http.MethodPost, "/oauthapi/keeper/wake", map[string]string{"X-Keeper-Wake": "1"}, http.StatusNotFound},
		{"cross-site fetch", http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1", "Sec-Fetch-Site": "cross-site"}, http.StatusForbidden},
		{"same-site but another origin (a sibling workspace)", http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1", "Sec-Fetch-Site": "same-site"}, http.StatusForbidden},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			rec := do(newServer(t, f, k).WakeHandler(), c.method, c.path, c.header)
			if rec.Code != c.code {
				t.Fatalf("%s %s = %d; want %d", c.method, c.path, rec.Code, c.code)
			}
			if len(f.scales) != 0 {
				t.Fatalf("refused request still woke the workspace: %+v", f.scales)
			}
		})
	}
}

func TestStatusAPIFailure(t *testing.T) {
	f, k := newFake(t, 0, 0)
	f.set(func(f *fakeAPI) { f.failGet = http.StatusForbidden })
	h := newServer(t, f, k).WakeHandler()
	if rec := do(h, http.MethodGet, "/api/keeper/status", nil); rec.Code != http.StatusBadGateway {
		t.Fatalf("status with API down = %d; want 502", rec.Code)
	}
	if rec := do(h, http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"}); rec.Code != http.StatusBadGateway {
		t.Fatalf("wake with API down = %d; want 502", rec.Code)
	}
}

func TestWakeFromTheSameOrigin(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	rec := do(newServer(t, f, k).WakeHandler(), http.MethodPost, "/api/keeper/wake",
		map[string]string{"X-Keeper-Wake": "1", "Sec-Fetch-Site": "same-origin"})
	if rec.Code != http.StatusOK || len(f.scales) != 1 {
		t.Fatalf("same-origin wake = %d, scales %+v; want 200 and one apply", rec.Code, f.scales)
	}
}

// The page wakes the workspace when it is shown, so another site must not be
// able to show it in a frame; same-origin frames (the dashboard's) are fine.
func TestSecurityHeadersOnEveryResponse(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newServer(t, f, k)
	for name, rec := range map[string]*httptest.ResponseRecorder{
		"page":   do(s.PageHandler(), http.MethodGet, "/", nil),
		"status": do(s.WakeHandler(), http.MethodGet, "/api/keeper/status", nil),
		"404":    do(s.WakeHandler(), http.MethodGet, "/nope", nil),
	} {
		h := rec.Header()
		if h.Get("X-Frame-Options") != "SAMEORIGIN" || h.Get("Content-Security-Policy") != "frame-ancestors 'self'" ||
			h.Get("X-Content-Type-Options") != "nosniff" {
			t.Errorf("%s: headers %v", name, h)
		}
	}
}

// Several tabs on one asleep workspace: one write, not one per tab.
func TestConcurrentWakesWriteOnce(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	h := newServer(t, f, k).WakeHandler()
	var wg sync.WaitGroup
	codes := make([]int, 8)
	for i := range codes {
		wg.Add(1)
		go func() {
			defer wg.Done()
			codes[i] = do(h, http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"}).Code
		}()
	}
	wg.Wait()
	for i, c := range codes {
		if c != http.StatusOK {
			t.Fatalf("wake %d = %d; want 200", i, c)
		}
	}
	if len(f.scales) != 1 {
		t.Fatalf("8 concurrent wakes made %d writes; want 1", len(f.scales))
	}
}

func TestConcurrentStatusPollsReadOnce(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	h := newServer(t, f, k).WakeHandler()
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			do(h, http.MethodGet, "/api/keeper/status", nil)
		}()
	}
	wg.Wait()
	if f.gets != 1 {
		t.Fatalf("8 polls arriving together cost %d API reads; want 1", f.gets)
	}
}

func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	return &buf
}

// The wake log names who woke it — the user only where the ingress vouches for
// the header (oauth2 mode sets UserHeader); in basic mode a client could send
// it, so it is ignored.
func TestWakeLogNamesTheRequester(t *testing.T) {
	hdr := map[string]string{"X-Keeper-Wake": "1", "X-Real-IP": "203.0.113.9", "X-Auth-Request-User": "alice"}

	f, k := newFake(t, 0, 0, FieldManager)
	s := newServer(t, f, k)
	s.UserHeader = "X-Auth-Request-User"
	buf := captureLog(t)
	do(s.WakeHandler(), http.MethodPost, "/oauth/api/keeper/wake", hdr)
	if got := buf.String(); !strings.Contains(got, "scaled 0 -> 1 for user=alice client=203.0.113.9") {
		t.Fatalf("oauth2 log = %q", got)
	}

	f, k = newFake(t, 0, 0, FieldManager)
	s = newServer(t, f, k) // basic mode: no UserHeader
	buf = captureLog(t)
	do(s.WakeHandler(), http.MethodPost, "/api/keeper/wake", hdr)
	if got := buf.String(); !strings.Contains(got, "scaled 0 -> 1 for client=203.0.113.9") || strings.Contains(got, "user=") {
		t.Fatalf("basic log = %q; want the client only, never an unvouched user header", got)
	}

	f, k = newFake(t, 0, 0) // stopped by an admin
	s = newServer(t, f, k)
	buf = captureLog(t)
	do(s.WakeHandler(), http.MethodPost, "/api/keeper/wake", hdr)
	if got := buf.String(); !strings.Contains(got, "stopped by an admin; not woken") {
		t.Fatalf("stopped log = %q", got)
	}
}

func TestStartupCheckSaysWhatItFound(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	buf := captureLog(t)
	startupCheck(k)
	if got := buf.String(); !strings.Contains(got, "startup check: ws-ns/ws-alice is asleep") {
		t.Fatalf("log = %q", got)
	}
	f.set(func(f *fakeAPI) { f.failGet = http.StatusForbidden })
	buf.Reset()
	startupCheck(k)
	if got := buf.String(); !strings.Contains(got, "cannot read ws-ns/ws-alice") || !strings.Contains(got, "403") {
		t.Fatalf("log = %q; want the failure, with the API's status", got)
	}
}
